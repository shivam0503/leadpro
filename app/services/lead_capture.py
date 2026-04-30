"""
services/lead_capture.py
─────────────────────────
Handles lead creation from two sources:
  1. Embedded form (name, email, phone, course, message)
  2. WhatsApp inbound message

Key improvements:
  - Conversation history included in AI context (learns from past chats)
  - Separate prompts for form (first-touch) vs WhatsApp (ongoing chat)
  - Language switching based on company ai_language setting
  - No meta-instructions leak ("Sure! Here's a reply you can send...")
  - Natural continuation — doesn't repeat greetings every message
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.db.company_models import Lead, LeadNote
from app.services.scraper import search_knowledge

ADMIN_NOTIFY_PHONE = os.getenv("ADMIN_NOTIFY_PHONE", "").strip()


def _clean_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if not phone.startswith("+"):
        return f"+{digits}"
    return phone


def _find_or_create_lead(db, phone, name, email, company_name, source):
    clean_phone = _clean_phone(phone)
    existing = db.query(Lead).filter(Lead.phone == clean_phone).first()
    if existing:
        if name and not existing.contact_name:
            existing.contact_name = name
        if email and not existing.email:
            existing.email = email
        existing.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(existing)
        return existing, False

    lead = Lead(
        company=company_name or name or "Unknown",
        contact_name=name, email=email, phone=clean_phone,
        source=source, status="new", score=10,
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead, True


# ── Real-time Lead Scoring ────────────────────────────────────────────────────

def _update_lead_score(db: Session, lead: Lead) -> int:
    """
    Calculate and update lead score based on real engagement signals.
    Called after every interaction (form submission, WhatsApp message, reply).

    Scoring algorithm:
      +10  Has phone number
      +10  Has email
      +5   Has name
      +5   Per form submission (max 20)
      +5   Per WhatsApp inbound message (max 30)
      +3   Per AI reply sent (max 15) — shows engagement
      +10  Has course/pain specified
      +5   Replied within 24h of outreach
      +10  Mentioned pricing/fees/enroll (high intent keywords)

    Buckets: 0-29 Cold, 30-49 Warm, 50+ Hot
    """
    notes = (
        db.query(LeadNote)
        .filter(LeadNote.lead_id == lead.id)
        .order_by(LeadNote.created_at.asc())
        .all()
    )

    score = 0

    # Profile completeness
    if lead.phone:
        score += 10
    if lead.email:
        score += 10
    if lead.contact_name:
        score += 5

    # Course/pain interest specified
    if lead.pain and lead.pain.strip() and lead.pain != "Course: N/A\nMessage: N/A":
        score += 10

    # Count interactions
    form_count = 0
    inbound_count = 0
    ai_reply_count = 0
    agent_reply_count = 0
    all_inbound_text = ""

    for note in notes:
        text = note.note
        if text.startswith("[Form Submission]"):
            form_count += 1
        elif text.startswith("[WhatsApp Inbound]"):
            inbound_count += 1
            all_inbound_text += " " + text.lower()
        elif text.startswith("[AI WhatsApp Reply") or text.startswith("[AI Auto-Reply"):
            ai_reply_count += 1
        elif text.startswith("[Agent Reply"):
            agent_reply_count += 1

    # Engagement scoring
    score += min(20, form_count * 5)       # forms: 5 pts each, max 20
    score += min(30, inbound_count * 5)    # messages: 5 pts each, max 30
    score += min(15, ai_reply_count * 3)   # AI replies: 3 pts each, max 15

    # High-intent keywords in customer messages
    high_intent_words = ["price", "pricing", "fee", "fees", "enroll", "enrol", "register",
                         "join", "admission", "buy", "purchase", "discount", "offer",
                         "payment", "emi", "installment", "batch", "start date", "demo"]
    for word in high_intent_words:
        if word in all_inbound_text:
            score += 10
            break  # only count once

    # Cap at 100
    score = min(100, max(0, score))

    # Update lead
    lead.score = score
    lead.updated_at = datetime.utcnow()
    db.commit()

    logger.debug(f"📊 Lead {lead.id} score updated: {score} "
                 f"(forms={form_count}, msgs={inbound_count}, replies={ai_reply_count})")
    return score


# ── Conversation history ──────────────────────────────────────────────────────

def _get_conversation_history(db: Session, lead_id: int, max_turns: int = 10) -> list[dict]:
    """Load recent conversation as OpenAI chat messages."""
    notes = (
        db.query(LeadNote)
        .filter(LeadNote.lead_id == lead_id)
        .order_by(LeadNote.created_at.desc())
        .limit(max_turns * 2)
        .all()
    )
    notes = list(reversed(notes))

    history = []
    for note in notes:
        text = note.note
        if text.startswith("[WhatsApp Inbound]"):
            content = text.replace("[WhatsApp Inbound]\n", "").strip()
            if content:
                history.append({"role": "user", "content": content})
        elif text.startswith("[Form Submission]"):
            lines = text.split("\n")
            msg_parts = [l for l in lines if l.startswith("Course:") or l.startswith("Message:")]
            if msg_parts:
                history.append({"role": "user", "content": " | ".join(msg_parts)})
        elif any(text.startswith(p) for p in ["[AI WhatsApp Reply", "[AI Auto-Reply", "[AI Reply"]):
            content = text.split("\n\n", 1)[1] if "\n\n" in text else text
            if content.startswith("["):
                content = content.split("]", 1)[-1].strip()
            if content:
                history.append({"role": "assistant", "content": content})
        elif text.startswith("[Agent Reply"):
            content = text.split("\n", 1)[1] if "\n" in text else text
            if content.strip():
                history.append({"role": "assistant", "content": content.strip()})

    return history[-(max_turns * 2):]


def _get_company_config(company_slug: str) -> dict:
    config = {"ai_persona": None, "ai_language": "hinglish"}
    try:
        from app.services.database import _MasterSession
        from app.db.master_models import Company
        mdb = _MasterSession()
        company = mdb.query(Company).filter(Company.slug == company_slug).first()
        if company:
            config["ai_persona"] = company.ai_persona or None
            config["ai_language"] = company.ai_language or "hinglish"
        mdb.close()
    except Exception:
        pass
    return config


LANGUAGE_RULES = {
    "hinglish": "Reply in Hinglish (natural mix of Hindi + English).",
    "hindi": "Reply in Hindi (Devanagari script).",
    "english": "Reply in professional English only. No Hindi or Hinglish.",
    "arabic": "Reply in Arabic only. Use a professional, natural Arabic tone and right-to-left phrasing.",
}


# ── AI Reply: Form submission (first touch) ──────────────────────────────────

def generate_form_reply(db, lead, message, course, company_slug, company_name):
    from app.core.config import settings
    from openai import OpenAI

    query = f"{course or ''} {message or ''}".strip()
    kb_chunks = search_knowledge(query=query, company_slug=company_slug, k=4)
    kb_context = "\n\n".join(kb_chunks) if kb_chunks else ""

    config = _get_company_config(company_slug)
    lang_rule = LANGUAGE_RULES.get(config["ai_language"], LANGUAGE_RULES["hinglish"])
    history = _get_conversation_history(db, lead.id, max_turns=5)
    is_returning = len(history) > 0
    persona = config["ai_persona"] or f"You are a helpful counsellor at {company_name}."

    system_prompt = f"""{persona}

