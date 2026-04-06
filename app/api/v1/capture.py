"""
api/v1/capture.py
──────────────────
Public endpoints (no auth) for lead capture:
  POST /capture/{company_slug}/form        — form submission
  POST /capture/{company_slug}/whatsapp    — WhatsApp widget click tracking
  GET  /capture/{company_slug}/widget.js   — embeddable JS widget script
  GET  /capture/{company_slug}/form.html   — embeddable form HTML

Webhook endpoints:
  POST /webhooks/{company_slug}/whatsapp   — Twilio/Meta inbound webhook

Dashboard endpoints (auth required):
  GET  /leads                              — all leads with last message
  GET  /leads/{id}/conversation            — full conversation history
  POST /leads/{id}/reply                   — manual agent reply
  PATCH /leads/{id}/assign                 — assign to counsellor
  PATCH /companies/{slug}/ai-toggle        — toggle AI auto-reply
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.security import get_current_user, require_roles
from app.db.company_models import Lead, LeadNote, User
from app.db.master_models import Company
from app.services.database import get_master_db, get_db
from app.services.lead_capture import handle_form_lead, handle_whatsapp_inbound

router = APIRouter(tags=["Lead Capture"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_company_or_404(slug: str, master_db: Session) -> Company:
    c = master_db.query(Company).filter(Company.slug == slug, Company.is_active == True).first()
    if not c:
        raise HTTPException(404, "Company not found")
    return c


def _get_company_db(slug: str) -> Session:
    from app.services.database import _get_company_engine, _company_sessions
    _get_company_engine(slug)
    return _company_sessions[slug]()


# ── Public: Form Submission ───────────────────────────────────────────────────

class FormSubmit(BaseModel):
    name: str
    phone: str
    email: str | None = None
    course: str | None = None
    message: str | None = None


@router.post("/capture/{company_slug}/form")
async def capture_form(
    company_slug: str,
    body: FormSubmit,
    master_db: Session = Depends(get_master_db),
):
    """Public endpoint — receives form submissions from embedded widget."""
    company = _get_company_or_404(company_slug, master_db)
    company_db = _get_company_db(company_slug)

    try:
        result = handle_form_lead(
            db=company_db,
            company_slug=company_slug,
            company_name=company.name,
            ai_auto_reply=not company.wa_demo_mode,
            name=body.name,
            phone=body.phone,
            email=body.email,
            course=body.course,
            message=body.message,
            source="website_form",
        )
        return {
            "ok": True,
            "message": "Thank you! Our counsellor will contact you shortly.",
            "lead_id": result["lead_id"],
        }
    finally:
        company_db.close()


# ── Public: WhatsApp Widget Click ─────────────────────────────────────────────

class WAWidgetClick(BaseModel):
    phone: str | None = None
    message: str | None = None
    page_url: str | None = None


@router.post("/capture/{company_slug}/whatsapp-click")
async def capture_wa_click(
    company_slug: str,
    body: WAWidgetClick,
    master_db: Session = Depends(get_master_db),
):
    """Track WhatsApp widget click and pre-create lead."""
    company = _get_company_or_404(company_slug, master_db)
    company_db = _get_company_db(company_slug)

    try:
        if not body.phone:
            return {"ok": True, "tracked": False}
        result = handle_form_lead(
            db=company_db,
            company_slug=company_slug,
            company_name=company.name,
            ai_auto_reply=False,
            name=None,
            phone=body.phone,
            email=None,
            course=None,
            message=body.message or f"WhatsApp widget click from {body.page_url or 'website'}",
            source="whatsapp_widget",
        )
        return {"ok": True, "lead_id": result["lead_id"]}
    finally:
        company_db.close()


# ── Webhook: Inbound WhatsApp ──────────────────────────────────────────────────

@router.post("/webhooks/{company_slug}/whatsapp")
async def whatsapp_webhook(
    company_slug: str,
    request: Request,
    master_db: Session = Depends(get_master_db),
):
    """Twilio/Meta WhatsApp inbound webhook — per company."""
    logger.info(f"📩 Webhook hit: /webhooks/{company_slug}/whatsapp")

    company = _get_company_or_404(company_slug, master_db)

    content_type = request.headers.get("content-type", "")
    payload: Dict[str, Any] = {}

    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        payload = dict(form)
    else:
        try:
            payload = await request.json()
        except Exception:
            payload = {}

    logger.info(f"📩 Webhook payload keys: {list(payload.keys())}")

    from_phone, text = _parse_whatsapp_payload(payload)
    logger.info(f"📩 Parsed → from_phone={from_phone}, text={text[:80] if text else None}")

    if not from_phone or not text:
        logger.warning(f"⚠️ Webhook ignored — missing from_phone or text. Payload: {payload}")
        return Response(content="<Response></Response>", media_type="application/xml")

    company_db = _get_company_db(company_slug)
    try:
        logger.info(f"📩 Processing inbound from {from_phone}: ai_auto_reply={not company.wa_demo_mode}")
        result = handle_whatsapp_inbound(
            db=company_db,
            company_slug=company_slug,
            company_name=company.name,
            ai_auto_reply=not company.wa_demo_mode,
            from_phone=from_phone,
            text=text,
        )
        logger.info(f"📩 Inbound processed: reply_sent={result.get('reply_sent')}, lead_id={result.get('lead_id')}")
    except Exception as e:
        logger.error(f"❌ Webhook processing error: {e}")
    finally:
        company_db.close()

    return Response(content="<Response></Response>", media_type="application/xml")


@router.get("/webhooks/{company_slug}/whatsapp")
def whatsapp_verify(
    company_slug: str,
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
):
    """Meta WhatsApp webhook verification."""
    if hub_mode and hub_challenge:
        return int(hub_challenge)
    return {"ok": True}


# ── Test: Simulate inbound WhatsApp (dev only) ──────────────────────────────────

class TestInbound(BaseModel):
    from_phone: str
    text: str


@router.post("/webhooks/{company_slug}/whatsapp/test")
async def test_whatsapp_inbound(
    company_slug: str,
    body: TestInbound,
    master_db: Session = Depends(get_master_db),
):
    """DEV ONLY — Simulate an inbound WhatsApp message."""
    company = _get_company_or_404(company_slug, master_db)
    company_db = _get_company_db(company_slug)

    try:
        logger.info(f"🧪 Test inbound from {body.from_phone}: {body.text}")
        result = handle_whatsapp_inbound(
            db=company_db,
            company_slug=company_slug,
            company_name=company.name,
            ai_auto_reply=not company.wa_demo_mode,
            from_phone=body.from_phone,
            text=body.text,
        )
        return {
            "ok": True,
            "lead_id": result["lead_id"],
            "reply_sent": result["reply_sent"],
            "ai_reply": result["ai_reply"],
        }
    finally:
        company_db.close()


# ── Debug: View raw notes for a lead ──────────────────────────────────────────

@router.get("/webhooks/{company_slug}/debug/lead/{lead_id}/notes")
def debug_lead_notes(
    company_slug: str,
    lead_id: int,
    master_db: Session = Depends(get_master_db),
):
    """DEV ONLY — View raw lead notes to debug conversation display issues."""
    _get_company_or_404(company_slug, master_db)
    company_db = _get_company_db(company_slug)
    try:
        notes = (
            company_db.query(LeadNote)
            .filter(LeadNote.lead_id == lead_id)
            .order_by(LeadNote.created_at.asc())
            .all()
        )
        return {
            "lead_id": lead_id,
            "total_notes": len(notes),
            "notes": [
                {
                    "id": n.id,
                    "prefix": n.note[:50],
                    "full_length": len(n.note),
                    "created_at": n.created_at,
                }
                for n in notes
            ],
        }
    finally:
        company_db.close()


def _parse_whatsapp_payload(payload: dict) -> tuple[str | None, str | None]:
    """Extract from_phone and text from Twilio or Meta payload."""
    if "From" in payload and "Body" in payload:
        phone = str(payload["From"]).replace("whatsapp:", "").strip()
        text = str(payload["Body"]).strip()
        return phone or None, text or None

    if "from" in payload and "text" in payload:
        return str(payload["from"]), str(payload["text"])

    try:
        msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
        phone = msg.get("from")
        text = msg.get("text", {}).get("body") if msg.get("type") == "text" else None
        return phone, text
    except Exception:
        return None, None


def _widget_locale(company: Company, request: Request) -> tuple[str, str]:
    lang = (request.query_params.get("lang") or "").strip().lower()
    if not lang:
        lang = "ar" if (company.ai_language or "").strip().lower() == "arabic" else "en"
    direction = (request.query_params.get("dir") or "").strip().lower()
    if direction not in {"ltr", "rtl"}:
        direction = "rtl" if lang.startswith("ar") else "ltr"
    return lang, direction


# ── Dashboard: Lead List with Last Message ────────────────────────────────────

@router.get("/leads/all")
def list_all_leads(
    status: str | None = None,
    source: str | None = None,
    search: str | None = None,
    assigned_to: int | None = None,
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """List all leads with their last message and assignment."""
    q = db.query(Lead)
    if status:
        q = q.filter(Lead.status == status)
    if source:
        q = q.filter(Lead.source == source)
    if search:
        pattern = f"%{search}%"
        from sqlalchemy import or_
        q = q.filter(or_(
            Lead.company.ilike(pattern),
            Lead.contact_name.ilike(pattern),
            Lead.phone.ilike(pattern),
            Lead.email.ilike(pattern),
        ))

    total = q.count()
    leads = q.order_by(Lead.created_at.desc()).offset(offset).limit(limit).all()

    result = []
    for lead in leads:
        last_note = (
            db.query(LeadNote)
            .filter(LeadNote.lead_id == lead.id)
            .order_by(LeadNote.created_at.desc())
            .first()
        )

        counsellor = None
        from app.db.company_models import LeadAssignment
        assignment = db.query(LeadAssignment).filter(LeadAssignment.lead_id == lead.id).first()
        if assignment and assignment.owner_user_id:
            u = db.get(User, assignment.owner_user_id)
            if u:
                counsellor = {"id": u.id, "name": u.name, "email": u.email}

        result.append({
            "id": lead.id,
            "company": lead.company,
            "contact_name": lead.contact_name,
            "phone": lead.phone,
            "email": lead.email,
            "source": lead.source,
            "status": lead.status,
            "score": lead.score,
            "pain": lead.pain,
            "created_at": lead.created_at,
            "last_contacted_at": lead.last_contacted_at,
            "counsellor": counsellor,
            "last_message": {
                "text": last_note.note[:120] if last_note else None,
                "created_at": last_note.created_at if last_note else None,
            } if last_note else None,
        })

    return {"total": total, "offset": offset, "limit": limit, "leads": result}


# ── Dashboard: Conversation History ──────────────────────────────────────────

@router.get("/leads/{lead_id}/conversation")
def get_conversation(
    lead_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Full conversation timeline for a lead."""
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    notes = (
        db.query(LeadNote)
        .filter(LeadNote.lead_id == lead_id)
        .order_by(LeadNote.created_at.asc())
        .all()
    )

    turns = []
    for note in notes:
        text = note.note
        if text.startswith("[WhatsApp Inbound]"):
            turns.append({
                "id": note.id,
                "direction": "inbound",
                "speaker": "student",
                "text": text.replace("[WhatsApp Inbound]\n", "").strip(),
                "created_at": note.created_at,
            })
        elif text.startswith("[AI WhatsApp Reply") or text.startswith("[AI Auto-Reply") or text.startswith("[AI Reply"):
            reply_text = text.split("\n\n", 1)[1] if "\n\n" in text else text
            if reply_text.startswith("["):
                reply_text = reply_text.split("]", 1)[-1].strip()
            turns.append({
                "id": note.id,
                "direction": "outbound",
                "speaker": "ai",
                "text": reply_text.strip(),
                "created_at": note.created_at,
            })
        elif text.startswith("[Agent Reply"):
            reply_text = text.split("\n", 1)[1] if "\n" in text else text
            turns.append({
                "id": note.id,
                "direction": "outbound",
                "speaker": "agent",
                "text": reply_text.strip(),
                "created_at": note.created_at,
            })
        elif text.startswith("[WhatsApp Sent"):
            reply_text = text.split("\n\n", 1)[1] if "\n\n" in text else text
            turns.append({
                "id": note.id,
                "direction": "outbound",
                "speaker": "agent",
                "text": reply_text.strip(),
                "created_at": note.created_at,
            })
        elif text.startswith("[Form Submission]"):
            turns.append({
                "id": note.id,
                "direction": "inbound",
                "speaker": "form",
                "text": text,
                "created_at": note.created_at,
            })
        elif text.startswith("[Follow-up"):
            turns.append({
                "id": note.id,
                "direction": "outbound",
                "speaker": "agent",
                "text": text.split("\n", 1)[1].strip() if "\n" in text else text,
                "created_at": note.created_at,
            })
        else:
            turns.append({
                "id": note.id,
                "direction": "note",
                "speaker": "system",
                "text": text,
                "created_at": note.created_at,
            })

    return {
        "lead": {
            "id": lead.id,
            "company": lead.company,
            "contact_name": lead.contact_name,
            "phone": lead.phone,
            "email": lead.email,
            "source": lead.source,
            "status": lead.status,
            "score": lead.score,
        },
        "conversation": turns,
        "total_messages": len(turns),
    }


