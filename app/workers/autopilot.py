from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.db.models import Lead, LeadEvent
from app.services.database import SessionLocal
from app.agents.event_agent import handle_event


NO_REPLY_WINDOWS_HOURS = [48, 72, 168]  # 2d, 3d, 7d


def enqueue_no_reply_events(db: Session, limit: int = 200) -> int:
    """Create events for leads that haven't replied after last_contacted_at."""
    now = datetime.utcnow()
    created = 0

    q = db.query(Lead).filter(Lead.last_contacted_at.isnot(None))
    q = q.filter(Lead.status.in_(["contacted", "replied", "demo"]))
    q = q.order_by(Lead.last_contacted_at.asc()).limit(limit)

    leads = q.all()
    for lead in leads:
        if not lead.last_contacted_at:
            continue
        hours_since = (now - lead.last_contacted_at).total_seconds() / 3600.0

        # choose the next window passed and not already enqueued
        for h in NO_REPLY_WINDOWS_HOURS:
            if hours_since >= h:
                etype = f"lead.no_reply_{h}h"
                exists = (
                    db.query(LeadEvent)
                    .filter(and_(LeadEvent.lead_id == lead.id, LeadEvent.type == etype))
                    .first()
                )
                if exists:
                    continue
                ev = LeadEvent(
                    lead_id=lead.id,
                    type=etype,
                    payload_json=json.dumps({"hours_since": h}),
                    status="pending",
                    created_at=now,
                )
                db.add(ev)
                created += 1
                break

    if created:
        db.commit()
    return created


def enqueue_due_followup_events(db: Session, limit: int = 200) -> int:
    now = datetime.utcnow()
    created = 0
    q = db.query(Lead).filter(Lead.next_followup_at.isnot(None))
    q = q.filter(Lead.next_followup_at <= now)
    q = q.order_by(Lead.next_followup_at.asc()).limit(limit)
    for lead in q.all():
        etype = "lead.followup_due"
        exists = (
            db.query(LeadEvent)
            .filter(and_(LeadEvent.lead_id == lead.id, LeadEvent.type == etype, LeadEvent.status.in_(["pending","processing"])))
            .first()
        )
        if exists:
            continue
        ev = LeadEvent(
            lead_id=lead.id,
            type=etype,
            payload_json=json.dumps({"due_at": lead.next_followup_at.isoformat() if lead.next_followup_at else None}),
            status="pending",
            created_at=now,
        )
        db.add(ev)
        created += 1
    if created:
        db.commit()
    return created


def process_events(db: Session, limit: int = 50) -> list[dict]:
    now = datetime.utcnow()
    events = (
        db.query(LeadEvent)
        .filter(LeadEvent.status == "pending")
        .order_by(LeadEvent.created_at.asc())
        .limit(limit)
        .all()
    )
    results = []
    for ev in events:
        ev.status = "processing"
        db.add(ev)
        db.commit()
        try:
            out = handle_event(db=db, event=ev)
            ev.status = "done"
            ev.processed_at = now
            db.add(ev)
            db.commit()
            results.append({"ok": True, **out})
        except Exception as e:
            ev.status = "failed"
            ev.error = str(e)
            ev.processed_at = now
            db.add(ev)
            db.commit()
            results.append({"ok": False, "event_id": ev.id, "error": str(e)})
    return results


def run_once() -> dict:
    db = SessionLocal()
    try:
        created_no_reply = enqueue_no_reply_events(db=db)
        created_due = enqueue_due_followup_events(db=db)
        processed = process_events(db=db, limit=50)
        return {
            "created_no_reply": created_no_reply,
            "created_followup_due": created_due,
            "processed": processed,
        }
    finally:
        db.close()


if __name__ == "__main__":
    print(run_once())