You are chatting DIRECTLY with a customer on WhatsApp. You ARE the company representative.

CRITICAL — OUTPUT FORMAT:
- Output ONLY the message text the customer will see on their phone.
- NEVER write "Here's a reply you can send" or "Sure! Here's a WhatsApp reply" or any meta-text.
- NEVER include "---" dividers or formatting that looks like a draft template.
- Do NOT start with the customer's name every single time. Vary your openings.

LANGUAGE: {lang_rule}
- If the customer writes in English, match and reply in English.
- If mixed, use Hinglish naturally.

BEHAVIOR:
- Keep reply under 120 words. Be direct and specific.
- Use details from knowledge base when relevant. Never invent fees/dates/URLs.
- {"Returning customer. Skip re-introductions." if is_returning else "New enquiry. Brief welcome."}
- End with ONE clear next step.

Knowledge Base:
{kb_context or "No KB data."}"""

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history[-6:])

    form_msg = f"Name: {lead.contact_name or 'Student'}"
    if course:
        form_msg += f", Course: {course}"
    if message:
        form_msg += f", Message: {message}"
    messages.append({"role": "user", "content": form_msg})

    try:
        response = client.chat.completions.create(
            model=settings.OPENAI_MODEL, messages=messages,
            max_tokens=250, temperature=0.7,
        )
        return _clean_ai_reply(response.choices[0].message.content.strip())
    except Exception as e:
        logger.error(f"AI form reply failed: {e}")
        name = lead.contact_name or "there"
        return f"Hi {name}! Thank you for reaching out to {company_name}! Hamari team aapko jaldi contact karegi. 🙏"


# ── AI Reply: WhatsApp inbound (ongoing chat) ────────────────────────────────

def generate_whatsapp_reply(db, lead, inbound_text, company_slug, company_name):
    from app.core.config import settings
    from openai import OpenAI

    query = f"{inbound_text} {lead.pain or ''}".strip()
    kb_chunks = search_knowledge(query=query, company_slug=company_slug, k=4)
    kb_context = "\n\n".join(kb_chunks) if kb_chunks else ""

    config = _get_company_config(company_slug)
    lang_rule = LANGUAGE_RULES.get(config["ai_language"], LANGUAGE_RULES["hinglish"])
    history = _get_conversation_history(db, lead.id, max_turns=8)
    cust_name = lead.contact_name or "the customer"
    persona = config["ai_persona"] or f"You are a helpful counsellor at {company_name}."

    system_prompt = f"""{persona}