# ── Dashboard: Manual Agent Reply ────────────────────────────────────────────

class AgentReply(BaseModel):
    message: str


@router.post("/leads/{lead_id}/reply")
def agent_reply(
    lead_id: int,
    body: AgentReply,
    db: Session = Depends(get_db),
    master_db: Session = Depends(get_master_db),
    user=Depends(get_current_user),
):
    """Agent sends manual WhatsApp reply to lead."""
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    if not lead.phone:
        raise HTTPException(400, "Lead has no phone number")

    company_slug = getattr(user, "company_slug", None)
    company = master_db.query(Company).filter(Company.slug == company_slug).first() if company_slug else None

    from app.services.whatsapp import send_text
    result = send_text(to_phone=lead.phone, message=body.message)

    agent_name = getattr(user, "name", "Agent")
    note = LeadNote(
        lead_id=lead_id,
        note=f"[Agent Reply - {agent_name}]\n{body.message}",
        created_at=datetime.utcnow(),
    )
    db.add(note)
    lead.last_contacted_at = datetime.utcnow()
    db.commit()

    return {
        "ok": result.ok,
        "sent": result.ok,
        "mode": result.mode,
        "error": result.error,
    }


# ── Dashboard: Assign Lead to Counsellor ─────────────────────────────────────

class AssignLead(BaseModel):
    user_id: int


