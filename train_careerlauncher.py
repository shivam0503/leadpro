#!/usr/bin/env python3
"""
train_careerlauncher.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
One-click script to train the Career Launcher AI.

Does 3 things automatically:
  1. Sets the CL AI persona (system prompt) in the company DB
  2. Registers the 4 product URLs in the company's kb_urls
  3. Scrapes all 4 URLs and stores knowledge chunks

Run:
    python train_careerlauncher.py

    # Or specify a different company slug:
    python train_careerlauncher.py --slug careerlauncher-prod

Requirements:
    - Your FastAPI app must be running OR you run this standalone (it uses DB directly)
    - pip install playwright beautifulsoup4 httpx
    - Playwright browsers installed: playwright install chromium
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import asyncio
import argparse
import sys
import json
from datetime import datetime
from pathlib import Path

# ── Make sure project root is on path ─────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# ── Career Launcher config ────────────────────────────────────────────────────
COMPANY_SLUG = "careerlauncher"

TRAINING_URLS = [
    "https://www.careerlauncher.com/cl-online/product-category.jsp?prodCat=MBA",
    "https://www.careerlauncher.com/cl-online/product-category.jsp?prodCat=LST",
    "https://www.careerlauncher.com/cl-online/product-category.jsp?prodCat=AFTER-12",
    "https://www.careerlauncher.com/cl-online/product-category.jsp?prodCat=GMAT",
]

CL_AI_PERSONA = """
You are the official AI counsellor for Career Launcher (CL), India's leading coaching institute since 1995.

YOUR ROLE:
- Help students choose the right program based on their exam goal, budget, and preparation level
- Provide ACCURATE fees, course details, batch types, and features from the knowledge base
- Handle enquiries in a warm, helpful, Hinglish-friendly tone (mix Hindi + English naturally)
- Guide students toward enrolling — always provide an enroll link or next step

PERSONALITY:
- Friendly, knowledgeable, like a senior CL counsellor
- Never make up fees or course details — only quote from the knowledge base
- If unsure, say: "Let me connect you with our counselling team for exact details"

PROGRAMS YOU COVER:
1. MBA / CAT — Online Coaching, Classroom, Test Series, GD-PI Prep, Self-Paced
2. Law / CLAT — Online, Classroom, Test Series (6/10 top CLAT 2025 ranks achieved)
3. IPM / BBA / CUET — Online, Classroom, Test Series
4. GMAT / GRE — Online Live, Classroom, Admission Consulting

KEY CONTACTS:
- Phone: 8130-038-836
- WhatsApp: 9267-989-969
- Website: https://www.careerlauncher.com/cl-online/

