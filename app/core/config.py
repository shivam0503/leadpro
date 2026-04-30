"""
core/config.py — Multi-tenant settings
"""
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)


class Settings:
    APP_NAME = os.getenv("APP_NAME", "LeadPro AI CRM")
    ENV = os.getenv("ENV", "dev")

    # LLM (global fallback — companies can override)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

    # Super admin
    SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL", "superadmin@leadpro.ai")
    SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", "LeadPro@2024!")

    # JWT
    JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-prod-32chars-minimum!")
    JWT_ALG = os.getenv("JWT_ALG", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "720"))

    # Auth mode
    DISABLE_AUTH = os.getenv("DISABLE_AUTH", "false").lower() in {"1", "true", "yes"}

    # Global WhatsApp fallback
    WHATSAPP_PROVIDER = os.getenv("WHATSAPP_PROVIDER", "twilio")
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
    TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    WHATSAPP_DEMO_MODE = os.getenv("WHATSAPP_DEMO_MODE", "true").lower() in {"1", "true"}

    # Global LeadSquared fallback
    LEADSQUARED_ACCESS_KEY = os.getenv("LEADSQUARED_ACCESS_KEY", "")
    LEADSQUARED_SECRET_KEY = os.getenv("LEADSQUARED_SECRET_KEY", "")
    LEADSQUARED_HOST = os.getenv("LEADSQUARED_HOST", "api-in21.leadsquared.com")
    LEADSQUARED_OWNER_ID = os.getenv("LEADSQUARED_OWNER_ID", "")
    LEADSQUARED_DEMO_MODE = os.getenv("LEADSQUARED_DEMO_MODE", "false").lower() in {"1", "true"}

    # Base URL (for webhooks)
    BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

    # App prefix for reverse-proxy deployments (e.g. "/whatsapp-crm")
    APP_PREFIX = os.getenv("APP_PREFIX", "")

    # Helper method (accessed as settings.is_openai_configured())
    def is_openai_configured(self):
        return bool(self.OPENAI_API_KEY and len(self.OPENAI_API_KEY) > 10)


settings = Settings()