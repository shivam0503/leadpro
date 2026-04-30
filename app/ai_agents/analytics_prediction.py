from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict

from sqlalchemy.orm import Session

from app.agents.base_agent import AgentInput, AgentOutput, BaseAgent
from app.db.models import Lead, LeadNote


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


class AnalyticsPredictionAgent(BaseAgent):
    name = "analytics_prediction"

    def __init__(self, db: Session):
        self.db = db

    def run(self, inp: AgentInput) -> AgentOutput:
        lead = inp.lead or {}
        score = float(lead.get("score") or 0)
        engagement = float(lead.get("engagement") or 0)

        # conversion probability heuristic
        x = (score - 50.0) / 10.0 + (engagement / 10.0)
        p = float(_sigmoid(x))
        # forecast revenue placeholder: expected_value = p * avg_deal
        avg_deal = float(lead.get("expected_deal_value") or 10000.0)
        expected = p * avg_deal

        facts = {
            "conversion_probability": round(p, 3),
            "expected_revenue": round(expected, 2),
        }
        return AgentOutput(facts=facts, trace={"agent": self.name, **facts})
