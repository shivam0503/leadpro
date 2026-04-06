from app.services.llm import generate_text
from app.prompts.system import LeadPro_SYSTEM
from app.prompts.outreach import FOLLOWUP_TEMPLATE

def generate_followup(
    company: str,
    website: str | None,
    contact_name: str | None,
    email: str | None,
    phone: str | None,
    pain: str | None,
    status: str,
    notes: str,
    memory: str = "",
) -> str:
    prompt = FOLLOWUP_TEMPLATE.format(
        company=company,
        website=website or "unknown",
        contact_name=contact_name or "unknown",
        email=email or "unknown",
        phone=phone or "unknown",
        pain=pain or "unknown",
        status=status,
        notes=notes or "none",
        memory=memory or "none",
    )
    return generate_text(system=LeadPro_SYSTEM, user=prompt)