@router.patch("/leads/{lead_id}/assign")
def assign_lead(
    lead_id: int,
    body: AssignLead,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Assign a lead to a counsellor."""
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    from app.db.company_models import LeadAssignment
    db.query(LeadAssignment).filter(LeadAssignment.lead_id == lead_id).delete()

    assignment = LeadAssignment(
        lead_id=lead_id,
        owner_user_id=body.user_id,
        assigned_at=datetime.utcnow(),
    )
    db.add(assignment)
    db.commit()

    counsellor = db.get(User, body.user_id)
    return {
        "ok": True,
        "lead_id": lead_id,
        "assigned_to": {"id": counsellor.id, "name": counsellor.name} if counsellor else None,
    }


# ── Dashboard: AI Toggle ──────────────────────────────────────────────────────

@router.patch("/companies/{slug}/ai-toggle")
def toggle_ai_reply(
    slug: str,
    enabled: bool,
    master_db: Session = Depends(get_master_db),
    user=Depends(get_current_user),
):
    """Toggle AI auto-reply on/off for a company."""
    if user.role not in ("admin", "super_admin"):
        if getattr(user, "company_slug", None) != slug:
            raise HTTPException(403, "Access denied")

    company = master_db.query(Company).filter(Company.slug == slug).first()
    if not company:
        raise HTTPException(404, "Company not found")

    company.wa_demo_mode = not enabled
    company.updated_at = datetime.utcnow()
    master_db.commit()

    return {
        "ok": True,
        "ai_auto_reply": enabled,
        "company": slug,
    }


# ══════════════════════════════════════════════════════════════════════════════
# EMBEDDABLE WIDGETS
# ─────────────────────────────────────────────────────────────────────────────
# All widget endpoints now serve SELF-CONTAINED inline JavaScript.
# No separate static file serving required.
#
# Routes:
#   GET /capture/{slug}/widget.js           — smart: ?mode=both|whatsapp|form
#   GET /capture/{slug}/whatsapp.js         — WhatsApp-only (alias)
#   GET /capture/{slug}/form-widget.js      — Form-only (alias)
#   GET /capture/{slug}/form.html           — Standalone form page (iframe)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/capture/{company_slug}/widget.js", response_class=Response)
def get_widget_js(
    company_slug: str,
    request: Request,
    master_db: Session = Depends(get_master_db),
):
    """
    Smart widget endpoint — reads ?mode= to serve the correct widget(s) inline.
      ?mode=both       (default) — WhatsApp + Lead Form
      ?mode=whatsapp   — WhatsApp button only
      ?mode=form       — Lead form only
    """
    company = _get_company_or_404(company_slug, master_db)
    lang, direction = _widget_locale(company, request)

    # Strip Twilio/Meta "whatsapp:" prefix and leading "+" so wa.me URLs work
    wa_phone = (company.wa_phone_from or "").replace("whatsapp:", "").strip().lstrip("+")

    mode = (request.query_params.get("mode") or "both").strip().lower()

    # Prefer X-Forwarded-Host so the widget's API calls resolve correctly
    # when running behind a reverse proxy (nginx / Caddy / etc.)
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    forwarded_host  = request.headers.get("x-forwarded-host",  "").split(",")[0].strip()
    if forwarded_proto and forwarded_host:
        base = f"{forwarded_proto}://{forwarded_host}"
    else:
        base = str(request.base_url).rstrip("/")

    # Guard: whatsapp mode requires a configured phone number
    if mode == "whatsapp" and not wa_phone:
        logger.warning(f"[widget.js] mode=whatsapp requested for '{company_slug}' but wa_phone_from is not set")
        return Response(
            content=(
                "console.warn('[Sponad] WhatsApp widget: no phone number configured for this company. "
                "Please set wa_phone_from in the company settings.');"
            ),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    if mode == "whatsapp":
        js = _build_whatsapp_widget(company_slug, company.name, wa_phone, base, direction)
    elif mode == "form":
        js = _build_form_widget(company_slug, company.name, base, direction)
    else:
        js = _build_combined_widget(company_slug, company.name, base, wa_phone, direction)

    return Response(
        content=js,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )


@router.get("/capture/{company_slug}/whatsapp.js", response_class=Response)
def get_whatsapp_widget(
    company_slug: str,
    request: Request,
    master_db: Session = Depends(get_master_db),
):
    """WhatsApp-only floating widget (self-contained)."""
    company = _get_company_or_404(company_slug, master_db)
    lang, direction = _widget_locale(company, request)
    wa_phone = (company.wa_phone_from or "").replace("whatsapp:", "").strip().lstrip("+")
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    forwarded_host  = request.headers.get("x-forwarded-host",  "").split(",")[0].strip()
    base = f"{forwarded_proto}://{forwarded_host}" if forwarded_proto and forwarded_host else str(request.base_url).rstrip("/")

    js = _build_whatsapp_widget(company_slug, company.name, wa_phone, base, direction)
    return Response(
        content=js,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"},
    )


@router.get("/capture/{company_slug}/form-widget.js", response_class=Response)
def get_form_widget(
    company_slug: str,
    request: Request,
    master_db: Session = Depends(get_master_db),
):
    """Lead form-only floating widget (self-contained)."""
    company = _get_company_or_404(company_slug, master_db)
    lang, direction = _widget_locale(company, request)
    base = str(request.base_url).rstrip("/")

    js = _build_form_widget(company_slug, company.name, base, direction)
    return Response(
        content=js,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"},
    )


@router.get("/capture/{company_slug}/form.html", response_class=HTMLResponse)
def get_form_html(
    company_slug: str,
    master_db: Session = Depends(get_master_db),
    request: Request = None,
):
    """Standalone form page for iframe embedding."""
    company = _get_company_or_404(company_slug, master_db)
    base_url = str(request.base_url).rstrip("/") if request else ""
    return HTMLResponse(_build_form_page(company_slug, company.name, base_url))


# ══════════════════════════════════════════════════════════════════════════════
# SHARED WIDGET ASSETS
# ══════════════════════════════════════════════════════════════════════════════

_WA_SVG = (
    '<svg viewBox="0 0 24 24">'
    '<path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15'
    "-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475"
    "-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52"
    ".149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207"
    '-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372'
    "-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2"
    ' 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118'
    '.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413z"/>'
    '<path d="M12 0C5.373 0 0 5.373 0 12c0 2.124.558 4.118 1.528 5.845L0 24l6.335-1.505'
    "C8.035 23.45 9.978 24 12 24c6.627 0 12-5.373 12-12S18.627 0 12 0zm0 22"
    "c-1.846 0-3.574-.492-5.065-1.349l-.361-.214-3.762.893.952-3.665-.235-.374"
    'C2.497 15.64 2 13.876 2 12 2 6.477 6.477 2 12 2s10 4.477 10 10-4.477 10-10 10z"/>'
    "</svg>"
)

_FORM_SVG = (
    '<svg viewBox="0 0 24 24">'
    '<path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2z'
    "m0 4l-8 5-8-5V6l8 5 8-5v2z\"/>"
    "</svg>"
)

_CHECK_SVG = (
    '<svg viewBox="0 0 24 24">'
    '<path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/>'
    "</svg>"
)

_SEND_SVG = (
    '<svg viewBox="0 0 24 24" width="20" height="20">'
    '<path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>'
    "</svg>"
)

# Shared CSS injected into every widget (namespaced to avoid host-page conflicts)
_WIDGET_CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

#lpw-root,#lpw-root *,#lpw-root *::before,#lpw-root *::after{
  box-sizing:border-box;margin:0;padding:0;
  font-family:'Inter',system-ui,-apple-system,sans-serif;
  line-height:normal;letter-spacing:normal;text-transform:none;
  text-decoration:none;font-style:normal;border:0 none;
  vertical-align:baseline;text-align:left;white-space:normal;
  -webkit-font-smoothing:antialiased;
}
#lpw-root{
  position:fixed;z-index:2147483647;
  display:flex;flex-direction:column;gap:10px;
  font-size:14px;color:#0f172a;
}

/* FAB shared */
.lpw-fab{
  width:58px;height:58px;border-radius:50%;border:none;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:transform .22s cubic-bezier(.34,1.56,.64,1),box-shadow .22s;
  position:relative;flex-shrink:0;padding:0;
}
.lpw-fab:hover{transform:scale(1.1) translateY(-3px)}
.lpw-fab:active{transform:scale(.94)}
.lpw-fab svg{width:28px;height:28px;fill:#fff;position:relative;z-index:1}

/* Pulse rings */
.lpw-ring{
  position:absolute;inset:-10px;border-radius:50%;
  animation:lpw-pulse 2.6s ease-out infinite;pointer-events:none;
}
.lpw-ring2{animation-delay:1.3s}
@keyframes lpw-pulse{
  0%{transform:scale(.8);opacity:.7}
  70%{transform:scale(1.5);opacity:0}
  100%{opacity:0}
}

/* WhatsApp FAB */
.lpw-wa-fab{
  background:linear-gradient(135deg,#25D366 0%,#128C5E 100%);
  box-shadow:0 8px 28px rgba(37,211,102,.45);
}
.lpw-wa-fab .lpw-ring{background:rgba(37,211,102,.2)}
.lpw-wa-fab:hover{box-shadow:0 14px 40px rgba(37,211,102,.6)}

/* Form FAB */
.lpw-form-fab{
  border-radius:16px !important;
  background:linear-gradient(135deg,#6366f1 0%,#4f46e5 100%);
  box-shadow:0 6px 22px rgba(99,102,241,.45);
}
.lpw-form-fab .lpw-ring{border-radius:20px !important;background:rgba(99,102,241,.2)}
.lpw-form-fab:hover{box-shadow:0 12px 32px rgba(99,102,241,.6)}

/* Unread badge */
.lpw-badge{
  position:absolute;top:-3px;right:-3px;min-width:18px;height:18px;
  border-radius:999px;background:#ef4444;border:2px solid #fff;
  display:flex;align-items:center;justify-content:center;
  font-size:9px;font-weight:800;color:#fff;padding:0 4px;
  animation:lpw-pop .4s cubic-bezier(.34,1.56,.64,1);
}
@keyframes lpw-pop{from{transform:scale(0)}to{transform:scale(1)}}

/* Panel */
.lpw-panel{
  width:340px;max-width:calc(100vw - 28px);
  border-radius:20px;overflow:hidden;background:#fff;
  box-shadow:0 20px 70px rgba(0,0,0,.18),0 6px 20px rgba(0,0,0,.08);
  display:none;
  animation:lpw-in .3s cubic-bezier(.34,1.4,.64,1);
}
.lpw-panel.open{display:block}
@keyframes lpw-in{
  from{opacity:0;transform:translateY(12px) scale(.93)}
  to{opacity:1;transform:none}
}

/* Panel header */
.lpw-hdr{padding:20px 18px 36px;position:relative;overflow:hidden}
.lpw-hdr::after{
  content:'';position:absolute;bottom:-1px;left:0;right:0;
  height:28px;background:#fff;border-radius:28px 28px 0 0;
}
.lpw-hdr.green{background:linear-gradient(135deg,#25D366 0%,#128C5E 100%)}
.lpw-hdr.purple{background:linear-gradient(135deg,#6366f1 0%,#4f46e5 100%)}
.lpw-hdr-row{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:8px}
.lpw-hdr-info{display:flex;align-items:center;gap:10px}
.lpw-avatar{
  width:42px;height:42px;border-radius:13px;
  background:rgba(255,255,255,.25);backdrop-filter:blur(8px);
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
}
.lpw-avatar svg{width:22px;height:22px;fill:#fff}
.lpw-hdr-name{color:#fff;font-size:15px;font-weight:800;letter-spacing:-.3px;line-height:1.2}
.lpw-hdr-sub{color:rgba(255,255,255,.8);font-size:11px;margin-top:2px}
.lpw-online{
  display:inline-flex;align-items:center;gap:5px;
  background:rgba(255,255,255,.18);padding:4px 10px;border-radius:999px;margin-top:8px;
}
.lpw-dot{
  width:6px;height:6px;border-radius:50%;background:#4ade80;
  animation:lpw-blink 2s ease-in-out infinite;display:inline-block;
}
@keyframes lpw-blink{0%,100%{opacity:1}50%{opacity:.4}}
.lpw-online span{font-size:11px;font-weight:600;color:rgba(255,255,255,.9)}

/* Close button */
.lpw-close{
  background:rgba(255,255,255,.2);border:none;border-radius:50%;
  width:30px;height:30px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  color:#fff;transition:background .15s;padding:0;flex-shrink:0;
}
.lpw-close:hover{background:rgba(255,255,255,.35)}
.lpw-close svg{width:11px;height:11px}

/* Panel body */
.lpw-body{padding:10px 16px 18px;background:#fff}
.lpw-g{margin-bottom:10px}
.lpw-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.lpw-lbl{
  display:block;font-size:10px;font-weight:700;color:#94a3b8;
  text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px;
}
.lpw-inp{
  width:100% !important;padding:10px 12px !important;
  border:1.5px solid #eef0f6 !important;border-radius:12px !important;
  font-size:13px !important;font-weight:500 !important;
  color:#0f172a !important;background:#f7f8fc !important;
  outline:none !important;transition:border-color .15s,box-shadow .15s;
  -webkit-appearance:none !important;appearance:none !important;
  font-family:'Inter',system-ui,sans-serif !important;
  display:block;box-shadow:none !important;margin:0;
}
.lpw-inp:focus{
  border-color:#6366f1 !important;background:#fff !important;
  box-shadow:0 0 0 4px rgba(99,102,241,.12) !important;
}
.lpw-inp::placeholder{color:#c8d0de !important;font-weight:400 !important;opacity:1}
textarea.lpw-inp{resize:none !important;height:68px !important;line-height:1.55;cursor:text}
select.lpw-inp{
  cursor:pointer !important;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='11' height='7'%3E%3Cpath d='M.5.5l5 5 5-5' stroke='%2394a3b8' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E") !important;
  background-repeat:no-repeat !important;
  background-position:right 12px center !important;
  padding-right:32px !important;
}

/* Submit button */
.lpw-btn{
  width:100% !important;padding:13px 16px !important;margin-top:12px;
  border:none !important;border-radius:14px !important;
  font-size:14px !important;font-weight:700 !important;
  color:#fff !important;cursor:pointer;
  transition:transform .2s,box-shadow .2s,opacity .15s;
  display:flex !important;align-items:center;justify-content:center;gap:8px;
  font-family:'Inter',system-ui,sans-serif !important;
  line-height:1.4;text-transform:none;
}
.lpw-btn.green{
  background:linear-gradient(135deg,#25D366 0%,#128C5E 100%) !important;
  box-shadow:0 6px 20px rgba(37,211,102,.35);
}
.lpw-btn.green:hover{transform:translateY(-2px);box-shadow:0 10px 28px rgba(37,211,102,.5)}
.lpw-btn.purple{
  background:linear-gradient(135deg,#6366f1 0%,#4f46e5 100%) !important;
  box-shadow:0 6px 20px rgba(99,102,241,.35);
}
.lpw-btn.purple:hover{transform:translateY(-2px);box-shadow:0 10px 28px rgba(99,102,241,.5)}
.lpw-btn:active{transform:scale(.98) !important}
.lpw-btn:disabled{opacity:.5 !important;cursor:not-allowed !important;transform:none !important}
.lpw-btn svg{width:16px;height:16px;fill:#fff}

/* Privacy note */
.lpw-note{
  text-align:center;margin-top:10px;font-size:10.5px;
  color:#b0bac8;display:flex;align-items:center;justify-content:center;gap:4px;
}

/* Success screen */
.lpw-success{text-align:center;padding:32px 20px 28px;background:#fff;display:none}
.lpw-tick{
  width:60px;height:60px;border-radius:50%;
  background:linear-gradient(135deg,#22c55e,#16a34a);
  display:flex;align-items:center;justify-content:center;
  margin:0 auto 16px;box-shadow:0 8px 28px rgba(34,197,94,.35);
  animation:lpw-pop .4s cubic-bezier(.34,1.56,.64,1);
}
.lpw-tick svg{width:30px;height:30px;fill:#fff}
.lpw-success h4{font-size:17px;font-weight:800;color:#0f172a;margin-bottom:6px;letter-spacing:-.2px}
.lpw-success p{font-size:13px;color:#64748b;line-height:1.65}

/* WA panel body */
.lpw-wa-body{padding:28px 20px;text-align:center;background:#fff}
.lpw-wa-icon{
  width:64px;height:64px;border-radius:50%;
  background:linear-gradient(135deg,#25D366,#128C5E);
  display:flex;align-items:center;justify-content:center;
  margin:0 auto 14px;box-shadow:0 4px 16px rgba(37,211,102,.3);
}
.lpw-wa-icon svg{width:32px;height:32px;fill:#fff}
.lpw-wa-body h4{font-size:16px;font-weight:700;color:#0f172a;margin-bottom:6px}
.lpw-wa-body p{font-size:13px;color:#64748b;margin-bottom:20px;line-height:1.5}

@media(max-width:480px){
  .lpw-panel{width:calc(100vw - 20px) !important}
  .lpw-row{grid-template-columns:1fr}
}
"""

_CLOSE_ICON = (
    '<svg width="11" height="11" viewBox="0 0 11 11" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round">'
    "<path d=\"M1 1l9 9M10 1L1 10\"/>"
    "</svg>"
)

_USER_SVG = (
    '<svg viewBox="0 0 24 24">'
    '<path d="M12 12c2.7 0 4.8-2.1 4.8-4.8S14.7 2.4 12 2.4 7.2 4.5 7.2 7.2 9.3 12 12 12z'
    "m0 2.4c-3.2 0-9.6 1.6-9.6 4.8v2.4h19.2v-2.4c0-3.2-6.4-4.8-9.6-4.8z\"/>"
    "</svg>"
)


def _js_escape(s: str) -> str:
    """Escape a string for safe embedding inside a JS single-quoted string."""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "")


