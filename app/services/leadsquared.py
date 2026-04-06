"""LeadSquared CRM integration service.

Pushes leads to LeadSquared via the Lead.Capture REST API.

Configuration (set in .env):
    LEADSQUARED_ACCESS_KEY  – Your LSQ Access Key
    LEADSQUARED_SECRET_KEY  – Your LSQ Secret Key
    LEADSQUARED_HOST        – Region host, e.g. api-in21.leadsquared.com (default)
    LEADSQUARED_OWNER_ID    – Lead owner GUID (optional)
    LEADSQUARED_DEMO_MODE   – Set to "true" to skip the real API call in dev/test
"""
from __future__ import annotations

from typing import Any, Optional

import httpx
from loguru import logger

from app.core.config import settings


def _build_url() -> str:
    host = (settings.LEADSQUARED_HOST or "api-in21.leadsquared.com").strip().rstrip("/")
    return f"https://{host}/v2/LeadManagement.svc/Lead.Capture"


def _build_payload(
    name: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    company: Optional[str] = None,
    city: Optional[str] = None,
    country: str = "India",
    source: str = "AI CRM",
    campaign: Optional[str] = None,
    utm_source: Optional[str] = None,
    utm_medium: Optional[str] = None,
    utm_campaign: Optional[str] = None,
    custom_fields: Optional[dict] = None,
) -> list:
    attrs = [
        {"Attribute": "FirstName",       "Value": name or ""},
        {"Attribute": "EmailAddress",    "Value": email or ""},
        {"Attribute": "Phone",           "Value": phone or ""},
        {"Attribute": "Company",         "Value": company or ""},
        {"Attribute": "City",            "Value": city or ""},
        {"Attribute": "Country",         "Value": country or "India"},
        {"Attribute": "Source",          "Value": source or "AI CRM"},
        {"Attribute": "mx_Campaign",     "Value": campaign or ""},
        {"Attribute": "mx_UTM_Source",   "Value": utm_source or ""},
        {"Attribute": "mx_UTM_Medium",   "Value": utm_medium or ""},
        {"Attribute": "mx_UTM_Campaign", "Value": utm_campaign or ""},
    ]

    owner_id = (settings.LEADSQUARED_OWNER_ID or "").strip()
    if owner_id:
        attrs.append({"Attribute": "OwnerId", "Value": owner_id})

    if custom_fields:
        for attr_name, attr_value in custom_fields.items():
            if attr_value:
                attrs.append({"Attribute": attr_name, "Value": str(attr_value)})

    return [item for item in attrs if item.get("Value")]


