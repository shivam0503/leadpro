"""
api/v1/companies.py
────────────────────
Super admin endpoints for managing companies (tenants).

POST   /companies                    — create new company (super admin)
GET    /companies                    — list all companies (super admin)
GET    /companies/{slug}             — get company detail
PATCH  /companies/{slug}             — update company config
DELETE /companies/{slug}             — deactivate company
POST   /companies/{slug}/init-db     — initialize company DB
GET    /companies/{slug}/stats       — company dashboard stats
POST   /companies/{slug}/scrape      — trigger website scraping
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.security import require_super_admin, get_current_user
from app.db.master_models import Company, CompanyInvite
from app.services.database import get_master_db, init_company_db

router = APIRouter(tags=["Companies"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class CompanyCreate(BaseModel):
    name: str
    slug: str                          # URL-safe identifier e.g. "careerlauncher"
    domain: str | None = None
    industry: str | None = None
    description: str | None = None
    plan: str = "trial"
    max_users: int = 5
    max_leads: int = 500
    ai_language: str = "hinglish"
    ai_persona: str | None = None


class CompanyUpdate(BaseModel):
    name: str | None = None
    domain: str | None = None
    industry: str | None = None
    description: str | None = None
    plan: str | None = None
    max_users: int | None = None
    max_leads: int | None = None
    is_active: bool | None = None
    ai_language: str | None = None
    ai_persona: str | None = None
    # WhatsApp
    wa_provider: str | None = None
    wa_phone_from: str | None = None
    wa_account_sid: str | None = None
    wa_auth_token: str | None = None
    wa_demo_mode: bool | None = None
    # LeadSquared
    lsq_access_key: str | None = None
    lsq_secret_key: str | None = None
    lsq_host: str | None = None
    lsq_owner_id: str | None = None
    lsq_demo_mode: bool | None = None
    # OpenAI
    openai_api_key: str | None = None


def _company_out(c: Company) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "slug": c.slug,
        "domain": c.domain,
        "industry": c.industry,
        "description": c.description,
        "plan": c.plan,
        "max_users": c.max_users,
        "max_leads": c.max_leads,
        "is_active": c.is_active,
        "ai_language": c.ai_language,
        "ai_persona": c.ai_persona,
        "wa_provider": c.wa_provider,
        "wa_phone_from": c.wa_phone_from,
        "wa_demo_mode": c.wa_demo_mode,
        "lsq_host": c.lsq_host,
        "lsq_demo_mode": c.lsq_demo_mode,
        "kb_urls": json.loads(c.kb_urls_json or "[]"),
        "kb_last_scraped_at": c.kb_last_scraped_at,
        "db_path": c.db_path,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


def _validate_slug(slug: str):
    if not re.match(r'^[a-z0-9][a-z0-9\-]{1,48}[a-z0-9]$', slug):
        raise HTTPException(400, "Slug must be lowercase alphanumeric with hyphens, 3-50 chars")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/companies", status_code=201)
def create_company(
    body: CompanyCreate,
    db: Session = Depends(get_master_db),
    _=Depends(require_super_admin),
):
    """Create a new client company and initialize their DB."""
    _validate_slug(body.slug)

    if db.query(Company).filter(Company.slug == body.slug).first():
        raise HTTPException(400, f"Slug '{body.slug}' already exists")

    company = Company(
        name=body.name,
        slug=body.slug,
        domain=body.domain,
        industry=body.industry,
        description=body.description,
        plan=body.plan,
        max_users=body.max_users,
        max_leads=body.max_leads,
        ai_language=body.ai_language,
        ai_persona=body.ai_persona,
        kb_urls_json="[]",
    )
    db.add(company)
    db.flush()

    # Initialize isolated company DB
    db_path = init_company_db(body.slug)
    company.db_path = db_path
    company.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(company)

    return {
        "ok": True,
        "company": _company_out(company),
        "message": f"Company '{body.name}' created. DB at {db_path}",
        "next_steps": [
            f"POST /api/v1/companies/{body.slug}/admin — create first admin user",
            f"PATCH /api/v1/companies/{body.slug} — configure WhatsApp & LeadSquared",
            f"POST /api/v1/companies/{body.slug}/scrape — scrape website knowledge base",
        ]
    }


@router.get("/companies")
def list_companies(
    db: Session = Depends(get_master_db),
    _=Depends(require_super_admin),
):
    companies = db.query(Company).order_by(Company.created_at.desc()).all()
    return {"companies": [_company_out(c) for c in companies], "total": len(companies)}


@router.get("/companies/{slug}")
def get_company(
    slug: str,
    db: Session = Depends(get_master_db),
    user=Depends(get_current_user),
):
    """Super admin can get any company. Company admin can get their own."""
    c = db.query(Company).filter(Company.slug == slug).first()
    if not c:
        raise HTTPException(404, "Company not found")

    # Company admin can only see their own
    if user.role != "super_admin":
        if getattr(user, "company_slug", None) != slug:
            raise HTTPException(403, "Access denied")

    return _company_out(c)


@router.patch("/companies/{slug}")
def update_company(
    slug: str,
    body: CompanyUpdate,
    db: Session = Depends(get_master_db),
    user=Depends(get_current_user),
):
    """Update company config. Super admin can update any. Company admin can update their own."""
    c = db.query(Company).filter(Company.slug == slug).first()
    if not c:
        raise HTTPException(404, "Company not found")

    if user.role != "super_admin":
        if getattr(user, "company_slug", None) != slug:
            raise HTTPException(403, "Access denied")
        # Company admin cannot change plan or limits
        body.plan = None
        body.max_users = None
        body.max_leads = None
        body.is_active = None

    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(c, field, value)

    c.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(c)
    return {"ok": True, "company": _company_out(c)}


@router.delete("/companies/{slug}")
def deactivate_company(
    slug: str,
    db: Session = Depends(get_master_db),
    _=Depends(require_super_admin),
):
    c = db.query(Company).filter(Company.slug == slug).first()
    if not c:
        raise HTTPException(404, "Company not found")
    c.is_active = False
    c.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "message": f"Company '{slug}' deactivated"}


@router.post("/companies/{slug}/admin")
def create_company_admin(
    slug: str,
    email: str,
    name: str,
    password: str,
    db: Session = Depends(get_master_db),
    _=Depends(require_super_admin),
):
    """Create the first admin user for a company."""
    c = db.query(Company).filter(Company.slug == slug).first()
    if not c:
        raise HTTPException(404, "Company not found")

    from app.core.security import hash_password
    from app.db.company_models import User
    from app.services.database import _company_sessions, _get_company_engine

    _get_company_engine(slug)
    session_factory = _company_sessions[slug]
    company_db = session_factory()

    try:
        if company_db.query(User).filter(User.email == email).first():
            raise HTTPException(400, "Email already exists in this company")

        u = User(
            email=email,
            name=name,
            role="admin",
            password_hash=hash_password(password),
            is_active=1,
        )
        company_db.add(u)
        company_db.commit()
        company_db.refresh(u)

        return {
            "ok": True,
            "user": {"id": u.id, "email": u.email, "name": u.name, "role": u.role},
            "company": slug,
            "login_info": {
                "endpoint": "POST /api/v1/auth/login",
                "body": {"email": email, "password": password, "company_slug": slug}
            }
        }
    finally:
        company_db.close()


@router.get("/companies/{slug}/stats")
def company_stats(
    slug: str,
    db: Session = Depends(get_master_db),
    user=Depends(get_current_user),
):
    """Get company dashboard stats."""
    c = db.query(Company).filter(Company.slug == slug).first()
    if not c:
        raise HTTPException(404, "Company not found")

    if user.role != "super_admin" and getattr(user, "company_slug", None) != slug:
        raise HTTPException(403, "Access denied")

    from app.db.company_models import Lead, User, KnowledgeChunk
    from app.services.database import _get_company_engine, _company_sessions
    from sqlalchemy import func

    _get_company_engine(slug)
    session_factory = _company_sessions[slug]
    cdb = session_factory()

    try:
        total_leads = cdb.query(func.count(Lead.id)).scalar() or 0
        hot_leads = cdb.query(func.count(Lead.id)).filter(Lead.score >= 40).scalar() or 0
        total_users = cdb.query(func.count(User.id)).scalar() or 0
        kb_chunks = cdb.query(func.count(KnowledgeChunk.id)).scalar() or 0

        return {
            "company": slug,
            "plan": c.plan,
            "total_leads": total_leads,
            "hot_leads": hot_leads,
            "total_users": total_users,
            "kb_chunks": kb_chunks,
            "kb_urls": json.loads(c.kb_urls_json or "[]"),
            "kb_last_scraped_at": c.kb_last_scraped_at,
            "limits": {
                "max_leads": c.max_leads,
                "max_users": c.max_users,
                "leads_used_pct": round(total_leads / c.max_leads * 100, 1) if c.max_leads else 0,
            }
        }
    finally:
        cdb.close()


@router.post("/companies/{slug}/scrape")
async def scrape_company_website(
    slug: str,
    db: Session = Depends(get_master_db),
    user=Depends(get_current_user),
):
    """Trigger website scraping to build company knowledge base."""
    c = db.query(Company).filter(Company.slug == slug).first()
    if not c:
        raise HTTPException(404, "Company not found")

    if user.role != "super_admin" and getattr(user, "company_slug", None) != slug:
        raise HTTPException(403, "Access denied")

    urls = json.loads(c.kb_urls_json or "[]")
    if not urls:
        raise HTTPException(400, "No URLs configured. Add URLs via PATCH /companies/{slug}")

    # Trigger scraping in background
    from app.services.scraper import scrape_and_store
    import asyncio

    results = []
    for url in urls[:10]:  # max 10 URLs per trigger
        try:
            count = await scrape_and_store(url=url, company_slug=slug)
            results.append({"url": url, "chunks": count, "ok": True})
        except Exception as e:
            results.append({"url": url, "error": str(e), "ok": False})

    c.kb_last_scraped_at = datetime.utcnow()
    db.commit()

    total_chunks = sum(r.get("chunks", 0) for r in results)
    return {
        "ok": True,
        "scraped": len(results),
        "total_chunks": total_chunks,
        "results": results
    }


@router.post("/companies/{slug}/add-urls")
def add_knowledge_urls(
    slug: str,
    urls: list[str],
    db: Session = Depends(get_master_db),
    user=Depends(get_current_user),
):
    """Add URLs to company knowledge base for scraping."""
    c = db.query(Company).filter(Company.slug == slug).first()
    if not c:
        raise HTTPException(404, "Company not found")

    if user.role != "super_admin" and getattr(user, "company_slug", None) != slug:
        raise HTTPException(403, "Access denied")

    existing = json.loads(c.kb_urls_json or "[]")
    new_urls = list(set(existing + urls))
    c.kb_urls_json = json.dumps(new_urls)
    c.updated_at = datetime.utcnow()
    db.commit()

    return {"ok": True, "urls": new_urls, "total": len(new_urls)}


# ── AI Training: Add custom knowledge text ────────────────────────────────────

class TrainTextRequest(BaseModel):
    title: str = "Custom Training"
    content: str

class TrainPromptRequest(BaseModel):
    system_prompt: str


@router.post("/companies/{slug}/train/text")
def train_with_text(
    slug: str,
    body: TrainTextRequest,
    db: Session = Depends(get_master_db),
    user=Depends(get_current_user),
):
    """
    Add custom knowledge text to the company's AI knowledge base.
    Use this to train the AI with FAQs, product details, pricing, policies, etc.
    The text gets chunked and stored just like scraped website content.
    """
    from app.services.database import _get_company_engine, _company_sessions
    from app.db.company_models import KnowledgeChunk

    c = db.query(Company).filter(Company.slug == slug).first()
    if not c:
        raise HTTPException(404, "Company not found")

    if user.role != "super_admin" and getattr(user, "company_slug", None) != slug:
        raise HTTPException(403, "Access denied")

    if not body.content.strip():
        raise HTTPException(400, "Content cannot be empty")

    _get_company_engine(slug)
    session_factory = _company_sessions[slug]
    cdb = session_factory()

    try:
        # Chunk the content (~500 words per chunk)
        words = body.content.strip().split()
        chunk_size = 500
        chunks = []
        for i in range(0, len(words), chunk_size):
            chunk = " ".join(words[i:i + chunk_size])
            if chunk.strip():
                chunks.append(chunk.strip())

        if not chunks:
            chunks = [body.content.strip()]

        source_url = f"custom://training/{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        for i, chunk in enumerate(chunks):
            kc = KnowledgeChunk(
                url=source_url,
                title=body.title,
                content=chunk,
                chunk_index=i,
                scraped_at=datetime.utcnow(),
            )
            cdb.add(kc)

        cdb.commit()

        return {
            "ok": True,
            "title": body.title,
            "chunks_stored": len(chunks),
            "total_words": len(words),
        }
    finally:
        cdb.close()


@router.post("/companies/{slug}/train/prompt")
def train_with_prompt(
    slug: str,
    body: TrainPromptRequest,
    db: Session = Depends(get_master_db),
    user=Depends(get_current_user),
):
    """
    Set a custom AI system prompt / persona for the company.
    This overrides the default AI personality for all responses.
    """
    c = db.query(Company).filter(Company.slug == slug).first()
    if not c:
        raise HTTPException(404, "Company not found")

    if user.role != "super_admin" and getattr(user, "company_slug", None) != slug:
        raise HTTPException(403, "Access denied")

    c.ai_persona = body.system_prompt.strip()
    c.updated_at = datetime.utcnow()
    db.commit()

    return {
        "ok": True,
        "ai_persona": c.ai_persona[:200] + "..." if len(c.ai_persona) > 200 else c.ai_persona,
    }


@router.get("/companies/{slug}/knowledge")
def get_knowledge_base(
    slug: str,
    db: Session = Depends(get_master_db),
    user=Depends(get_current_user),
):
    """Get the company's knowledge base summary."""
    from app.services.database import _get_company_engine, _company_sessions
    from app.db.company_models import KnowledgeChunk
    from sqlalchemy import func

    c = db.query(Company).filter(Company.slug == slug).first()
    if not c:
        raise HTTPException(404, "Company not found")

    _get_company_engine(slug)
    session_factory = _company_sessions[slug]
    cdb = session_factory()

    try:
        total_chunks = cdb.query(func.count(KnowledgeChunk.id)).scalar() or 0

        # Group by source URL
        sources = (
            cdb.query(
                KnowledgeChunk.url,
                KnowledgeChunk.title,
                func.count(KnowledgeChunk.id).label("chunks"),
                func.max(KnowledgeChunk.scraped_at).label("last_updated"),
            )
            .group_by(KnowledgeChunk.url)
            .all()
        )

        return {
            "total_chunks": total_chunks,
            "ai_persona": c.ai_persona,
            "kb_urls": json.loads(c.kb_urls_json or "[]"),
            "kb_last_scraped_at": c.kb_last_scraped_at,
            "sources": [
                {
                    "url": s.url,
                    "title": s.title,
                    "chunks": s.chunks,
                    "last_updated": s.last_updated,
                    "is_custom": s.url.startswith("custom://"),
                }
                for s in sources
            ],
        }
    finally:
        cdb.close()