RESPONSE RULES:
1. Always mention the fee when discussing a specific course
2. Mention key features concisely (sessions, mocks, mentorship)
3. End every response with a CTA: enroll link, phone, or WhatsApp
4. For fee questions: quote discounted price first, then original (strikethrough)
5. Keep responses under 200 words unless the student asks for full details
6. Never hallucinate — if a fee is not in context, say "please call 8130-038-836 for current pricing"
""".strip()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Set AI persona in company DB
# ─────────────────────────────────────────────────────────────────────────────
def set_ai_persona(slug: str) -> bool:
    print(f"\n{'='*60}")
    print(f"  STEP 1: Setting CL AI Persona for [{slug}]")
    print(f"{'='*60}")
    try:
        from app.services.database import get_master_db
        from app.db.master_models import Company

        db = next(get_master_db())
        company = db.query(Company).filter(Company.slug == slug).first()

        if not company:
            print(f"  ❌ Company '{slug}' not found in DB.")
            print(f"     Create it first via: POST /api/v1/companies")
            return False

        company.ai_persona = CL_AI_PERSONA
        company.updated_at = datetime.utcnow()
        db.commit()

        print(f"  ✅ AI persona set ({len(CL_AI_PERSONA)} chars)")
        return True

    except Exception as e:
        print(f"  ❌ Failed to set persona: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Register training URLs in company kb_urls
# ─────────────────────────────────────────────────────────────────────────────
def register_kb_urls(slug: str) -> bool:
    print(f"\n{'='*60}")
    print(f"  STEP 2: Registering KB URLs for [{slug}]")
    print(f"{'='*60}")
    try:
        from app.services.database import get_master_db
        from app.db.master_models import Company

        db = next(get_master_db())
        company = db.query(Company).filter(Company.slug == slug).first()

        if not company:
            print(f"  ❌ Company '{slug}' not found.")
            return False

        company.kb_urls_json = json.dumps(TRAINING_URLS)
        company.updated_at   = datetime.utcnow()
        db.commit()

        print(f"  ✅ Registered {len(TRAINING_URLS)} URLs:")
        for url in TRAINING_URLS:
            print(f"     • {url}")

        return True

    except Exception as e:
        print(f"  ❌ Failed to register URLs: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Scrape all 4 URLs and store knowledge chunks
# ─────────────────────────────────────────────────────────────────────────────
async def scrape_all_urls(slug: str) -> dict:
    print(f"\n{'='*60}")
    print(f"  STEP 3: Scraping Career Launcher Product Pages")
    print(f"{'='*60}")
    print(f"  This will open headless Chrome and render each JSP page.")
    print(f"  Expected time: ~2–4 minutes for all 4 URLs.\n")

    from app.services.scraper import scrape_and_store
    from app.services.database import get_master_db
    from app.db.master_models import Company

    results = {}
    total_chunks = 0

    url_labels = {
        TRAINING_URLS[0]: "MBA / CAT Programs",
        TRAINING_URLS[1]: "Law / CLAT Programs",
        TRAINING_URLS[2]: "IPM / BBA / CUET Programs",
        TRAINING_URLS[3]: "GMAT / Study Abroad Programs",
    }

    for url in TRAINING_URLS:
        label = url_labels.get(url, url)
        print(f"  🔄 Scraping: {label}")
        print(f"     URL: {url}")
        try:
            chunks = await scrape_and_store(url=url, company_slug=slug, crawl=False)
            results[url] = {"ok": True, "chunks": chunks, "label": label}
            total_chunks += chunks
            print(f"  ✅ Done → {chunks} chunks stored\n")
        except Exception as e:
            results[url] = {"ok": False, "error": str(e), "label": label}
            print(f"  ❌ Failed: {e}\n")

    # Update last_scraped_at in master DB
    try:
        db = next(get_master_db())
        company = db.query(Company).filter(Company.slug == slug).first()
        if company:
            company.kb_last_scraped_at = datetime.utcnow()
            db.commit()
    except Exception:
        pass

    return {"results": results, "total_chunks": total_chunks}


# ─────────────────────────────────────────────────────────────────────────────
# VERIFICATION — Test search_knowledge to confirm training worked
# ─────────────────────────────────────────────────────────────────────────────
def verify_training(slug: str) -> None:
    print(f"\n{'='*60}")
    print(f"  VERIFICATION: Testing knowledge retrieval")
    print(f"{'='*60}")

    from app.services.scraper import search_knowledge

    test_queries = [
        "MBA CAT online coaching fees",
        "CLAT law program price",
        "IPM BBA classroom batch",
        "GMAT online live course cost",
    ]

    for query in test_queries:
        results = search_knowledge(query=query, company_slug=slug, k=1)
        status  = "✅" if results else "❌"
        preview = results[0][:120].replace('\n', ' ') + "..." if results else "No results"
        print(f"\n  {status} Query: \"{query}\"")
        print(f"     → {preview}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
async def main(slug: str):
    print(f"""
╔══════════════════════════════════════════════════════════╗
║       CAREER LAUNCHER AI — TRAINING PIPELINE            ║
║       Company Slug: {slug:<36}║
╚══════════════════════════════════════════════════════════╝
""")

    start = datetime.utcnow()

    # Step 1: AI Persona
    ok1 = set_ai_persona(slug)
    if not ok1:
        print("\n⚠️  Skipping persona (company may not exist yet). Continuing with scrape.\n")

    # Step 2: Register URLs
    ok2 = register_kb_urls(slug)
    if not ok2:
        print("\n⚠️  Skipping URL registration. Continuing with scrape.\n")

    # Step 3: Scrape
    scrape_result = await scrape_all_urls(slug)
    total = scrape_result["total_chunks"]
    results = scrape_result["results"]

    # Verification
    if total > 0:
        verify_training(slug)

    # Summary
    elapsed = (datetime.utcnow() - start).seconds
    success = sum(1 for r in results.values() if r.get("ok"))

    print(f"""
╔══════════════════════════════════════════════════════════╗
║                   TRAINING COMPLETE                     ║
╠══════════════════════════════════════════════════════════╣
║  URLs scraped:    {success}/{len(TRAINING_URLS)} successful{" "*33}║
║  Total chunks:    {total} knowledge chunks stored{" "*18}║
║  Time taken:      {elapsed}s{" "*42}║
╠══════════════════════════════════════════════════════════╣
║  NEXT STEPS:                                            ║
║  1. Your AI will now answer with accurate CL data       ║
║  2. Re-run this script whenever prices/programs update  ║
║  3. API: POST /companies/{slug}/scrape to re-scrape   ║
╚══════════════════════════════════════════════════════════╝
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train Career Launcher AI CRM")
    parser.add_argument(
        "--slug",
        default=COMPANY_SLUG,
        help=f"Company slug in your CRM (default: {COMPANY_SLUG})"
    )
    args = parser.parse_args()

    asyncio.run(main(slug=args.slug))
