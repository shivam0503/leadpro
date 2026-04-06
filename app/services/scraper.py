from __future__ import annotations

import re
from datetime import datetime
from typing import List
from urllib.parse import urljoin, urlparse

from loguru import logger

from playwright.async_api import async_playwright

try:
    from bs4 import BeautifulSoup
    BS4_OK = True
except ImportError:
    BS4_OK = False
    logger.warning("beautifulsoup4 not installed — scraper will use basic text extraction")


# =========================
# CONFIG
# =========================
CHUNK_SIZE = 400          # words per chunk — larger = more context per retrieval
MAX_PAGES  = 4            # we only scrape the 4 specific CL URLs, no crawling
MAX_DEPTH  = 0            # no auto-crawl; URLs are passed explicitly


# =========================
# PRICE NORMALISER
# Career Launcher shows prices like "19799.0" or " 34999.0 "
# =========================
def _format_price(raw: str) -> str:
    try:
        val = float(raw.strip())
        return f"Rs.{int(val):,}"
    except Exception:
        return raw.strip()


# =========================
# CLEAN TEXT — CL-specific
# Strips nav/footer noise, keeps product cards + price blocks
# =========================
def _clean_text(html: str) -> str:
    if not BS4_OK:
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        return re.sub(r'\s+', ' ', text).strip()

    soup = BeautifulSoup(html, "html.parser")

    # Remove noise elements
    for tag in soup(["script", "style", "noscript", "iframe", "nav", "footer", "header"]):
        tag.decompose()

    for modal in soup.find_all("div", class_=re.compile(r"modal|popup|login|signup", re.I)):
        modal.decompose()

    # Extract page title
    title = soup.title.string.strip() if soup.title else ""

    # Extract structured content
    content_blocks = []

    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5"]):
        txt = h.get_text(strip=True)
        if txt:
            content_blocks.append(f"\n## {txt}")

    for ol in soup.find_all("ol"):
        items = [li.get_text(strip=True) for li in ol.find_all("li") if li.get_text(strip=True)]
        if items:
            content_blocks.append("\n" + "\n".join(f"- {i}" for i in items))

    for ul in soup.find_all("ul"):
        items = [li.get_text(strip=True) for li in ul.find_all("li") if li.get_text(strip=True)]
        if items:
            content_blocks.append("\n" + "\n".join(f"* {i}" for i in items))

    # Full text with price normalisation
    full_text = soup.get_text(separator=" ", strip=True)
    full_text = re.sub(
        r'\b(\d{4,6}\.0)\b',
        lambda m: _format_price(m.group(1)),
        full_text
    )

    structured = "\n".join(content_blocks).strip()
    combined   = f"{title}\n\n{structured}\n\n{full_text}" if structured else f"{title}\n\n{full_text}"
    combined   = re.sub(r'\n{3,}', '\n\n', combined)
    combined   = re.sub(r' {2,}', ' ', combined)

    return combined.strip()


# =========================
# CHUNK TEXT
# =========================
def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> List[str]:
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size):
        chunk = " ".join(words[i:i + chunk_size]).strip()
        if chunk:
            chunks.append(chunk)
    return chunks


# =========================
# GET LINKS
# =========================
def _get_links(html: str, base_url: str) -> List[str]:
    if not BS4_OK:
        return []
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc
    allowed_keywords = ["course", "program", "mba", "bba", "study", "online",
                        "law", "clat", "ipm", "gre", "gmat", "cuet"]
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)
        if parsed.netloc == base_domain and parsed.scheme in ("http", "https"):
            if any(k in full_url.lower() for k in allowed_keywords):
                if not any(ext in parsed.path for ext in [".pdf", ".jpg", ".png", ".css", ".js"]):
                    links.append(full_url.split("#")[0])
    return list(set(links))


# =========================
# FETCH — Playwright with JS execution
# Career Launcher pages are JSP/dynamic; we wait for full render
# =========================
async def _fetch_url(url: str) -> tuple[str, str]:
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, channel="chrome")
        except Exception:
            browser = await p.chromium.launch(
                headless=True,
                executable_path="C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
            )

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Block images/fonts to speed up scraping
        await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot}",
                         lambda route: route.abort())

        await page.goto(url, timeout=90000, wait_until="domcontentloaded")

        # Wait for product cards and price elements to render
        try:
            await page.wait_for_selector("ol li, ul li, h3, h4", timeout=10000)
        except Exception:
            pass

        await page.wait_for_timeout(3500)

        content   = await page.content()
        final_url = page.url

        await browser.close()
        return content, final_url


