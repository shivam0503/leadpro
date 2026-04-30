from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.agents.base_agent import AgentInput, AgentOutput, BaseAgent


class LeadQualificationAgent(BaseAgent):
    name = "lead_qualification"

    def run(self, inp: AgentInput) -> AgentOutput:
        lead = inp.lead or {}
        # Inputs that might exist (safe defaults)
        engagement = int(lead.get("engagement") or 0)
        base_score = int(lead.get("score") or 0)

        # derive intent & budget if present in prior extraction (stored in lead dict by caller sometimes)
        # but in our pipeline, the orchestrator merges facts; so we keep it simple.
        # use last note length as a proxy engagement too
        notes = inp.recent_notes or []
        msg_len = len((inp.inbound_text or "").strip())

        score = 0
        score += min(30, max(0, base_score))
        score += min(20, engagement)
        score += 40 if (lead.get("website") or "").strip() else 0
        score += 30 if (lead.get("email") or "").strip() else 0
        score += 10 if msg_len > 12 else 0
        score += 5 if msg_len > 80 else 0

        # simple hot/warm/cold
        bucket = "cold"
        if score >= 40:
            bucket = "hot"
        elif score >= 30:
            bucket = "warm"

        tags = []
        if bucket == "hot":
            tags.append("hot")
        if engagement >= 10:
            tags.append("engaged")
        if (lead.get("source") or "").lower() in {"meta", "google", "website"}:
            tags.append("paid")
        if not lead.get("phone"):
            tags.append("missing_phone")

        actions = [
            {"type": "update_lead", "score": int(max(0, min(score, 100))), "next_followup_in_days": 2}
        ]
        actions.append({"type": "add_note", "note": f"[LeadQualification] bucket={bucket} score={score} tags={','.join(tags) or '-'}"})

        return AgentOutput(
            facts={"lead_score": int(max(0, min(score, 100))), "lead_bucket": bucket, "tags": tags},
            actions=actions,
            trace={"agent": self.name, "bucket": bucket, "score": score, "tags": tags},
        )
