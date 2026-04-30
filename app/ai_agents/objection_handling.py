from __future__ import annotations

import json
from typing import Any, Dict

from loguru import logger

from app.agents.base_agent import AgentInput, AgentOutput, BaseAgent
from app.services.llm import generate_text
from app.prompts.system import AGENT_BASE_SYSTEM


class ObjectionHandlingAgent(BaseAgent):
    name = "objection_handling"

    _SYSTEM = (
        AGENT_BASE_SYSTEM.strip()
        + "\n\n"
        + """
You are an Objection Handling Agent for a coaching institute CRM.

If the student's message includes:
- fee/price objection  → offer EMI options, compare value vs exam importance
- competitor comparison → highlight CL's track record (ranks, results, faculty)
- trust issue          → share results data, mention money-back guarantee where applicable
- time concern         → flexible batches (weekday/weekend/online/classroom)

Generate a SHORT, empathetic reply (2-4 lines), not salesy.

Return ONLY JSON:
{ "has_objection": true/false, "category": "price|competitor|trust|time|other", "reply": "..." }
""".strip()
    )

    def _heuristic(self, text: str) -> Dict[str, Any]:
        t = (text or "").lower()
        cat = None

        # Price objections
        if any(k in t for k in ["expensive", "too much", "costly", "high price",
                                 "mehnga", "paisa nahi", "budget nahi", "fees zyada"]):
            cat = "price"

        # Competitor comparison
        elif any(k in t for k in ["competitor", "other coaching", "cheaper",
                                   "time2learn", "unacademy", "byju", "testbook",
                                   "alternative", "better option"]):
            cat = "competitor"

        # Trust / proof
        elif any(k in t for k in ["trust", "scam", "proof", "reviews",
                                   "guarantee", "results", "rank", "placement"]):
            cat = "trust"

        # Time concerns
        elif any(k in t for k in ["no time", "busy", "time nahi", "working",
                                   "job", "schedule", "flexible", "weekend"]):
            cat = "time"

        if not cat:
            return {"has_objection": False}

        # CL-specific replies
        replies = {
            "price": (
                "Fees ki baat samajh aata hai! 😊 Career Launcher mein EMI options available hain, "
                "aur abhi special discount chal raha hai. Aapka exam goal kya hai? "
                "Main aapke budget ke hisaab se best program suggest kar sakta hoon. "
                "Call karein: 8130-038-836"
            ),
            "competitor": (
                "Fair point! CL ke results bolte hain — CLAT 2025 mein 6/10 top ranks hamare students ke the. "
                "Aap apne exam aur target colleges batao, main honestly comparison kar ke bataunga. "
                "WhatsApp: 9267-989-969"
            ),
            "trust": (
                "Bilkul valid concern hai! CL 1995 se hai — 29+ years of results. "
                "CAT Black Elite program mein Money Back Guarantee bhi hai. "
                "Free counselling session ke liye call karein: 8130-038-836"
            ),
            "time": (
                "Iske liye CL ne flexible batches banaye hain — Weekend, Weekday, aur fully Online options. "
                "Aap khud decide kar sakte ho apna schedule. "
                "Details ke liye: careerlauncher.com/cl-online"
            ),
        }

        return {
            "has_objection": True,
            "category": cat,
            "reply": replies.get(cat, "Aapka concern samajh aa gaya. Zyada details ke liye call karein: 8130-038-836"),
        }

    def run(self, inp: AgentInput) -> AgentOutput:
        text = (inp.inbound_text or "").strip()
        if not text:
            return AgentOutput(facts={"has_objection": False})

        # Try LLM first
        try:
            payload = {"lead": inp.lead, "text": text}
            raw  = generate_text(
                system=self._SYSTEM,
                user=json.dumps(payload, ensure_ascii=False),
                json_object=True,
            )
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("has_objection") is True and data.get("reply"):
                return AgentOutput(
                    facts=data,
                    actions=[{
                        "type": "send_whatsapp",
                        "message": str(data.get("reply")).strip(),
                        "next_followup_in_days": 2,
                    }],
                    trace={"agent": self.name, **data},
                )
        except Exception as e:
            logger.debug("ObjectionHandling LLM skipped: {}", e)

        # Heuristic fallback with CL-specific replies
        h = self._heuristic(text)
        if h.get("has_objection") and h.get("reply"):
            return AgentOutput(
                facts=h,
                actions=[{
                    "type": "send_whatsapp",
                    "message": h["reply"],
                    "next_followup_in_days": 2,
                }],
                trace={"agent": self.name, **h},
            )

        return AgentOutput(facts={"has_objection": False})
