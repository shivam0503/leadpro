from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentInput:
    """Normalized input passed between agents."""
    channel: str  # whatsapp|autopilot|ui
    lead: Dict[str, Any]
    inbound_text: Optional[str] = None
    event_type: Optional[str] = None
    event_payload: Optional[Dict[str, Any]] = None
    recent_notes: List[Dict[str, Any]] = field(default_factory=list)
    memory_snippets: List[str] = field(default_factory=list)
    now_utc: Optional[str] = None
    user_context: Optional[Dict[str, Any]] = None  # authenticated user, org, role, etc.


@dataclass
class AgentOutput:
    """Agent output + trace artifacts."""
    facts: Dict[str, Any] = field(default_factory=dict)     # structured extractions
    suggestions: Dict[str, Any] = field(default_factory=dict)  # recommendations
    actions: List[Dict[str, Any]] = field(default_factory=list)  # CRM actions (runner executes)
    trace: Dict[str, Any] = field(default_factory=dict)     # user-friendly trace for UI


class BaseAgent(ABC):
    name: str = "base"

    @abstractmethod
    def run(self, inp: AgentInput) -> AgentOutput:
        raise NotImplementedError
