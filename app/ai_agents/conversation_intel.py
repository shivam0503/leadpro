from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from loguru import logger

from app.agents.base_agent import AgentInput, AgentOutput, BaseAgent
from app.services.llm import generate_text
from app.prompts.system import AGENT_BASE_SYSTEM


class ConversationIntelligenceAgent(BaseAgent):
    name = "conversation_intelligence"

    _SYSTEM = (
        AGENT_BASE_SYSTEM.strip()
        + "\n\n"
        + """
You are a Conversation Intelligence Agent for a coaching institute (Career Launcher) WhatsApp CRM.

Given a student's inbound message + lead context, extract:
- intent: one of [pricing, enroll, program_info, batch_timing, followup, objection, stop, other]
- sentiment: [positive, neutral, negative]
- urgency: [low, medium, high]
- exam_interest: the exam they're asking about e.g. "CAT", "CLAT", "GMAT", "IPM", "CUET", null
- budget: numeric INR if mentioned, else null
- language: [en, hi, hinglish, other]
- buying_signals: short list (max 5 keywords)
- confidence: 0.0 to 1.0

Return ONLY valid JSON:
{
  "intent": "...",
  "sentiment": "...",
  "urgency": "...",
  "exam_interest": null,
  "budget": null,
  "language": "...",
  "buying_signals": [],
  "confidence": 0.0
}
""".strip()
    )

    def _heuristic(self, text: str) -> Dict[str, Any]:
        t = (text or "").lower()

        # ── Intent ────────────────────────────────────────────────────────────
        intent = "other"
        if any(k in t for k in ["fee", "fees", "price", "cost", "kitna", "charges",
                                 "kitne", "paisa", "rupees", "₹"]):
            intent = "pricing"
        elif any(k in t for k in ["enroll", "join", "register", "admission", "buy",
                                   "purchase", "lena hai", "karna hai"]):
            intent = "enroll"
        elif any(k in t for k in ["batch", "timing", "schedule", "time", "weekend",
                                   "weekday", "morning", "evening", "online", "classroom"]):
            intent = "batch_timing"
        elif any(k in t for k in ["course", "program", "syllabus", "details", "features",
                                   "kya hai", "bata", "information", "info"]):
            intent = "program_info"
        elif any(k in t for k in ["stop", "unsubscribe", "remove", "block",
                                   "mat bhejo", "band karo"]):
            intent = "stop"
        elif any(k in t for k in ["expensive", "mehnga", "costly", "better option",
                                   "competitor", "other coaching"]):
            intent = "objection"

        # ── Exam interest ─────────────────────────────────────────────────────
        exam_interest = None
        exam_map = {
            "cat":    "CAT",
            "mba":    "CAT/MBA",
            "clat":   "CLAT",
            "ailet":  "AILET",
            "law":    "CLAT",
            "ipm":    "IPM",
            "ipmat":  "IPM",
            "bba":    "BBA",
            "cuet":   "CUET",
            "gmat":   "GMAT",
            "gre":    "GRE",
            "snap":   "SNAP",
            "nmat":   "NMAT",
            "xat":    "XAT",
            "gate":   "GATE",
            "upsc":   "UPSC",
        }
        for kw, label in exam_map.items():
            if kw in t:
                exam_interest = label
                break

        # ── Sentiment ─────────────────────────────────────────────────────────
        sentiment = "neutral"
        if any(k in t for k in ["great", "thanks", "awesome", "perfect",
                                  "bahut accha", "shukriya", "helpful"]):
            sentiment = "positive"
        if any(k in t for k in ["bad", "worst", "angry", "spam",
                                  "irritating", "bekar", "bakwas"]):
            sentiment = "negative"

        # ── Urgency ───────────────────────────────────────────────────────────
        urgency = "low"
        if any(k in t for k in ["today", "asap", "urgent", "immediately",
                                  "now", "abhi", "jaldi"]):
            urgency = "high"
        elif any(k in t for k in ["this week", "soon", "tomorrow", "kal",
                                   "is hafte"]):
            urgency = "medium"

        # ── Budget ────────────────────────────────────────────────────────────
        budget = None
        m = re.search(r"(?:inr|₹|rs\.?)\s*([0-9][0-9,]*(?:\.[0-9]+)?)", t)
        if m:
            try:
                budget = float(m.group(1).replace(",", ""))
            except Exception:
                pass

        # ── Language ─────────────────────────────────────────────────────────
        language = "en"
        if re.search(r"[\u0900-\u097F]", text or ""):
            language = "hi"
        if any(w in t for w in ["bhai", "yaar", "kya", "hai", "nahi", "karna",
                                  "chahiye", "paisa", "kitna", "lena", "batao"]):
            language = "hinglish"

        # ── Buying signals ────────────────────────────────────────────────────
        signal_kws = ["enroll", "join", "price", "fee", "batch", "start", "register",
                       "admission", "lena", "karna"]
        buying_signals = [k for k in signal_kws if k in t][:5]

        return {
            "intent":         intent,
            "sentiment":      sentiment,
            "urgency":        urgency,
            "exam_interest":  exam_interest,
            "budget":         budget,
            "language":       language,
            "buying_signals": buying_signals,
            "confidence":     0.58,
        }

    def run(self, inp: AgentInput) -> AgentOutput:
        text = (inp.inbound_text or "").strip()
        if not text:
            return AgentOutput(facts={
                "intent": "followup", "sentiment": "neutral",
                "urgency": "low", "confidence": 0.5,
            })

        # Try LLM
        try:
            payload = {"lead": inp.lead, "text": text}
            raw  = generate_text(
                system=self._SYSTEM,
                user=json.dumps(payload, ensure_ascii=False),
                json_object=True,
            )
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("intent"):
                return AgentOutput(facts=data, trace={"agent": self.name, "extracted": data})
        except Exception as e:
            logger.debug("ConversationIntel LLM skipped: {}", e)

        h = self._heuristic(text)
        return AgentOutput(facts=h, trace={"agent": self.name, "extracted": h})
