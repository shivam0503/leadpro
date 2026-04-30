from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy.orm import Session

from app.ai.filters import FilterContext
from app.ai.rules import first_matching_rule
from app.ai.trace import DecisionTrace
from app.agents.base_agent import AgentInput, AgentOutput
from app.ai_agents.analytics_prediction import AnalyticsPredictionAgent
from app.ai_agents.conversation_intel import ConversationIntelligenceAgent
from app.ai_agents.lead_qualification import LeadQualificationAgent
from app.ai_agents.objection_handling import ObjectionHandlingAgent
from app.ai_agents.sales_automation import SalesAutomationAgent
from app.services.llm import generate_text
from app.prompts.system import LeadPro_SYSTEM, get_system_prompt


# ─────────────────────────────────────────────────────────────────────────────
# FINAL RESPONSE COMPOSER — appended to the company system prompt at runtime
# ─────────────────────────────────────────────────────────────────────────────
_COMPOSER_SUFFIX = """

You are also the Final Response Composer for this company's CRM.

You receive:
- extracted signals (intent / sentiment / urgency / budget / KB context)
- a lead summary
- a list of candidate WhatsApp messages
- a transparency trace (internal only — never mention it)

Your job:
- Produce ONE best WhatsApp reply: warm, human, concise (2-4 lines max)
- Use the KNOWLEDGE BASE context to quote accurate fees / program details
- Match the student's language (Hinglish if they wrote Hinglish)
- If student asked to stop → comply immediately, no selling
- If you need clarification → ask ONE question only
- Never promise outcomes, never be needy

Return ONLY valid JSON:
{ "message": "...", "alt_messages": ["...", "..."] }
""".strip()


def _build_composer_system(company_slug: str | None) -> str:
    base = get_system_prompt(company_slug)
    return base + "\n\n" + _COMPOSER_SUFFIX


def _utcnow() -> datetime:
    return datetime.utcnow()


def run_multi_agent_decision(
    db: Session,
    ctx: Dict[str, Any],
) -> Tuple[DecisionTrace, List[Dict[str, Any]]]:
    """Runs agent pipeline + rule engine, returns (trace, actions)."""

    company_slug: str | None = ctx.get("company_slug") or None

    inp = AgentInput(
        channel=str(ctx.get("channel") or "unknown"),
        lead=ctx.get("lead") or {},
        inbound_text=ctx.get("inbound_text"),
        event_type=ctx.get("event_type"),
        event_payload=ctx.get("event_payload") or {},
        recent_notes=ctx.get("recent_notes") or [],
        memory_snippets=ctx.get("memory_snippets") or [],
        now_utc=ctx.get("now_utc"),
        user_context=ctx.get("user") or None,
    )

    # Inject KB context into inp so agents can access it
    kb_context: str = ctx.get("kb_context") or ""
    inp.kb_context = kb_context  # type: ignore[attr-defined]

    trace = DecisionTrace(agent_path=[])

    # ── 1. Conversation intelligence ─────────────────────────────────────────
    conv = ConversationIntelligenceAgent().run(inp)
    trace.agent_path.append(ConversationIntelligenceAgent.name)
    trace.intent          = conv.facts.get("intent")
    trace.sentiment       = conv.facts.get("sentiment")
    trace.urgency         = conv.facts.get("urgency")
    trace.buying_signals  = conv.facts.get("buying_signals") or []
    extracted             = dict(conv.facts)

    # ── 2. Lead qualification ─────────────────────────────────────────────────
    qual = LeadQualificationAgent().run(AgentInput(**{**inp.__dict__, "user_context": inp.user_context}))
    trace.agent_path.append(LeadQualificationAgent.name)
    lead_score       = qual.facts.get("lead_score")
    trace.lead_score = lead_score if isinstance(lead_score, int) else None
    extracted.update(qual.facts)

    # ── 3. Objection handling ─────────────────────────────────────────────────
    obj = ObjectionHandlingAgent().run(AgentInput(**{**inp.__dict__, "inbound_text": inp.inbound_text}))
    if obj.facts.get("has_objection"):
        trace.agent_path.append(ObjectionHandlingAgent.name)
        extracted.update(obj.facts)

    # ── 4. Sales automation ───────────────────────────────────────────────────
    sales = SalesAutomationAgent(db=db).run(inp)
    trace.agent_path.append(SalesAutomationAgent.name)
    extracted.update(sales.facts)

    # ── 5. Analytics prediction ───────────────────────────────────────────────
    ana = AnalyticsPredictionAgent(db=db).run(inp)
    trace.agent_path.append(AnalyticsPredictionAgent.name)
    extracted.update(ana.facts)

    # Collect candidate actions
    candidate_actions: List[Dict[str, Any]] = []
    for out in [conv, qual, obj, sales, ana]:
        candidate_actions.extend(out.actions or [])

    # ── Rule engine ───────────────────────────────────────────────────────────
    fc = FilterContext(lead=inp.lead, inbound_text=inp.inbound_text, extracted=extracted, now=_utcnow())
    rm = first_matching_rule(db=db, ctx=fc)
    if rm:
        trace.triggered_rule = {"id": rm.rule_id, "name": rm.name, "priority": rm.priority, "filters": rm.filters}
        trace.why.append(f"Matched automation rule: {rm.name}")
        candidate_actions = (rm.actions or []) + candidate_actions

    # ── Deduplicate send_whatsapp actions ─────────────────────────────────────
    primary_send = None
    others       = []
    for a in candidate_actions:
        if a.get("type") == "send_whatsapp" and a.get("message"):
            if primary_send is None:
                primary_send = a
        else:
            others.append(a)

    if primary_send is None:
        primary_send = {
            "type": "send_whatsapp",
            "message": "Bata do — aap kaun sa exam crack karna chahte ho aur kab tak? Main best program suggest karunga!",
            "next_followup_in_days": 2,
        }

    # ── Final message composer (LLM rewrite with KB context) ─────────────────
    try:
        composer_system = _build_composer_system(company_slug)

        payload = {
            "lead":              inp.lead,
            "inbound_text":      inp.inbound_text,
            "extracted":         extracted,
            "candidate_message": primary_send.get("message"),
            "kb_context":        kb_context[:2000] if kb_context else "",   # cap to avoid token overflow
            "notes":             (inp.recent_notes or [])[-3:],
        }
        raw  = generate_text(system=composer_system, user=json.dumps(payload, ensure_ascii=False), json_object=True)
        data = json.loads(raw)
        msg  = (data.get("message") or "").strip()
        alts = [str(x).strip() for x in (data.get("alt_messages") or []) if str(x).strip()]

        if msg:
            primary_send["message"] = msg

        trace.alternatives = [{"type": "send_whatsapp", "message": m} for m in alts[:3]]
        trace.confidence   = float(extracted.get("confidence") or extracted.get("intent_confidence") or 0.65)

    except Exception as e:
        logger.debug("Final composer skipped: {}", e)
        trace.confidence = float(extracted.get("confidence") or extracted.get("intent_confidence") or 0.55)

    actions = [primary_send] + others
    return trace, actions