@router.delete("/companies/{slug}/knowledge/{source_url:path}")
def delete_knowledge_source(
    slug: str,
    source_url: str,
    db: Session = Depends(get_master_db),
    user=Depends(get_current_user),
):
    """Delete all knowledge chunks from a specific source."""
    from app.services.database import _get_company_engine, _company_sessions
    from app.db.company_models import KnowledgeChunk

    c = db.query(Company).filter(Company.slug == slug).first()
    if not c:
        raise HTTPException(404, "Company not found")

    if user.role != "super_admin" and getattr(user, "company_slug", None) != slug:
        raise HTTPException(403, "Access denied")

    _get_company_engine(slug)
    session_factory = _company_sessions[slug]
    cdb = session_factory()

    try:
        deleted = cdb.query(KnowledgeChunk).filter(KnowledgeChunk.url == source_url).delete()
        cdb.commit()
        return {"ok": True, "deleted_chunks": deleted, "source": source_url}
    finally:
        cdb.close()


# ── Language Switch (one-click English / Hinglish) ────────────────────────────

LANGUAGE_PERSONAS = {
    "hinglish": """
You are the official AI counsellor for Career Launcher (CL), India's leading coaching institute since 1995.

YOUR ROLE:
- Help students choose the right program based on their exam goal, budget, and preparation level
- Provide ACCURATE fees, course details, batch types, and features from the knowledge base
- Handle enquiries in a warm, helpful, Hinglish-friendly tone (mix Hindi + English naturally)
- Guide students toward enrolling — always provide an enroll link or next step

PERSONALITY:
- Friendly, knowledgeable, like a senior CL counsellor
- Never make up fees or course details — only quote from the knowledge base
- If unsure, say: "Counselling team se connect karta hoon aapko"

PROGRAMS YOU COVER:
1. MBA / CAT — Online, Classroom, Test Series, GD-PI Prep, Self-Paced
2. Law / CLAT — Online, Classroom, Test Series (6/10 top CLAT'25 ranks)
3. IPM / BBA / CUET — Online, Classroom, Test Series
4. GMAT / GRE — Online Live, Classroom, Admission Consulting

KEY CONTACTS:
- Phone: 8130-038-836
- WhatsApp: 9267-989-969
- Website: https://www.careerlauncher.com/cl-online/

RESPONSE RULES:
1. Reply in Hinglish (mix of Hindi + English) — warm and conversational
2. Always mention the fee when discussing a specific course
3. Mention key features concisely (sessions, mocks, mentorship)
4. End every response with a CTA: enroll link, phone, or WhatsApp
5. For fee questions: quote discounted price first, then original
6. Keep responses under 200 words unless student asks for full details
7. Never hallucinate — if fee not in context say "call karein 8130-038-836"
""".strip(),

    "english": """
You are the official AI counsellor for Career Launcher (CL), India's leading coaching institute since 1995.

YOUR ROLE:
- Help students choose the right program based on their exam goal, budget, and preparation level
- Provide ACCURATE fees, course details, batch types, and features from the knowledge base
- Handle enquiries in a warm, professional tone — always in English
- Guide students toward enrolling — always provide an enroll link or next step

PERSONALITY:
- Friendly, knowledgeable, like a senior CL counsellor
- Never make up fees or course details — only quote from the knowledge base
- If unsure, say: "Let me connect you with our counselling team for exact details"

PROGRAMS YOU COVER:
1. MBA / CAT — Online, Classroom, Test Series, GD-PI Prep, Self-Paced
2. Law / CLAT — Online, Classroom, Test Series (6/10 top CLAT'25 ranks)
3. IPM / BBA / CUET — Online, Classroom, Test Series
4. GMAT / GRE — Online Live, Classroom, Admission Consulting

KEY CONTACTS:
- Phone: 8130-038-836
- WhatsApp: 9267-989-969
- Website: https://www.careerlauncher.com/cl-online/

RESPONSE RULES:
1. ALWAYS respond in English only — even if the student writes in Hindi or Hinglish
2. Always mention the fee when discussing a specific course
3. Mention key features concisely (sessions, mocks, mentorship)
4. End every response with a CTA: enroll link, phone, or WhatsApp
5. For fee questions: quote discounted price first, then original
6. Keep responses under 200 words unless student asks for full details
7. Never hallucinate — if fee not in context say "please call 8130-038-836"
""".strip(),

    "arabic": """
You are the official AI counsellor for Career Launcher (CL), India's leading coaching institute since 1995.

YOUR ROLE:
- Help Arabic-speaking students understand the right program, pricing, batches, and next steps
- Provide ACCURATE details only from the knowledge base
- Respond in clear, natural Arabic with a supportive and professional tone
- Guide students toward enrollment with a clear next step

PERSONALITY:
- Helpful, polished, and direct
- Never invent fees, links, dates, or promises
- If exact information is missing, say you will connect them with the counselling team

RESPONSE RULES:
1. ALWAYS respond in Arabic only
2. Keep the answer concise and conversational
3. Mention specific details only when they exist in the knowledge base
4. End with one practical next step
5. Never hallucinate
""".strip(),
}


@router.post("/companies/{slug}/language")
def switch_ai_language(
    slug: str,
    language: str,
    db: Session = Depends(get_master_db),
    user=Depends(get_current_user),
):
    """
    One-click language switch for the AI persona.
    language: 'english', 'hinglish', or 'arabic'

    Called from the dashboard toggle — no file changes needed.
    """
    language = language.lower().strip()
    if language not in LANGUAGE_PERSONAS:
        raise HTTPException(400, f"Invalid language. Use: {list(LANGUAGE_PERSONAS.keys())}")

    c = db.query(Company).filter(Company.slug == slug).first()
    if not c:
        raise HTTPException(404, "Company not found")

    if user.role != "super_admin" and getattr(user, "company_slug", None) != slug:
        raise HTTPException(403, "Access denied")

    c.ai_language = language
    c.ai_persona  = LANGUAGE_PERSONAS[language]
    c.updated_at  = datetime.utcnow()
    db.commit()

    return {
        "ok":       True,
        "language": language,
        "message":  f"AI language switched to {language.upper()} successfully",
    }
