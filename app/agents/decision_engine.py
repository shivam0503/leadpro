from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.ai.orchestrator import run_multi_agent_decision
from app.ai.trace import DecisionTrace
from app.db.models import AIDecisionTrace


@dataclass
class Decision:
    summary: str
    confidence: float
    actions: list[dict[str, Any]]
    raw: str
    trace: Optional[dict[str, Any]] = None
    trace_id: Optional[int] = None


def decide_reply(context: Dict[str, Any], *, db: Session, lead_id: int, event_id: int | None = None) -> Decision:
    """Central Decision Engine (multi-agent + filter-driven rules + transparency trace).

    - Returns actionable JSON for ActionRunner
    - Stores a DecisionTrace row for UI transparency
    """
    trace, actions = run_multi_agent_decision(db=db, ctx=context)

    # Human internal summary
    lead = (context.get("lead") or {})
    summary = f"Next best action for {lead.get('company') or 'lead'}"

    decision = Decision(
        summary=summary,
        confidence=float(trace.confidence or 0.6),
        actions=actions,
        raw=json.dumps({"trace": trace.to_public_dict(), "actions": actions}, ensure_ascii=False),
        trace=trace.to_public_dict(),
    )

    # Persist trace for UI (best-effort)
    try:
        row = AIDecisionTrace(
            lead_id=int(lead_id),
            event_id=int(event_id) if event_id is not None else None,
            trace_json=json.dumps(trace.to_public_dict(), ensure_ascii=False),
            actions_json=json.dumps(actions, ensure_ascii=False),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        decision.trace_id = row.id
    except Exception as e:
        logger.debug("Trace persist failed: {}", e)
        try:
            db.rollback()
        except Exception:
            pass

    return decision
