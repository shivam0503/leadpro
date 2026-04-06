from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.agents.base_agent import AgentInput, AgentOutput, BaseAgent
from app.db.models import User, LeadAssignment, LeadTask


def _utcnow() -> datetime:
    return datetime.utcnow()


class SalesAutomationAgent(BaseAgent):
    name = "sales_automation"

    def __init__(self, db: Session):
        self.db = db

    def _pick_sales_rep(self, region: Optional[str] = None, industry: Optional[str] = None) -> Optional[User]:
        q = self.db.query(User).filter(User.role.in_(["sales", "manager"]))  # sales+manager can own
        if region:
            q = q.filter((User.region == region) | (User.region.is_(None)))
        if industry:
            q = q.filter((User.industry == industry) | (User.industry.is_(None)))
        reps = q.all()
        if not reps:
            return None

        # pick least loaded: open tasks count
        best = None
        best_load = 10**9
        for r in reps:
            load = (
                self.db.query(LeadTask)
                .filter(LeadTask.assignee_user_id == r.id, LeadTask.status == "open")
                .count()
            )
            if load < best_load:
                best = r
                best_load = load
        return best

    def run(self, inp: AgentInput) -> AgentOutput:
        lead = inp.lead or {}
        # Next followup rules
        urgency = (lead.get("urgency") or "").lower()
        next_days = 2
        if urgency == "high":
            next_days = 0
        elif urgency == "medium":
            next_days = 1

        # Assign sales rep
        rep = self._pick_sales_rep(region=lead.get("region"), industry=lead.get("industry"))
        actions = []
        facts: Dict[str, Any] = {"next_followup_in_days": next_days}

        if rep:
            facts["assigned_to"] = {"id": rep.id, "name": rep.name, "role": rep.role}
            # create assignment (idempotent-ish)
            actions.append({"type": "add_note", "note": f"[SalesAutomation] suggested_owner={rep.name} (id={rep.id})"})
            # create a task for call/review
            actions.append({
                "type": "create_task",
                "task": {"type": "call", "title": "Follow up lead", "description": "Auto-created by SalesAutomationAgent", "due_in_days": next_days, "assignee_user_id": rep.id},
            })
        else:
            actions.append({"type": "add_note", "note": "[SalesAutomation] no sales rep configured; skipping assignment"})

        # ensure followup
        actions.append({"type": "update_lead", "next_followup_in_days": next_days})

        return AgentOutput(facts=facts, actions=actions, trace={"agent": self.name, **facts})