def _wa_url(phone: str, company_name: str) -> str:
    """Build a wa.me URL with a pre-filled greeting."""
    import urllib.parse
    msg = f"Hi! I am interested in {company_name}."
    return f"https://wa.me/{phone}?text={urllib.parse.quote(msg)}"


# ══════════════════════════════════════════════════════════════════════════════
# WIDGET BUILDERS — each returns a complete, self-contained IIFE JS string
# ══════════════════════════════════════════════════════════════════════════════

def _build_whatsapp_widget(
    company_slug: str,
    company_name: str,
    wa_phone: str,
    api_base: str,
    direction: str = "ltr",
) -> str:
    """
    WhatsApp-only floating button widget.
    Clicking the FAB opens WhatsApp directly (no popup).
    Also fires a tracking POST to the API if visitor phone is available.
    """
    is_left = direction == "rtl"
    side = "left:20px" if is_left else "right:20px"
    wa_href = _wa_url(wa_phone or "14155238886", company_name)
    slug_safe = _js_escape(company_slug)
    api_safe  = _js_escape(api_base)

    root_css = (
        f"position:fixed;bottom:24px;{side};z-index:2147483647;"
        "display:flex;flex-direction:column;"
        f"align-items:{'flex-start' if is_left else 'flex-end'};"
    )

    css = _WIDGET_CSS + f"""
#lpw-wa-root{{
  {root_css}
}}
"""

    return f"""
(function(){{
  'use strict';
  if(document.getElementById('lpw-wa-root'))return;

  var st=document.createElement('style');
  st.textContent={json.dumps(css)};
  document.head.appendChild(st);

  var root=document.createElement('div');
  root.id='lpw-wa-root';
  root.style.cssText={json.dumps(root_css)};
  root.innerHTML=
    '<button class="lpw-fab lpw-wa-fab" id="lpw-wa-fab" aria-label="Chat on WhatsApp">'
    +'<span class="lpw-ring" style="background:rgba(37,211,102,.18)"></span>'
    +'<span class="lpw-ring lpw-ring2" style="background:rgba(37,211,102,.18)"></span>'
    +{json.dumps(_WA_SVG)}
    +'<span class="lpw-badge" id="lpw-wa-bdg">1</span>'
    +'</button>';
  document.body.appendChild(root);

  document.getElementById('lpw-wa-fab').addEventListener('click',function(){{
    var bdg=document.getElementById('lpw-wa-bdg');
    if(bdg)bdg.style.display='none';
    // Fire tracking (best-effort)
    try{{
      fetch({json.dumps(api_base)}+'/api/v1/capture/{slug_safe}/whatsapp-click',{{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{phone:'',message:'WhatsApp widget click',page_url:window.location.href}})
      }}).catch(function(){{}});
    }}catch(e){{}}
    window.open({json.dumps(wa_href)},'_blank','noopener,noreferrer');
  }});
}})();
""".strip()