# =========================
# MAIN SCRAPER
# =========================
async def scrape_and_store(url: str, company_slug: str, crawl: bool = False) -> int:
    """
    Scrape a URL and store knowledge chunks in the company DB.

    For Career Launcher, call with crawl=False and pass each product URL:
      - MBA:  .../product-category.jsp?prodCat=MBA
      - LAW:  .../product-category.jsp?prodCat=LST
      - IPM:  .../product-category.jsp?prodCat=AFTER-12
      - GMAT: .../product-category.jsp?prodCat=GMAT
    """
    from app.services.database import _get_company_engine, _company_sessions
    from app.db.company_models import KnowledgeChunk

    _get_company_engine(company_slug)
    session_factory = _company_sessions[company_slug]

    visited      = set()
    to_visit     = [(url, 0)]
    total_chunks = 0

    while to_visit and len(visited) < MAX_PAGES:
        current_url, depth = to_visit.pop(0)

        if current_url in visited:
            continue
        visited.add(current_url)

        try:
            logger.info(f"[{company_slug}] Scraping: {current_url}")

            html, final_url = await _fetch_url(current_url)

            if BS4_OK:
                soup  = BeautifulSoup(html, "html.parser")
                title = soup.title.string.strip() if soup.title else current_url
            else:
                title = current_url

            text = _clean_text(html)

            if len(text) < 200:
                logger.warning(f"Skipping near-empty page: {final_url}")
                continue

            chunks = _chunk_text(text)

            db = session_factory()
            try:
                # Delete old chunks for this URL before re-inserting
                db.query(KnowledgeChunk).filter(
                    KnowledgeChunk.url == final_url
                ).delete()

                for i, chunk in enumerate(chunks):
                    db.add(KnowledgeChunk(
                        url         = final_url,
                        title       = title,
                        content     = chunk,
                        chunk_index = i,
                        scraped_at  = datetime.utcnow(),
                    ))

                db.commit()
                total_chunks += len(chunks)
                logger.info(f"✅ Stored {len(chunks)} chunks from {final_url}")

            finally:
                db.close()

            if crawl and depth < MAX_DEPTH:
                links = _get_links(html, final_url)
                for link in links[:5]:
                    if link not in visited:
                        to_visit.append((link, depth + 1))

        except Exception as e:
            logger.error(f"❌ Failed: {current_url} → {e}")
            continue

    logger.info(f"🔥 DONE: {len(visited)} pages, {total_chunks} chunks stored for [{company_slug}]")
    return total_chunks


# =========================
# SEARCH KNOWLEDGE BASE
# =========================
def search_knowledge(query: str, company_slug: str, k: int = 6) -> List[str]:
    """
    Retrieve top-k knowledge chunks relevant to the query.
    Uses keyword frequency scoring with CL domain boosting.
    """
    from app.services.database import _get_company_engine, _company_sessions
    from app.db.company_models import KnowledgeChunk

    _get_company_engine(company_slug)
    session_factory = _company_sessions[company_slug]
    db = session_factory()

    try:
        words = re.sub(r'[^\w\s]', '', query.lower()).split()

        # Boost domain-relevant terms for better retrieval
        BOOST_KEYWORDS = {
            "fee", "fees", "price", "cost", "pricing",
            "mba", "cat", "clat", "law", "ipm", "bba", "gmat", "gre", "cuet",
            "online", "classroom", "test", "series", "mock", "batch",
            "enroll", "course", "program", "coaching",
        }

        chunks = db.query(KnowledgeChunk).all()

        scored = []
        for chunk in chunks:
            content_lower = chunk.content.lower()
            score = 0
            for w in words:
                count = content_lower.count(w)
                boost = 2 if w in BOOST_KEYWORDS else 1
                score += count * boost
            if score > 0:
                scored.append((score, chunk.content))

        scored.sort(reverse=True)
        return [c for _, c in scored[:k]]

    finally:
        db.close()
