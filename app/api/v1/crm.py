"""
api/v1/crm.py — Company-scoped CRM
────────────────────────────────────
All endpoints automatically scoped to the requesting company's DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.company_models import Lead, LeadNote
from app.services.database import get_db

router = APIRouter(tags=["CRM"])

LEAD_STATUSES = {"new", "contacted", "replied", "demo", "closed", "lost"}


# ── Schemas ───────────────────────────────────────────────────────────────────

class LeadCreate(BaseModel):
    company: str = Field(..., min_length=2)
    website: str | None = None
    pain: str | None = None
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    source: str | None = "manual"


class LeadUpdateStatus(BaseModel):
    status: str
    score: int | None = None
    next_followup_at: datetime | None = None
    last_contacted_at: datetime | None = None


class LeadNoteCreate(BaseModel):
    note: str = Field(..., min_length=1)


class FollowupComplete(BaseModel):
    note: str = Field(..., min_length=1)
    next_followup_in_days: int = Field(default=2, ge=0, le=30)
    new_status: str | None = None


class ReplyRequest(BaseModel):
    """Schema for manual reply from agent"""
    message: str = Field(..., min_length=1, max_length=2000)


# ── Lead CRUD ──────────────────────────────────────────────────────────────────

@router.post("/leads", status_code=201)
def create_lead(
    payload: LeadCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    lead = Lead(
        company=payload.company,
        website=payload.website,
        pain=payload.pain,
        contact_name=payload.contact_name,
        email=payload.email,
        phone=payload.phone,
        source=payload.source or "manual",
        status="new",
        score=0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)

    # Trigger WhatsApp AI welcome (non-blocking)
    company_slug = getattr(user, "company_slug", None)
    if company_slug and lead.phone:
        try:
            _trigger_whatsapp_welcome(db=db, lead=lead, company_slug=company_slug)
        except Exception:
            pass

    return lead


@router.get("/leads")
def list_leads(
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    query = db.query(Lead)
    if status:
        if status not in LEAD_STATUSES:
            raise HTTPException(400, f"Invalid status. Use: {sorted(LEAD_STATUSES)}")
        query = query.filter(Lead.status == status)
    if q:
        pattern = f"%{q}%"
        query = query.filter(
            or_(
                Lead.company.ilike(pattern),
                Lead.contact_name.ilike(pattern),
                Lead.email.ilike(pattern),
                Lead.phone.ilike(pattern),
            )
        )

    total = query.count()
    leads = query.order_by(Lead.created_at.desc()).offset(offset).limit(limit).all()

    return {"total": total, "offset": offset, "limit": limit, "leads": leads}


@router.get("/leads/{lead_id}")
def get_lead(lead_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    return lead


@router.patch("/leads/{lead_id}/status")
def update_lead_status(
    lead_id: int,
    payload: LeadUpdateStatus,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    if payload.status not in LEAD_STATUSES:
        raise HTTPException(400, f"Invalid status. Use: {sorted(LEAD_STATUSES)}")

    lead.status = payload.status
    if payload.score is not None:
        lead.score = int(payload.score)
    if payload.next_followup_at is not None:
        lead.next_followup_at = payload.next_followup_at
    if payload.last_contacted_at is not None:
        lead.last_contacted_at = payload.last_contacted_at
    lead.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(lead)
    return lead


@router.delete("/leads/{lead_id}")
def delete_lead(lead_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    db.delete(lead)
    db.commit()
    return {"ok": True, "deleted_id": lead_id}


# ── Notes ─────────────────────────────────────────────────────────────────────

@router.post("/leads/{lead_id}/notes")
def add_note(lead_id: int, payload: LeadNoteCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    note = LeadNote(lead_id=lead_id, note=payload.note.strip(), created_at=datetime.utcnow())
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


@router.get("/leads/{lead_id}/notes")
def list_notes(lead_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")
    return db.query(LeadNote).filter(LeadNote.lead_id == lead_id).order_by(LeadNote.created_at.desc()).all()


# ── Manual WhatsApp Reply (NEW ENDPOINT) ─────────────────────────────────────

@router.post("/leads/{lead_id}/reply")
def manual_reply_to_lead(
    lead_id: int,
    payload: ReplyRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """
    Send manual WhatsApp reply to lead from CRM dashboard.
    This sends the message directly to the lead's phone number.
    """
    from app.services.whatsapp import send_text

    # Get the lead
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    # Check if lead has phone number
    if not lead.phone:
        raise HTTPException(400, "Lead has no phone number to send reply")

    # Send WhatsApp message
    result = send_text(to_phone=lead.phone, message=payload.message)

    if not result.ok:
        raise HTTPException(400, f"Failed to send WhatsApp: {result.error}")

    # Save the reply as a note in CRM
    note = LeadNote(
        lead_id=lead.id,
        note=f"[Agent Manual Reply - Sent]\n\n{payload.message}",
        created_at=datetime.utcnow()
    )
    db.add(note)

    # Update lead status and last contacted
    lead.last_contacted_at = datetime.utcnow()
    if lead.status == "new":
        lead.status = "contacted"
    lead.updated_at = datetime.utcnow()

    db.commit()

    return {
        "success": True,
        "message": "Reply sent successfully",
        "lead_id": lead.id,
        "phone": lead.phone,
        "sent_at": datetime.utcnow().isoformat()
    }


# ── Conversation History (NEW ENDPOINT) ─────────────────────────────────────

@router.get("/leads/{lead_id}/conversation")
def get_lead_conversation(
    lead_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """
    Get full conversation history for a lead.
    Returns all notes and messages in chronological order.
    """
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    notes = db.query(LeadNote).filter(
        LeadNote.lead_id == lead_id
    ).order_by(LeadNote.created_at.asc()).limit(limit).all()

    return {
        "lead_id": lead.id,
        "lead_name": lead.contact_name,
        "lead_phone": lead.phone,
        "conversation": [
            {
                "id": note.id,
                "message": note.note,
                "timestamp": note.created_at.isoformat(),
                "type": "note"
            }
            for note in notes
        ]
    }


# ── Follow-ups ─────────────────────────────────────────────────────────────────

@router.get("/followups/due")
def followups_due(
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default="contacted"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    now = datetime.utcnow()
    q = db.query(Lead).filter(Lead.next_followup_at.isnot(None), Lead.next_followup_at <= now)
    if status:
        q = q.filter(Lead.status == status)
    leads = q.order_by(Lead.next_followup_at.asc()).limit(limit).all()
    return {"count": len(leads), "now_utc": now, "leads": leads}


@router.post("/followups/{lead_id}/complete")
def complete_followup(
    lead_id: int,
    payload: FollowupComplete,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    now = datetime.utcnow()
    note = LeadNote(lead_id=lead.id, note=f"[Follow-up Completed]\n{payload.note.strip()}", created_at=now)
    db.add(note)

    lead.last_contacted_at = now
    lead.next_followup_at = now + timedelta(days=int(payload.next_followup_in_days))
    lead.updated_at = now

    if payload.new_status:
        if payload.new_status not in LEAD_STATUSES:
            raise HTTPException(400, f"Invalid status. Use: {sorted(LEAD_STATUSES)}")
        lead.status = payload.new_status
    elif lead.status == "new":
        lead.status = "contacted"

    db.commit()
    db.refresh(lead)
    return {"ok": True, "lead": lead}


# ── Dashboard KPIs ─────────────────────────────────────────────────────────────

@router.get("/dashboard/kpis")
def dashboard_kpis(db: Session = Depends(get_db), user=Depends(get_current_user)):
    total = db.query(func.count(Lead.id)).scalar() or 0
    hot = db.query(func.count(Lead.id)).filter(Lead.score >= 40).scalar() or 0
    closed = db.query(func.count(Lead.id)).filter(Lead.status == "closed").scalar() or 0
    conversion = round((closed / total * 100.0), 2) if total else 0.0
    return {
        "total_leads": int(total),
        "hot_leads": int(hot),
        "conversion_pct": conversion,
    }


# ── WhatsApp AI welcome trigger ─────────────────────────────────────────────────

def _trigger_whatsapp_welcome(db: Session, lead: Lead, company_slug: str):
    """Send AI welcome WhatsApp using company knowledge base + persona."""
    from app.services.scraper import search_knowledge
    from app.core.config import settings
    from openai import OpenAI

    # Fetch KB context
    query = f"{lead.company} {lead.pain or ''} fee price course"
    kb_snippets = search_knowledge(query=query, company_slug=company_slug, k=4)
    kb_context = "\n\n".join(kb_snippets) if kb_snippets else ""

    # Load company AI persona
    try:
        from app.services.database import get_master_db
        from app.db.master_models import Company as CompanyModel
        master_db = next(get_master_db())
        co = master_db.query(CompanyModel).filter(CompanyModel.slug == company_slug).first()
        ai_persona = (co.ai_persona or "").strip() if co else ""
    except Exception:
        ai_persona = ""

    system_prompt = ai_persona if ai_persona else "You are a helpful AI counsellor. Be warm, specific, and brief."

    user_prompt = f"""KNOWLEDGE BASE:
{kb_context or "No KB loaded yet."}

STUDENT INFO:
- Name: {lead.company}
- Interest: {lead.pain or "Not specified"}
- Phone: {lead.phone}

Write a warm WhatsApp welcome message (max 3 sentences, Hinglish ok).
If fees or course details are in KB above, mention ONE relevant option.
End with CTA: call 8130-038-836 or WhatsApp 9267-989-969.
Never be generic.""".strip()

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=200,
    )
    message = response.choices[0].message.content.strip()

    from app.services.whatsapp import send_text
    send_text(to_phone=lead.phone, message=message)