def _build_form_widget(
    company_slug: str,
    company_name: str,
    api_base: str,
    direction: str = "ltr",
    courses: str = "Admissions,Live classes,Online program,Pricing,Other",
    offset: int = 24,
) -> str:
    """Lead enquiry form floating widget — form-only."""
    is_left = direction == "rtl"
    side = "left:20px" if is_left else "right:20px"
    slug_safe = _js_escape(company_slug)
    api_safe  = _js_escape(api_base)
    cn_safe   = _js_escape(company_name)

    root_css = (
        f"position:fixed;bottom:{offset}px;{side};z-index:2147483646;"
        "display:flex;flex-direction:column;"
        f"align-items:{'flex-start' if is_left else 'flex-end'};"
    )

    opts = "".join(
        f'<option value="{c.strip()}">{c.strip()}</option>'
        for c in courses.split(",") if c.strip()
    )

    css = _WIDGET_CSS + f"#lpw-form-root{{{root_css}}}"

    send_icon = _SEND_SVG
    form_icon = _FORM_SVG
    close_icon = _CLOSE_ICON
    user_icon = _USER_SVG
    check_icon = _CHECK_SVG
    wa_icon = _WA_SVG

    return f"""
(function(){{
  'use strict';
  if(document.getElementById('lpw-form-root'))return;

  var st=document.createElement('style');
  st.textContent={json.dumps(css)};
  document.head.appendChild(st);

  var root=document.createElement('div');
  root.id='lpw-form-root';
  root.style.cssText={json.dumps(root_css)};
  root.innerHTML=
    '<div class="lpw-panel" id="lpw-fp">'
    +'<div class="lpw-hdr purple">'
    +'<div class="lpw-hdr-row">'
    +'<div class="lpw-hdr-info">'
    +'<div class="lpw-avatar">' + {json.dumps(user_icon)} + '</div>'
    +'<div><div class="lpw-hdr-name">Get in touch</div>'
    +'<div class="lpw-hdr-sub">We reply on WhatsApp within minutes</div></div>'
    +'</div>'
    +'<button class="lpw-close" id="lpw-fc" aria-label="Close">' + {json.dumps(close_icon)} + '</button>'
    +'</div>'
    +'<div class="lpw-online"><span class="lpw-dot"></span><span>Online now</span></div>'
    +'</div>'
    +'<div class="lpw-body">'
    +'<div id="lpw-ff">'
    +'<div class="lpw-row">'
    +'<div class="lpw-g"><label class="lpw-lbl">Name *</label>'
    +'<input id="lpw-fn" class="lpw-inp" type="text" placeholder="Your full name" autocomplete="name"></div>'
    +'<div class="lpw-g"><label class="lpw-lbl">WhatsApp *</label>'
    +'<input id="lpw-fp2" class="lpw-inp" type="tel" placeholder="+91 98765 43210" autocomplete="tel"></div>'
    +'</div>'
    +'<div class="lpw-g"><label class="lpw-lbl">Email</label>'
    +'<input id="lpw-fe" class="lpw-inp" type="email" placeholder="you@email.com" autocomplete="email"></div>'
    +'<div class="lpw-g"><label class="lpw-lbl">Interested in</label>'
    +'<select id="lpw-fc2" class="lpw-inp">'
    +'<option value="">Choose a course\u2026</option>'
    +{json.dumps(opts)}
    +'</select></div>'
    +'<div class="lpw-g"><label class="lpw-lbl">Message</label>'
    +'<textarea id="lpw-fm" class="lpw-inp" placeholder="Tell us what you\u2019re looking for\u2026"></textarea></div>'
    +'<button id="lpw-fsub" class="lpw-btn purple">'
    + {json.dumps(send_icon)} + ' Send Enquiry'
    +'</button>'
    +'<div class="lpw-note">🔒 Your details are safe with us</div>'
    +'</div>'
    +'<div class="lpw-success" id="lpw-fok">'
    +'<div class="lpw-tick">' + {json.dumps(check_icon)} + '</div>'
    +'<h4>Enquiry Sent! 🎉</h4>'
    +'<p>Our counsellor will reach out to you<br>on WhatsApp within a few minutes.</p>'
    +'</div>'
    +'</div>'
    +'</div>'
    +'<button class="lpw-fab lpw-form-fab" id="lpw-ffab" aria-label="Open enquiry form">'
    +'<span class="lpw-ring" style="background:rgba(99,102,241,.2)"></span>'
    + {json.dumps(form_icon)}
    +'</button>';
  document.body.appendChild(root);

  var panel=document.getElementById('lpw-fp');
  var fab=document.getElementById('lpw-ffab');
  var closeBtn=document.getElementById('lpw-fc');
  var subBtn=document.getElementById('lpw-fsub');
  var formDiv=document.getElementById('lpw-ff');
  var okDiv=document.getElementById('lpw-fok');

  fab.addEventListener('click',function(e){{e.stopPropagation();panel.classList.toggle('open');}});
  closeBtn.addEventListener('click',function(e){{e.stopPropagation();panel.classList.remove('open');}});
  panel.addEventListener('click',function(e){{e.stopPropagation();}});
  panel.addEventListener('mousedown',function(e){{e.stopPropagation();}});
  document.addEventListener('click',function(){{panel.classList.remove('open');}});

  subBtn.addEventListener('click',function(){{
    var name=document.getElementById('lpw-fn').value.trim();
    var phone=document.getElementById('lpw-fp2').value.trim();
    if(!name||!phone){{alert('Please enter your name and WhatsApp number.');return;}}
    subBtn.disabled=true;
    subBtn.innerHTML='\u23F3 Sending\u2026';
    fetch({json.dumps(api_base)}+'/api/v1/capture/{slug_safe}/form',{{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{
        name:name,phone:phone,
        email:document.getElementById('lpw-fe').value.trim(),
        course:document.getElementById('lpw-fc2').value,
        message:document.getElementById('lpw-fm').value.trim()
      }})
    }})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(d.ok){{formDiv.style.display='none';okDiv.style.display='block';}}
      else{{subBtn.disabled=false;subBtn.innerHTML={json.dumps(send_icon + ' Send Enquiry')};alert('Something went wrong.');}}
    }})
    .catch(function(){{subBtn.disabled=false;subBtn.innerHTML={json.dumps(send_icon + ' Send Enquiry')};alert('Network error. Please retry.');}});
  }});
}})();
""".strip()


