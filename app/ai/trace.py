from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DecisionTrace:
    """Human-readable transparency layer."""
    intent: Optional[str] = None
    sentiment: Optional[str] = None
    urgency: Optional[str] = None
    buying_signals: List[str] = field(default_factory=list)

    lead_score: Optional[int] = None
    confidence: float = 0.0

    triggered_rule: Optional[Dict[str, Any]] = None
    agent_path: List[str] = field(default_factory=list)

    why: List[str] = field(default_factory=list)
    alternatives: List[Dict[str, Any]] = field(default_factory=list)

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "sentiment": self.sentiment,
            "urgency": self.urgency,
            "buying_signals": self.buying_signals,
            "lead_score": self.lead_score,
            "confidence": round(float(self.confidence or 0.0), 3),
            "triggered_rule": self.triggered_rule,
            "agent_path": self.agent_path,
            "why": self.why,
            "alternatives": self.alternatives,
        }
