from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict

from sqlalchemy.orm import Session

from app.agents.action_runner import run_actions
from app.agents.decision_engine import decide_reply
from app.db.models import Lead, LeadEvent, LeadNote
from app.services.vector_store import search_memory


def _memory_snippets(query: str, k: int = 3) -> list[str]:
    try:
        res = search_memory(query=query, k=k)
        docs = res.get("documents", [[]])[0] if isinstance(res, dict) else []
        return [str(d) for d in (docs or [])][:k]
    except Exception:
        return []


def build_event_context(db: Session, lead_id: int, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    lead = db.get(Lead, lead_id)
    if not lead:
        raise ValueError("Lead not found")

    notes = (
        db.query(LeadNote)
        .filter(LeadNote.lead_id == lead_id)
        .order_by(LeadNote.created_at.desc())
        .limit(10)
        .all()
    )
    last_notes = "\n\n---\n\n".join([n.note for n in reversed(notes)])

    mem_q = f"{lead.company} {lead.website or ''} {event_type}".strip()

    return {
        "now_utc": datetime.utcnow().isoformat(),
        "channel": "autopilot",
        "event_type": event_type,
        "event_payload": payload,
        "lead": {
            "id": lead.id,
            "company": lead.company,
            "website": lead.website,
            "pain": lead.pain,
            "contact_name": lead.contact_name,
            "email": lead.email,
            "phone": lead.phone,
            "status": lead.status,
            "score": lead.score,
            "last_contacted_at": lead.last_contacted_at.isoformat() if lead.last_contacted_at else None,
            "next_followup_at": lead.next_followup_at.isoformat() if lead.next_followup_at else None,
        },
        "recent_notes": [{"at": n.created_at.isoformat(), "note": n.note} for n in reversed(notes)],
        "last_notes": last_notes,
        "memory_snippets": _memory_snippets(mem_q, k=3),
        "business": {"name": "LeadPro"},
    }


def handle_event(db: Session, event: LeadEvent) -> Dict[str, Any]:
    try:
        payload = json.loads(event.payload_json or "{}")
    except Exception:
        payload = {"_raw": event.payload_json}

    ctx = build_event_context(db=db, lead_id=event.lead_id, event_type=event.type, payload=payload)
    decision = decide_reply(context=ctx, db=db, lead_id=event.lead_id, event_id=event.id)
    result = run_actions(db=db, lead_id=event.lead_id, actions=decision.actions)

    return {
        "event_id": event.id,
        "decision": {"summary": decision.summary, "confidence": decision.confidence, "actions": decision.actions},
        "executed": result.executed,
        "skipped": result.skipped,
    }