def _build_combined_widget(
    company_slug: str,
    company_name: str,
    api_base: str,
    wa_phone: str,
    direction: str = "ltr",
    courses: str = "Admissions,Live classes,Online program,Pricing,Other",
) -> str:
    """
    Combined widget — WhatsApp FAB (bottom) + Form FAB (above it).
    Each FAB opens its own panel independently.
    """
    is_left = direction == "rtl"
    side = "left:20px" if is_left else "right:20px"
    slug_safe = _js_escape(company_slug)
    wa_href = _wa_url(wa_phone or "14155238886", company_name)

    root_css = (
        f"position:fixed;bottom:24px;{side};z-index:2147483646;"
        "display:flex;flex-direction:column;"
        f"align-items:{'flex-start' if is_left else 'flex-end'};gap:10px;"
    )

    opts = "".join(
        f'<option value="{c.strip()}">{c.strip()}</option>'
        for c in courses.split(",") if c.strip()
    )

    css = _WIDGET_CSS + f"#lpw-combo-root{{{root_css}}}"

    send_icon  = _SEND_SVG
    form_icon  = _FORM_SVG
    close_icon = _CLOSE_ICON
    user_icon  = _USER_SVG
    check_icon = _CHECK_SVG
    wa_icon    = _WA_SVG

    return f"""
(function(){{
  'use strict';
  if(document.getElementById('lpw-combo-root'))return;

  var st=document.createElement('style');
  st.textContent={json.dumps(css)};
  document.head.appendChild(st);

  var root=document.createElement('div');
  root.id='lpw-combo-root';
  root.style.cssText={json.dumps(root_css)};
  root.innerHTML=
    /* WhatsApp panel */
    '<div class="lpw-panel" id="lpw-wap">'
    +'<div class="lpw-hdr green">'
    +'<div class="lpw-hdr-row">'
    +'<div class="lpw-hdr-info">'
    +'<div class="lpw-avatar">' + {json.dumps(wa_icon)} + '</div>'
    +'<div><div class="lpw-hdr-name">{_js_escape(company_name)}</div>'
    +'<div class="lpw-hdr-sub">Typically replies within minutes</div></div>'
    +'</div>'
    +'<button class="lpw-close" id="lpw-wac" aria-label="Close">' + {json.dumps(close_icon)} + '</button>'
    +'</div>'
    +'<div class="lpw-online"><span class="lpw-dot"></span><span>Online now</span></div>'
    +'</div>'
    +'<div class="lpw-wa-body">'
    +'<div class="lpw-wa-icon">' + {json.dumps(wa_icon)} + '</div>'
    +'<h4>Chat with us on WhatsApp</h4>'
    +'<p>Start a conversation — we\u2019re here to help!</p>'
    +'<a href="{wa_href}" target="_blank" rel="noopener noreferrer" style="text-decoration:none" id="lpw-wa-link">'
    +'<button class="lpw-btn green">' + {json.dumps(wa_icon)} + ' Open WhatsApp</button>'
    +'</a>'
    +'</div>'
    +'</div>'
    /* Form panel */
    +'<div class="lpw-panel" id="lpw-fop">'
    +'<div class="lpw-hdr purple">'
    +'<div class="lpw-hdr-row">'
    +'<div class="lpw-hdr-info">'
    +'<div class="lpw-avatar">' + {json.dumps(user_icon)} + '</div>'
    +'<div><div class="lpw-hdr-name">Get in touch</div>'
    +'<div class="lpw-hdr-sub">We reply on WhatsApp within minutes</div></div>'
    +'</div>'
    +'<button class="lpw-close" id="lpw-foc" aria-label="Close">' + {json.dumps(close_icon)} + '</button>'
    +'</div>'
    +'<div class="lpw-online"><span class="lpw-dot"></span><span>Online now</span></div>'
    +'</div>'
    +'<div class="lpw-body">'
    +'<div id="lpw-cff">'
    +'<div class="lpw-row">'
    +'<div class="lpw-g"><label class="lpw-lbl">Name *</label>'
    +'<input id="lpw-cn" class="lpw-inp" type="text" placeholder="Your full name" autocomplete="name"></div>'
    +'<div class="lpw-g"><label class="lpw-lbl">WhatsApp *</label>'
    +'<input id="lpw-cp" class="lpw-inp" type="tel" placeholder="+91 98765 43210" autocomplete="tel"></div>'
    +'</div>'
    +'<div class="lpw-g"><label class="lpw-lbl">Email</label>'
    +'<input id="lpw-ce" class="lpw-inp" type="email" placeholder="you@email.com" autocomplete="email"></div>'
    +'<div class="lpw-g"><label class="lpw-lbl">Interested in</label>'
    +'<select id="lpw-cc" class="lpw-inp">'
    +'<option value="">Choose a course\u2026</option>'
    +{json.dumps(opts)}
    +'</select></div>'
    +'<div class="lpw-g"><label class="lpw-lbl">Message</label>'
    +'<textarea id="lpw-cm" class="lpw-inp" placeholder="Tell us what you\u2019re looking for\u2026"></textarea></div>'
    +'<button id="lpw-csub" class="lpw-btn purple">'
    + {json.dumps(send_icon)} + ' Send Enquiry'
    +'</button>'
    +'<div class="lpw-note">🔒 Your details are safe with us</div>'
    +'</div>'
    +'<div class="lpw-success" id="lpw-cok">'
    +'<div class="lpw-tick">' + {json.dumps(check_icon)} + '</div>'
    +'<h4>Enquiry Sent! 🎉</h4>'
    +'<p>Our counsellor will reach out to you<br>on WhatsApp within a few minutes.</p>'
    +'</div>'
    +'</div>'
    +'</div>'
    /* Form FAB */
    +'<button class="lpw-fab lpw-form-fab" id="lpw-cfab" aria-label="Open enquiry form">'
    +'<span class="lpw-ring" style="background:rgba(99,102,241,.2)"></span>'
    + {json.dumps(form_icon)}
    +'</button>'
    /* WhatsApp FAB */
    +'<button class="lpw-fab lpw-wa-fab" id="lpw-wfab" aria-label="Chat on WhatsApp">'
    +'<span class="lpw-ring" style="background:rgba(37,211,102,.18)"></span>'
    +'<span class="lpw-ring lpw-ring2" style="background:rgba(37,211,102,.18)"></span>'
    + {json.dumps(wa_icon)}
    +'<span class="lpw-badge" id="lpw-wbdg">1</span>'
    +'</button>';
  document.body.appendChild(root);

  function closeAll(){{
    document.getElementById('lpw-wap').classList.remove('open');
    document.getElementById('lpw-fop').classList.remove('open');
  }}

  /* WhatsApp FAB */
  document.getElementById('lpw-wfab').addEventListener('click',function(e){{
    e.stopPropagation();
    var wap=document.getElementById('lpw-wap');
    var wasOpen=wap.classList.contains('open');
    closeAll();
    if(!wasOpen){{
      wap.classList.add('open');
      var bdg=document.getElementById('lpw-wbdg');
      if(bdg)bdg.style.display='none';
    }}
  }});

  /* Form FAB */
  document.getElementById('lpw-cfab').addEventListener('click',function(e){{
    e.stopPropagation();
    var fop=document.getElementById('lpw-fop');
    var wasOpen=fop.classList.contains('open');
    closeAll();
    if(!wasOpen)fop.classList.add('open');
  }});

  /* Close buttons */
  document.getElementById('lpw-wac').addEventListener('click',function(e){{e.stopPropagation();document.getElementById('lpw-wap').classList.remove('open');}});
  document.getElementById('lpw-foc').addEventListener('click',function(e){{e.stopPropagation();document.getElementById('lpw-fop').classList.remove('open');}});

  /* Close on outside click */
  document.addEventListener('click',function(){{closeAll();}});
  document.getElementById('lpw-wap').addEventListener('click',function(e){{e.stopPropagation();}});
  document.getElementById('lpw-fop').addEventListener('click',function(e){{e.stopPropagation();}});
  document.getElementById('lpw-wap').addEventListener('mousedown',function(e){{e.stopPropagation();}});
  document.getElementById('lpw-fop').addEventListener('mousedown',function(e){{e.stopPropagation();}});

  /* Track WA click */
  document.getElementById('lpw-wa-link').addEventListener('click',function(){{
    try{{
      fetch({json.dumps(api_base)}+'/api/v1/capture/{slug_safe}/whatsapp-click',{{
        method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{phone:'',message:'WhatsApp widget click',page_url:window.location.href}})
      }}).catch(function(){{}});
    }}catch(e){{}}
    setTimeout(function(){{document.getElementById('lpw-wap').classList.remove('open');}},300);
  }});

  /* Form submit */
  document.getElementById('lpw-csub').addEventListener('click',function(){{
    var name=document.getElementById('lpw-cn').value.trim();
    var phone=document.getElementById('lpw-cp').value.trim();
    if(!name||!phone){{alert('Please enter your name and WhatsApp number.');return;}}
    var btn=document.getElementById('lpw-csub');
    btn.disabled=true;btn.innerHTML='\u23F3 Sending\u2026';
    fetch({json.dumps(api_base)}+'/api/v1/capture/{slug_safe}/form',{{
      method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{
        name:name,phone:phone,
        email:document.getElementById('lpw-ce').value.trim(),
        course:document.getElementById('lpw-cc').value,
        message:document.getElementById('lpw-cm').value.trim()
      }})
    }})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(d.ok){{
        document.getElementById('lpw-cff').style.display='none';
        document.getElementById('lpw-cok').style.display='block';
      }}else{{
        btn.disabled=false;btn.innerHTML={json.dumps(send_icon + ' Send Enquiry')};
        alert('Something went wrong.');
      }}
    }})
    .catch(function(){{
      btn.disabled=false;btn.innerHTML={json.dumps(send_icon + ' Send Enquiry')};
      alert('Network error. Please retry.');
    }});
  }});
}})();
""".strip()


