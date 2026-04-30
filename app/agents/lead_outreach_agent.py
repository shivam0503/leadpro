from app.services.llm import generate_text
from app.prompts.system import LeadPro_SYSTEM
from app.prompts.outreach import OUTREACH_TEMPLATE
from app.services.vector_store import store_memory

def create_outreach(company: str, website: str | None = None, pain: str | None = None) -> dict:
    user_prompt = OUTREACH_TEMPLATE.format(
        company=company,
        website=website or "unknown",
        pain=pain or "unknown",
    )

    draft = generate_text(system=LeadPro_SYSTEM, user=user_prompt)

    # Auto-store to memory
    mem_text = f"Company: {company}\nWebsite: {website}\nPain: {pain}\n\nOutreach Draft:\n{draft}"
    mem_id = store_memory(
        text=mem_text,
        metadata={"type": "outreach", "company": company, "website": website or "", "pain": pain or ""}
    )

    return {"company": company, "draft": draft, "memory_id": mem_id}