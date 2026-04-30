from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.action_runner import run_actions
from app.agents.decision_engine import decide_reply
from app.db.company_models import Lead, LeadNote
from app.services.vector_store import search_memory, store_memory


def _lead_snapshot(lead: Lead) -> Dict[str, Any]:
    return {
        "id":                lead.id,
        "company":           lead.company,
        "website":           lead.website,
        "pain":              lead.pain,
        "contact_name":      lead.contact_name,
        "email":             lead.email,
        "phone":             lead.phone,
        "source":            lead.source,
        "status":            lead.status,
        "score":             lead.score,
        "last_contacted_at": lead.last_contacted_at.isoformat() if lead.last_contacted_at else None,
        "next_followup_at":  lead.next_followup_at.isoformat() if lead.next_followup_at else None,
        "updated_at":        lead.updated_at.isoformat() if lead.updated_at else None,
    }


def _recent_notes(db: Session, lead_id: int, limit: int = 12) -> list[dict[str, Any]]:
    notes = (
        db.query(LeadNote)
        .filter(LeadNote.lead_id == lead_id)
        .order_by(LeadNote.created_at.desc())
        .limit(limit)
        .all()
    )
    notes = list(reversed(notes))
    return [{"at": n.created_at.isoformat(), "note": n.note} for n in notes]


def _memory_snippets(lead: Lead, k: int = 3) -> list[str]:
    q = f"{lead.company} {lead.website or ''} {lead.email or ''} {lead.phone or ''}".strip()
    try:
        res  = search_memory(query=q, k=k)
        docs = res.get("documents", [[]])[0] if isinstance(res, dict) else []
        return [str(d) for d in (docs or [])][:k]
    except Exception:
        return []


def _store_conversation_memory(lead: Lead, inbound_text: str) -> None:
    try:
        snippet = f"Lead {lead.company} ({lead.phone or ''}) said: {inbound_text.strip()[:500]}"
        store_memory(
            text=snippet,
            metadata={"lead_id": str(lead.id), "phone": lead.phone or "", "source": "whatsapp"},
        )
    except Exception as e:
        logger.debug("Memory store failed: {}", e)


def _kb_context(inbound_text: str, company_slug: str, k: int = 5) -> str:
    """
    Retrieve relevant knowledge chunks for the student's message.
    Returns empty string if KB not available yet.
    """
    try:
        from app.services.scraper import search_knowledge
        chunks = search_knowledge(query=inbound_text, company_slug=company_slug, k=k)
        return "\n\n".join(chunks) if chunks else ""
    except Exception as e:
        logger.debug("KB search failed: {}", e)
        return ""


def handle_whatsapp_inbound(
    db: Session,
    lead_id: int,
    inbound_text: str,
    company_slug: str = "",
) -> Dict[str, Any]:
    """
    Main entry for self-operating behaviour on WhatsApp inbound messages.

    company_slug is used to:
      1. Pull the right KB context for accurate program/fee answers
      2. Load the company's AI persona for the response composer
    """

    lead = db.get(Lead, lead_id)
    if not lead:
        raise ValueError("Lead not found")

    # ── Fetch KB context relevant to this message ─────────────────────────────
    kb = _kb_context(inbound_text, company_slug, k=5) if company_slug else ""

    context = {
        "now_utc":        datetime.utcnow().isoformat(),
        "channel":        "whatsapp",
        "inbound_text":   inbound_text,
        "lead":           _lead_snapshot(lead),
        "recent_notes":   _recent_notes(db, lead_id, limit=14),
        "memory_snippets": _memory_snippets(lead, k=3),
        "company_slug":   company_slug,  # ← passed into orchestrator
        "kb_context":     kb,            # ← injected for composer
        "business": {
            "name":    "Career Launcher",
            "offer":   "Coaching for CAT/MBA, CLAT/Law, IPM/BBA, GMAT, GRE, CUET",
            "cta":     "Enroll now at careerlauncher.com/cl-online or call 8130-038-836",
        },
    }

    _store_conversation_memory(lead, inbound_text)

    decision     = decide_reply(context=context, db=db, lead_id=lead_id, event_id=None)
    action_result = run_actions(db=db, lead_id=lead_id, actions=decision.actions)

    return {
        "decision": {
            "summary":    decision.summary,
            "confidence": decision.confidence,
            "actions":    decision.actions,
        },
        "executed": action_result.executed,
        "skipped":  action_result.skipped,
    }
