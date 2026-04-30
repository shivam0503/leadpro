"""
Master Registry DB Models
─────────────────────────
This DB (master.db) stores:
  - Companies (tenants)
  - Super admin users
  - Billing / plan info

Each company gets its own isolated DB file: dbs/{slug}.db
"""

from datetime import datetime
from sqlalchemy import String, Integer, DateTime, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase


class MasterBase(DeclarativeBase):
    pass


class Company(MasterBase):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    domain: Mapped[str | None] = mapped_column(String(200), nullable=True)      # e.g. careerlauncher.com
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)    # edtech, realestate, etc.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Plan
    plan: Mapped[str] = mapped_column(String(50), default="trial")              # trial|starter|growth|enterprise
    max_users: Mapped[int] = mapped_column(Integer, default=5)
    max_leads: Mapped[int] = mapped_column(Integer, default=500)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # WhatsApp config (per-company)
    wa_provider: Mapped[str] = mapped_column(String(20), default="twilio")
    wa_phone_from: Mapped[str | None] = mapped_column(String(50), nullable=True)
    wa_account_sid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    wa_auth_token: Mapped[str | None] = mapped_column(String(100), nullable=True)
    wa_demo_mode: Mapped[bool] = mapped_column(Boolean, default=True)

    # LeadSquared config (per-company)
    lsq_access_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lsq_secret_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lsq_host: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lsq_owner_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lsq_demo_mode: Mapped[bool] = mapped_column(Boolean, default=True)

    # AI config
    openai_api_key: Mapped[str | None] = mapped_column(String(200), nullable=True)  # overrides global
    ai_persona: Mapped[str | None] = mapped_column(Text, nullable=True)             # custom AI personality
    ai_language: Mapped[str] = mapped_column(String(20), default="hinglish")        # hinglish|english|hindi|arabic

    # Knowledge base (scraped URLs stored here as JSON)
    kb_urls_json: Mapped[str] = mapped_column(Text, default="[]")
    kb_last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # DB path
    db_path: Mapped[str | None] = mapped_column(String(300), nullable=True)         # dbs/{slug}.db

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SuperAdmin(MasterBase):
    __tablename__ = "super_admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150), default="Super Admin")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CompanyInvite(MasterBase):
    __tablename__ = "company_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(30), default="sales")
    token: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