You are in an ONGOING WhatsApp conversation with {cust_name}.

CRITICAL — OUTPUT FORMAT:
- Output ONLY the message text. Nothing else.
- NEVER write meta-text like "Here's a reply" or "Sure!".
- NEVER include "---" dividers.

LANGUAGE: {lang_rule}
- Match the customer's language. If they write English, reply in English.

BEHAVIOR:
- This is a CONTINUING chat. Do NOT greet or re-introduce yourself every message.
  Just answer the question directly.
- Be specific. Answer what they actually asked. Don't dodge with generic responses.
- If they ask for URLs/links: only share from knowledge base. If unknown, say you'll send shortly.
- If they ask for pricing: give specific numbers from KB. If not in KB, say you'll confirm.
- Keep reply under 120 words. Concise and helpful.
- Vary your endings. Don't always say "Kya aap kal 10-11 baje free hain".

Knowledge Base:
{kb_context or "No KB data."}"""

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history[-12:])
    messages.append({"role": "user", "content": inbound_text})

    try:
        response = client.chat.completions.create(
            model=settings.OPENAI_MODEL, messages=messages,
            max_tokens=250, temperature=0.7,
        )
        return _clean_ai_reply(response.choices[0].message.content.strip())
    except Exception as e:
        logger.error(f"AI WhatsApp reply failed: {e}")
        return "Thank you for your message! Hamari team aapko jaldi reply karegi. 🙏"


def _clean_ai_reply(reply: str) -> str:
    """Remove meta-instructions that leaked into the AI response."""
    bad_starts = [
        "sure! here's a whatsapp reply", "sure! here is a whatsapp reply",
        "here's a reply you can send", "here is a reply you can send",
        "here's the whatsapp message", "here's a whatsapp reply",
        "whatsapp reply:", "here is the reply:",
    ]
    lower = reply.lower()
    for prefix in bad_starts:
        if lower.startswith(prefix):
            for sep in ["---", "\n\n", ":\n"]:
                if sep in reply:
                    reply = reply.split(sep, 1)[1].strip()
                    break
            else:
                reply = reply[len(prefix):].strip().lstrip(":").strip()
            break

    reply = re.sub(r'^---+\s*', '', reply, flags=re.MULTILINE).strip()
    reply = re.sub(r'\s*---+\s*$', '', reply).strip()
    return reply


# ── Admin notification ────────────────────────────────────────────────────────

def _build_admin_notification(lead_name, lead_phone, lead_email, course, message, company_name, ai_reply, source, is_new):
    tag = "🆕 NEW LEAD" if is_new else "🔄 RETURNING LEAD"
    lines = [
        f"{tag} — {company_name}", "━━━━━━━━━━━━━━━━━━━━",
        f"👤 Name    : {lead_name or 'N/A'}", f"📱 Phone   : {lead_phone or 'N/A'}",
        f"📧 Email   : {lead_email or 'N/A'}", f"📚 Course  : {course or 'N/A'}",
        f"💬 Message : {message or 'N/A'}", f"🔗 Source  : {source}",
        "━━━━━━━━━━━━━━━━━━━━", "🤖 AI Reply (already sent):", "", ai_reply,
    ]
    return "\n".join(lines)


def _send_admin_notification(lead_name, lead_phone, lead_email, course, message, company_name, ai_reply, source, is_new):
    if not ADMIN_NOTIFY_PHONE:
        return
    try:
        from app.services.whatsapp import send_text
        notification = _build_admin_notification(
            lead_name, lead_phone, lead_email, course, message,
            company_name, ai_reply, source, is_new,
        )
        result = send_text(to_phone=ADMIN_NOTIFY_PHONE, message=notification)
        if result.ok:
            logger.info(f"📋 Admin notified at {ADMIN_NOTIFY_PHONE}")
    except Exception as e:
        logger.warning(f"⚠️ Admin notification error: {e}")


# ── Handlers ──────────────────────────────────────────────────────────────────

def handle_form_lead(db, company_slug, company_name, ai_auto_reply, name, phone, email=None, course=None, message=None, source="website_form"):
    lead, is_new = _find_or_create_lead(db, phone, name, email, name, source)

    note_text = f"[Form Submission]\nName: {name}\nCourse: {course or 'N/A'}\nMessage: {message or 'N/A'}"
    db.add(LeadNote(lead_id=lead.id, note=note_text, created_at=datetime.utcnow()))
    if course or message:
        lead.pain = f"Course: {course or 'N/A'}\nMessage: {message or 'N/A'}"
        lead.updated_at = datetime.utcnow()
    db.commit()

    reply_sent = False
    ai_reply = None

    if ai_auto_reply:
        try:
            ai_reply = generate_form_reply(db, lead, message, course, company_slug, company_name)
            from app.services.whatsapp import send_text
            result = send_text(to_phone=lead.phone, message=ai_reply)
            reply_sent = result.ok
            logger.info(f"✅ Form reply → {lead.phone}: {result.ok}")

            db.add(LeadNote(
                lead_id=lead.id,
                note=f"[AI WhatsApp Reply - {'sent' if reply_sent else 'failed'}]\n\n{ai_reply}",
                created_at=datetime.utcnow(),
            ))
            db.commit()

            _send_admin_notification(name, lead.phone, email, course, message, company_name, ai_reply, source, is_new)
        except Exception as e:
            logger.error(f"Form reply failed for lead {lead.id}: {e}")

    # Update lead score after every form interaction
    try:
        _update_lead_score(db, lead)
    except Exception:
        pass

    return {"lead_id": lead.id, "is_new": is_new, "phone": lead.phone, "reply_sent": reply_sent, "ai_reply": ai_reply}


def handle_whatsapp_inbound(db, company_slug, company_name, ai_auto_reply, from_phone, text):
    lead, is_new = _find_or_create_lead(db, from_phone, None, None, None, "whatsapp")

    db.add(LeadNote(lead_id=lead.id, note=f"[WhatsApp Inbound]\n{text}", created_at=datetime.utcnow()))
    lead.last_contacted_at = datetime.utcnow()
    db.commit()

    reply_sent = False
    ai_reply = None

    if ai_auto_reply:
        try:
            ai_reply = generate_whatsapp_reply(db, lead, text, company_slug, company_name)
            from app.services.whatsapp import send_text
            result = send_text(to_phone=from_phone, message=ai_reply)
            reply_sent = result.ok
            logger.info(f"📤 WhatsApp reply → {from_phone}: {result.ok}")

            db.add(LeadNote(
                lead_id=lead.id,
                note=f"[AI WhatsApp Reply - {'sent' if reply_sent else 'failed'}]\n\n{ai_reply}",
                created_at=datetime.utcnow(),
            ))
            db.commit()

            _send_admin_notification(lead.contact_name, from_phone, lead.email, None, text, company_name, ai_reply, "whatsapp_inbound", is_new)
        except Exception as e:
            logger.error(f"WhatsApp AI reply error: {e}")

    # Update lead score after every WhatsApp interaction
    try:
        _update_lead_score(db, lead)
    except Exception:
        pass

    return {"lead_id": lead.id, "is_new": is_new, "reply_sent": reply_sent, "ai_reply": ai_reply}
