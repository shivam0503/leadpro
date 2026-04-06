"""
WhatsApp outbound service.

Supports two providers controlled by WHATSAPP_PROVIDER in .env:
  - "twilio"  → uses Twilio WhatsApp API (default)
  - "meta"    → uses Meta WhatsApp Cloud API

Set WHATSAPP_DEMO_MODE=true to skip real API calls in dev/test.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.core.config import settings


@dataclass
class WhatsAppSendResult:
    ok: bool
    mode: str
    response: dict[str, Any] | None = None
    error: str | None = None


def _send_via_twilio(to_phone: str, message: str) -> WhatsAppSendResult:
    """Send WhatsApp message via Twilio."""

    account_sid = (settings.TWILIO_ACCOUNT_SID or "").strip()
    auth_token = (settings.TWILIO_AUTH_TOKEN or "").strip()
    from_number = (settings.TWILIO_WHATSAPP_FROM or "whatsapp:+14155238886").strip()

    # ── Config guard ────────────────────────────────────────────────────────
    if not account_sid:
        logger.error("❌ TWILIO_ACCOUNT_SID is not set in .env")
        return WhatsAppSendResult(ok=False, mode="config_error", error="TWILIO_ACCOUNT_SID missing")

    if not auth_token:
        logger.error("❌ TWILIO_AUTH_TOKEN is not set in .env")
        return WhatsAppSendResult(ok=False, mode="config_error", error="TWILIO_AUTH_TOKEN missing")

    if account_sid.startswith("AC") is False or len(account_sid) < 10:
        logger.error("❌ TWILIO_ACCOUNT_SID looks invalid — should start with 'AC'")
        return WhatsAppSendResult(ok=False, mode="config_error", error="TWILIO_ACCOUNT_SID invalid format")

    # ── Format phone number ──────────────────────────────────────────────────
    # Twilio requires whatsapp:+91xxxxxxxxxx format
    if not to_phone.startswith("whatsapp:"):
        to_whatsapp = f"whatsapp:{to_phone}"
    else:
        to_whatsapp = to_phone

    logger.info("📤 Sending WhatsApp via Twilio → {} | from: {}", to_whatsapp, from_number)

    try:
        from twilio.rest import Client
        from twilio.http.http_client import TwilioHttpClient

        http_client = TwilioHttpClient()
        http_client.session.verify = False
        client = Client(account_sid, auth_token, http_client=http_client)

        msg = client.messages.create(
            from_=from_number,
            to=to_whatsapp,
            body=message,
        )

        logger.info("✅ Twilio WhatsApp sent! SID={} Status={}", msg.sid, msg.status)
        return WhatsAppSendResult(
            ok=True,
            mode="twilio",
            response={"sid": msg.sid, "status": msg.status, "to": to_whatsapp},
        )

    except Exception as exc:
        error_str = str(exc)
        logger.error("❌ Twilio WhatsApp failed: {}", error_str)

        # Friendly error hints
        if "21608" in error_str:
            logger.error("💡 Hint: The number {} has not joined your Twilio sandbox. Ask them to send 'join <sandbox-word>' to +14155238886", to_phone)
        elif "21211" in error_str:
            logger.error("💡 Hint: Invalid 'To' phone number format: {}", to_whatsapp)
        elif "20003" in error_str:
            logger.error("💡 Hint: Authentication failed — check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN")

        return WhatsAppSendResult(ok=False, mode="twilio", error=error_str)


def _send_via_meta(to_phone: str, message: str) -> WhatsAppSendResult:
    """Send WhatsApp message via Meta Cloud API."""
    import httpx

    if not settings.WHATSAPP_TOKEN or not settings.WHATSAPP_PHONE_NUMBER_ID:
        return WhatsAppSendResult(ok=False, mode="config_error", error="Missing WHATSAPP_TOKEN or WHATSAPP_PHONE_NUMBER_ID")

    url = f"https://graph.facebook.com/v20.0/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": message},
    }

    try:
        with httpx.Client(timeout=15) as client:
            r = client.post(url, headers=headers, json=payload)
            if r.status_code >= 400:
                return WhatsAppSendResult(ok=False, mode="meta", error=f"{r.status_code}: {r.text}")
            return WhatsAppSendResult(ok=True, mode="meta", response=r.json())
    except Exception as e:
        return WhatsAppSendResult(ok=False, mode="meta", error=str(e))


def send_text(to_phone: str, message: str) -> WhatsAppSendResult:
    """
    Send a WhatsApp text message.

    Provider is selected by WHATSAPP_PROVIDER in .env:
      - "twilio" (default)
      - "meta"

    If WHATSAPP_DEMO_MODE=true, skips real API and returns ok=True with mode='demo'.
    """
    to_phone = (to_phone or "").strip()
    message = (message or "").strip()

    if not to_phone or not message:
        return WhatsAppSendResult(ok=False, mode="invalid", error="missing_to_or_message")

    # ── Demo mode ────────────────────────────────────────────────────────────
    if settings.WHATSAPP_DEMO_MODE:
        logger.warning("⚠️  WHATSAPP_DEMO_MODE=true — skipping real WhatsApp send to {}", to_phone)
        return WhatsAppSendResult(
            ok=True,
            mode="demo",
            response={"to": to_phone, "message_preview": message[:200]},
        )

    provider = (settings.WHATSAPP_PROVIDER or "twilio").strip().lower()
    logger.info("📱 WhatsApp provider: {}", provider)

    if provider == "twilio":
        return _send_via_twilio(to_phone=to_phone, message=message)
    elif provider == "meta":
        return _send_via_meta(to_phone=to_phone, message=message)
    else:
        logger.error("❌ Unknown WHATSAPP_PROVIDER '{}' — use 'twilio' or 'meta'", provider)
        return WhatsAppSendResult(ok=False, mode="config_error", error=f"Unknown provider: {provider}")
