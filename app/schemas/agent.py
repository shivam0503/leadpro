from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


class SendWhatsAppAction(BaseModel):
    type: Literal["send_whatsapp"]
    message: str = Field(..., min_length=1)
    next_followup_in_days: int = Field(2, ge=0, le=30)


class AddNoteAction(BaseModel):
    type: Literal["add_note"]
    note: str = Field(..., min_length=1)


class UpdateLeadAction(BaseModel):
    type: Literal["update_lead"]
    status: Optional[Literal["new", "contacted", "replied", "demo", "closed", "lost"]] = None
    score: Optional[int] = Field(default=None, ge=0, le=100)
    next_followup_in_days: Optional[int] = Field(default=None, ge=0, le=30)


class DoNotContactAction(BaseModel):
    type: Literal["do_not_contact"]
    reason: str = Field("user_requested", min_length=1)


class HandoffHumanAction(BaseModel):
    type: Literal["handoff_human"]
    reason: str = Field("handoff", min_length=1)


Action = Union[SendWhatsAppAction, AddNoteAction, UpdateLeadAction, DoNotContactAction, HandoffHumanAction]


class DecisionPayload(BaseModel):
    summary: str = Field(default="", description="Short internal summary")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    actions: List[Action] = Field(default_factory=list)

    @field_validator("summary")
    @classmethod
    def _trim_summary(cls, v: str) -> str:
        return (v or "").strip()[:500]


class DecisionParseResult(BaseModel):
    decision: DecisionPayload
    raw_text: str
    raw_json: Optional[Dict[str, Any]] = None
    warnings: List[str] = Field(default_factory=list)