# ── Standalone Form Page (for iframe) ─────────────────────────────────────────

def _build_form_page(company_slug: str, company_name: str, api_base: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Enquiry — {company_name}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:'Inter',system-ui,sans-serif}}
body{{background:#f1f5f9;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:16px}}
.card{{background:#fff;border-radius:20px;padding:32px;max-width:440px;width:100%;box-shadow:0 8px 32px rgba(0,0,0,.06)}}
h2{{font-size:22px;font-weight:800;color:#1e293b;letter-spacing:-.3px}}
.sub{{font-size:13px;color:#64748b;margin:6px 0 24px;line-height:1.5}}
label{{display:block;font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.6px;margin-bottom:5px}}
input,select,textarea{{width:100%;padding:12px 14px;border:1.5px solid #eef0f6;border-radius:12px;font-size:14px;color:#1e293b;background:#f7f8fc;outline:none;transition:all .2s;font-family:inherit;margin-bottom:16px}}
input:focus,select:focus,textarea:focus{{border-color:#6366f1;background:#fff;box-shadow:0 0 0 4px rgba(99,102,241,.1)}}
textarea{{resize:none;height:80px}}
select{{appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='11' height='7'%3E%3Cpath d='M.5.5l5 5 5-5' stroke='%2394a3b8' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;padding-right:32px}}
button{{width:100%;padding:14px;background:linear-gradient(135deg,#6366f1 0%,#4f46e5 100%);color:#fff;border:none;border-radius:12px;font-size:15px;font-weight:700;cursor:pointer;transition:all .25s}}
button:hover{{box-shadow:0 4px 16px rgba(99,102,241,.35);transform:translateY(-1px)}}
.ok{{text-align:center;padding:20px 0}}
.ok .check{{width:56px;height:56px;border-radius:50%;background:linear-gradient(135deg,#25D366,#128C5E);display:flex;align-items:center;justify-content:center;margin:0 auto 14px}}
.ok .check svg{{width:28px;height:28px;fill:#fff}}
.ok h3{{font-size:18px;font-weight:800;color:#1e293b;margin-bottom:4px}}
.ok p{{font-size:13px;color:#64748b}}
</style>
</head>
<body>
<div class="card">
  <h2>Get in touch</h2>
  <p class="sub">Fill this form and our team will reach out on WhatsApp shortly.</p>
  <div id="fw">
    <label>Your name *</label><input id="fn" placeholder="Full name" required>
    <label>WhatsApp number *</label><input id="fp" placeholder="+91 98765 43210" required>
    <label>Email</label><input id="fe" type="email" placeholder="you@email.com">
    <label>Course interest</label>
    <select id="fc"><option value="">Select...</option><option>CAT Preparation</option><option>CUET</option><option>IPM / BBA</option><option>CLAT / Law</option><option>GMAT / GRE</option><option>Other</option></select>
    <label>Message</label><textarea id="fm" placeholder="Any question or requirement..."></textarea>
    <button onclick="go()">Send enquiry</button>
  </div>
  <div id="ok" class="ok" style="display:none">
    <div class="check"><svg viewBox="0 0 24 24"><path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z"/></svg></div>
    <h3>Thank you!</h3>
    <p>We\u2019ll reach out on WhatsApp soon</p>
  </div>
</div>
<script>
function go(){{
  var n=document.getElementById('fn').value.trim(),p=document.getElementById('fp').value.trim();
  if(!n||!p){{alert('Please fill name and phone.');return;}}
  fetch('{api_base}/api/v1/capture/{company_slug}/form',{{
    method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{name:n,phone:p,email:document.getElementById('fe').value,course:document.getElementById('fc').value,message:document.getElementById('fm').value}})
  }}).then(function(){{document.getElementById('fw').style.display='none';document.getElementById('ok').style.display='block';}}).catch(function(){{alert('Error. Please retry.');}});
}}
</script>
</body></html>"""
