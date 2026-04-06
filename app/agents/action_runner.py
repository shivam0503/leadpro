from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Lead, LeadNote
from app.services.whatsapp import send_text


@dataclass
class ActionResult:
    executed: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


ALLOWED_STATUSES = {"new", "contacted", "replied", "demo", "closed", "lost"}


def _utcnow() -> datetime:
    return datetime.utcnow()


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        v = int(value)
    except Exception:
        v = default
    return max(low, min(v, high))


def _add_note(db: Session, lead_id: int, note: str, created_at: datetime | None = None) -> int:
    n = LeadNote(
        lead_id=lead_id,
        note=(note or "").strip(),
        created_at=created_at or _utcnow(),
    )
    db.add(n)
    db.commit()
    db.refresh(n)
    return n.id


def run_actions(db: Session, lead_id: int, actions: list[dict[str, Any]]) -> ActionResult:
    lead = db.get(Lead, lead_id)
    if not lead:
        raise ValueError("Lead not found")

    executed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    now = _utcnow()

    for action in actions:
        if not isinstance(action, dict):
            skipped.append({"action": action, "reason": "not_a_dict"})
            continue

        a_type = action.get("type")

        # --- Send WhatsApp (demo or live based on settings) ---
        if a_type == "send_whatsapp":
            msg = (action.get("message") or "").strip()
            if not msg:
                skipped.append({"action": action, "reason": "empty_message"})
                continue

            if not getattr(lead, "phone", None):
                skipped.append({"action": action, "reason": "lead_missing_phone"})
                continue

            next_days = _clamp_int(action.get("next_followup_in_days"), default=2, low=0, high=30)

            # Provider call (your service decides demo vs live)
            res = send_text(to_phone=lead.phone, message=msg)

            # Build note safely (no raw newlines inside f-string literal)
            note_text = (
                f"[WhatsApp Sent - {str(res.mode).upper()}]\n\n{msg}"
                + (f"\n\n[ProviderError] {res.error}" if not getattr(res, "ok", False) else "")
            )

            note = LeadNote(
                lead_id=lead.id,
                note=note_text,
                created_at=now,
            )
            db.add(note)

            # Update lead timings (record attempt even if provider failed)
            lead.last_contacted_at = now
            lead.next_followup_at = now + timedelta(days=next_days)
            lead.updated_at = now
            if lead.status == "new":
                lead.status = "contacted"

            db.add(lead)
            db.commit()
            db.refresh(note)
            db.refresh(lead)

            if getattr(res, "ok", False):
                executed.append(
                    {
                        "type": "send_whatsapp",
                        "mode": getattr(res, "mode", "unknown"),
                        "note_id": note.id,
                        "message_preview": msg[:160],
                    }
                )
            else:
                executed.append(
                    {
                        "type": "send_whatsapp_failed",
                        "mode": getattr(res, "mode", "unknown"),
                        "note_id": note.id,
                        "error": getattr(res, "error", "unknown_error"),
                    }
                )
            continue

        # --- Add Note ---
        if a_type == "add_note":
            note_txt = (action.get("note") or "").strip()
            if not note_txt:
                skipped.append({"action": action, "reason": "empty_note"})
                continue

            note_id = _add_note(db, lead.id, note_txt, created_at=now)
            lead.updated_at = now
            db.add(lead)
            db.commit()

            executed.append({"type": "add_note", "note_id": note_id})
            continue

        # --- Update Lead ---
        if a_type == "update_lead":
            status = action.get("status")
            score = action.get("score")
            next_days = action.get("next_followup_in_days")

            if status and status in ALLOWED_STATUSES:
                lead.status = status

            if score is not None:
                try:
                    lead.score = int(score)
                except Exception:
                    skipped.append({"action": action, "reason": "invalid_score"})

            if next_days is not None:
                nd = _clamp_int(next_days, default=2, low=0, high=30)
                lead.next_followup_at = now + timedelta(days=nd)

            lead.updated_at = now
            db.add(lead)
            db.commit()
            db.refresh(lead)

            executed.append({"type": "update_lead", "status": lead.status, "score": getattr(lead, "score", None)})
            continue

        # --- Do Not Contact ---
        if a_type == "do_not_contact":
            reason = (action.get("reason") or "user_requested").strip()
            _add_note(db, lead.id, f"[DO NOT CONTACT]\nReason: {reason}", created_at=now)
            lead.status = "lost"
            lead.updated_at = now
            db.add(lead)
            db.commit()
            executed.append({"type": "do_not_contact", "status": lead.status})
            continue

        # --- Handoff Human ---
        if a_type == "handoff_human":
            reason = (action.get("reason") or "handoff").strip()
            _add_note(db, lead.id, f"[HANDOFF TO HUMAN]\nReason: {reason}", created_at=now)
            executed.append({"type": "handoff_human", "reason": reason})
            continue


        # --- Create Task ---
        if a_type == "create_task":
            task = action.get("task") or {}
            t_type = (task.get("type") or "custom").strip()
            title = (task.get("title") or "Task").strip()
            desc = (task.get("description") or "").strip() or None
            due_in = _clamp_int(task.get("due_in_days"), default=2, low=0, high=30)
            assignee_id = task.get("assignee_user_id")

            from app.db.models import LeadTask  # local import to avoid circulars
            due_at = now + timedelta(days=due_in) if due_in is not None else None

            lt = LeadTask(
                lead_id=lead.id,
                type=t_type,
                title=title,
                description=desc,
                status="open",
                due_at=due_at,
                assignee_user_id=int(assignee_id) if assignee_id is not None else None,
                created_at=now,
            )
            db.add(lt)
            db.commit()
            db.refresh(lt)
            executed.append({"type": "create_task", "task_id": lt.id})
            continue

        skipped.append({"action": action, "reason": "unknown_type"})

    return ActionResult(executed=executed, skipped=skipped)