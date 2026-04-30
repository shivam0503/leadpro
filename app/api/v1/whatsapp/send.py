"""
WhatsApp outbound send — versioned endpoint (/api/v1/whatsapp/send).

NOTE: The actual implementation lives in crm.py to avoid duplication.
This module re-exports the router from crm.py so routes.py stays working.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models import Lead, LeadNote
from app.schemas.lead import WhatsAppSendRequest
from app.services.database import get_db
from app.services.whatsapp import send_text

router = APIRouter(tags=["WhatsApp"])


@router.post("/whatsapp/send", summary="Send WhatsApp message to a lead (outbound API)")
def whatsapp_send(payload: WhatsAppSendRequest, db: Session = Depends(get_db)):
    """
    Send a WhatsApp message to a lead via the outbound WhatsApp Cloud API.

    - Respects WHATSAPP_DEMO_MODE from .env (true = no real API call)
    - Writes a CRM note and updates follow-up timestamps
    - Updates lead status to 'contacted' if currently 'new'
    """
    lead = db.get(Lead, payload.lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    if not lead.phone:
        raise HTTPException(status_code=400, detail="Lead has no phone number")

    msg = (payload.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message is required")

    now = datetime.utcnow()
    res = send_text(to_phone=lead.phone, message=msg)

    note = LeadNote(
        lead_id=lead.id,
        note=f"[WhatsApp Sent - {res.mode.upper()}]\n\n{msg}",
        created_at=now,
    )
    db.add(note)

    lead.last_contacted_at = now
    lead.next_followup_at = now + timedelta(days=int(payload.next_followup_in_days or 2))
    lead.updated_at = now
    if lead.status == "new":
        lead.status = "contacted"

    db.add(lead)
    db.commit()
    db.refresh(note)
    db.refresh(lead)

    if not res.ok:
        raise HTTPException(status_code=502, detail=f"WhatsApp send failed: {res.error}")

    return {
        "ok": True,
        "mode": res.mode,
        "lead_id": lead.id,
        "note_id": note.id,
        "status": lead.status,
        "last_contacted_at": lead.last_contacted_at,
        "next_followup_at": lead.next_followup_at,
        "provider_response": res.response,
    }