def send_to_leadsquared(
    name: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    company: Optional[str] = None,
    city: Optional[str] = None,
    country: str = "India",
    source: str = "AI CRM",
    campaign: Optional[str] = None,
    utm_source: Optional[str] = None,
    utm_medium: Optional[str] = None,
    utm_campaign: Optional[str] = None,
    custom_fields: Optional[dict] = None,
) -> dict:

    # ── Step 1: Print all config so you can see exactly what's loaded ──────────
    logger.info("=" * 60)
    logger.info("🔍 LEADSQUARED DEBUG — send_to_leadsquared() called")
    logger.info("  LEADSQUARED_DEMO_MODE  : {}", settings.LEADSQUARED_DEMO_MODE)
    logger.info("  LEADSQUARED_HOST       : {}", settings.LEADSQUARED_HOST)
    logger.info("  LEADSQUARED_ACCESS_KEY : {}", (settings.LEADSQUARED_ACCESS_KEY or "")[:8] + "..." if settings.LEADSQUARED_ACCESS_KEY else "❌ NOT SET")
    logger.info("  LEADSQUARED_SECRET_KEY : {}", "✅ SET" if settings.LEADSQUARED_SECRET_KEY else "❌ NOT SET")
    logger.info("  LEADSQUARED_OWNER_ID   : {}", settings.LEADSQUARED_OWNER_ID or "(not set)")
    logger.info("  Lead data → name={} email={} phone={} company={}", name, email, phone, company)
    logger.info("=" * 60)

    # ── Step 2: Demo mode check ─────────────────────────────────────────────────
    if settings.LEADSQUARED_DEMO_MODE:
        logger.warning("⚠️  LEADSQUARED_DEMO_MODE=true — NOT sending to LeadSquared. Set it to false in .env to go live.")
        return {
            "ok": True,
            "mode": "demo",
            "data": {"name": name, "email": email, "phone": phone},
        }

    # ── Step 3: Credentials check ───────────────────────────────────────────────
    access_key = (settings.LEADSQUARED_ACCESS_KEY or "").strip()
    secret_key = (settings.LEADSQUARED_SECRET_KEY or "").strip()

    if not access_key:
        logger.error("❌ LEADSQUARED_ACCESS_KEY is empty. Open .env and set it.")
        return {"ok": False, "mode": "config_error", "error": "LEADSQUARED_ACCESS_KEY is not set in .env"}

    if not secret_key:
        logger.error("❌ LEADSQUARED_SECRET_KEY is empty. Open .env and set it.")
        return {"ok": False, "mode": "config_error", "error": "LEADSQUARED_SECRET_KEY is not set in .env"}

    if access_key in ("YOUR_LSQ_ACCESS_KEY_HERE", "ffffffffffffffffffffffffffffffffff$"):
        logger.error("❌ LEADSQUARED_ACCESS_KEY still has placeholder value. Replace it with your real key from LeadSquared → Settings → API & Webhooks.")
        return {"ok": False, "mode": "config_error", "error": "LEADSQUARED_ACCESS_KEY is still a placeholder — update .env"}

    # ── Step 4: Build payload and log it ───────────────────────────────────────
    url = _build_url()
    payload = _build_payload(
        name=name, email=email, phone=phone,
        company=company, city=city, country=country,
        source=source, campaign=campaign,
        utm_source=utm_source, utm_medium=utm_medium,
        utm_campaign=utm_campaign, custom_fields=custom_fields,
    )
    headers = {
        "Content-Type": "application/json",
        "x-LSQ-AccessKey": access_key,
        "x-LSQ-SecretKey": secret_key,
    }

    logger.info("📤 Sending to LeadSquared URL: {}", url)
    logger.info("📦 Payload: {}", payload)

    # ── Step 5: Make the HTTP request ──────────────────────────────────────────
    try:
        with httpx.Client(timeout=12) as client:
            response = client.post(url, json=payload, headers=headers)

        logger.info("📥 LeadSquared HTTP status : {}", response.status_code)
        logger.info("📥 LeadSquared response    : {}", response.text[:500])

        if response.status_code == 401:
            logger.error("❌ 401 Unauthorized — Your ACCESS KEY or SECRET KEY is wrong. Check LeadSquared → Settings → API & Webhooks.")
            return {"ok": False, "mode": "live", "error": "401 Unauthorized — wrong API credentials"}

        if response.status_code == 403:
            logger.error("❌ 403 Forbidden — Your IP may be blocked or the key lacks permissions.")
            return {"ok": False, "mode": "live", "error": "403 Forbidden — check IP whitelist in LeadSquared"}

        if response.status_code >= 400:
            logger.error("❌ LeadSquared returned error {}: {}", response.status_code, response.text[:300])
            return {"ok": False, "mode": "live", "error": f"HTTP {response.status_code}: {response.text[:200]}"}

        logger.info("✅ Lead successfully sent to LeadSquared!")
        return {"ok": True, "mode": "live", "data": response.json()}

    except httpx.ConnectError as exc:
        logger.error("❌ Cannot connect to LeadSquared. Check LEADSQUARED_HOST in .env. Error: {}", exc)
        return {"ok": False, "mode": "live", "error": f"Connection failed — check LEADSQUARED_HOST: {exc}"}

    except httpx.TimeoutException as exc:
        logger.error("❌ LeadSquared request timed out after 12s. Their API may be slow or the host is wrong. Error: {}", exc)
        return {"ok": False, "mode": "live", "error": f"Timeout: {exc}"}

    except Exception as exc:
        logger.exception("❌ Unexpected error sending to LeadSquared: {}", exc)
        return {"ok": False, "mode": "live", "error": str(exc)}
