"""
prompts/system.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Central system prompt loader.

For Career Launcher the DB company has `ai_persona` set.
At runtime the orchestrator calls `get_system_prompt(company_slug)`
which returns the company-specific persona if found, else the default.

For agents that build their prompt at class-definition time (not runtime),
they import `AGENT_BASE_SYSTEM` which is a neutral base — no brand hardcoded.
"""

from __future__ import annotations

# ── Legacy default (kept for non-CL / generic companies) ──────────────────────
LeadPro_SYSTEM = """
You are an AI CRM Automation Strategist for a coaching institute.

You generate:
1) Practical, actionable output for student enquiries
2) Honest, compliance-aware outreach (avoid spammy language; prefer opt-in)
3) Concise, helpful responses in the student's language

Always recommend best practices: personalisation, clear value, easy opt-out.
""".strip()


# ── Neutral agent base — used by agents that embed system at class load time ───
AGENT_BASE_SYSTEM = LeadPro_SYSTEM


# ── Runtime loader — call this in orchestrator/agents that run per-request ─────
def get_system_prompt(company_slug: str | None = None) -> str:
    """
    Return the best system prompt for a company slug.
    Priority:  company DB ai_persona  >  LeadPro_SYSTEM default
    """
    if not company_slug:
        return LeadPro_SYSTEM

    try:
        from app.services.database import get_master_db
        from app.db.master_models import Company

        db = next(get_master_db())
        co = db.query(Company).filter(Company.slug == company_slug).first()
        if co and co.ai_persona and co.ai_persona.strip():
            return co.ai_persona.strip()
    except Exception:
        pass

    return LeadPro_SYSTEM
