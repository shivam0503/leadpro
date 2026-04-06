"""
services/database.py
─────────────────────
Multi-tenant DB manager.

Architecture:
  master.db          → companies, super_admins, invites
  dbs/{slug}.db      → per-company isolated DB (leads, users, notes, etc.)

Usage:
  # Get master DB session
  db = get_master_db()

  # Get company DB session
  db = get_company_db("careerlauncher")

  # FastAPI dependency (reads company slug from request)
  db: Session = Depends(get_db)
"""

from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache
from typing import Generator

from fastapi import Depends, HTTPException, Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from app.db.master_models import MasterBase, Company
from app.db.company_models import CompanyBase

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parents[2]
MASTER_DB_PATH = BASE_DIR / "master.db"
COMPANY_DBS_DIR = BASE_DIR / "dbs"
COMPANY_DBS_DIR.mkdir(exist_ok=True)


# ── Master DB ─────────────────────────────────────────────────────────────────

_master_engine = create_engine(
    f"sqlite:///{MASTER_DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False,
)

_MasterSession = sessionmaker(bind=_master_engine, autoflush=False, autocommit=False)


def init_master_db():
    """Create master DB tables if they don't exist."""
    MasterBase.metadata.create_all(bind=_master_engine)


def get_master_db() -> Generator[Session, None, None]:
    db = _MasterSession()
    try:
        yield db
    finally:
        db.close()


# ── Per-Company DB ────────────────────────────────────────────────────────────

# Cache engines per company slug (avoid re-creating)
_company_engines: dict[str, object] = {}
_company_sessions: dict[str, sessionmaker] = {}


def get_company_db_path(slug: str) -> Path:
    return COMPANY_DBS_DIR / f"{slug}.db"


def _get_company_engine(slug: str):
    if slug not in _company_engines:
        db_path = get_company_db_path(slug)
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            echo=False,
        )
        # Auto-create tables for this company DB
        CompanyBase.metadata.create_all(bind=engine)
        _company_engines[slug] = engine
        _company_sessions[slug] = sessionmaker(
            bind=engine, autoflush=False, autocommit=False
        )
    return _company_engines[slug]


def init_company_db(slug: str) -> str:
    """
    Initialize a new company DB. Creates the file and all tables.
    Returns the DB path.
    """
    _get_company_engine(slug)
    return str(get_company_db_path(slug))


def get_company_session(slug: str) -> Generator[Session, None, None]:
    """Direct company session — use in non-request contexts."""
    _get_company_engine(slug)
    session_factory = _company_sessions[slug]
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


# ── FastAPI Dependencies ──────────────────────────────────────────────────────

def get_db(request: Request) -> Generator[Session, None, None]:
    """
    FastAPI dependency — resolves correct company DB from request.
    Company slug comes from:
      1. JWT token payload (company_slug claim)
      2. X-Company-Slug header (fallback for dev)
      3. Query param ?company (dev only)
    """
    slug = getattr(request.state, "company_slug", None)

    if not slug:
        slug = request.headers.get("X-Company-Slug")

    if not slug:
        slug = request.query_params.get("company")

    if not slug:
        raise HTTPException(
            status_code=400,
            detail="Company not identified. Include X-Company-Slug header or valid JWT."
        )

    _get_company_engine(slug)
    session_factory = _company_sessions[slug]
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


# Keep backward compat — old code imports Base from here
Base = CompanyBase


# ── Startup ───────────────────────────────────────────────────────────────────

def startup():
    """Call on app startup to init master DB."""
    init_master_db()
    # Re-init engines for all existing companies
    master_db = _MasterSession()
    try:
        companies = master_db.query(Company).filter(Company.is_active == True).all()
        for company in companies:
            try:
                _get_company_engine(company.slug)
            except Exception as e:
                print(f"Warning: Could not init DB for {company.slug}: {e}")
    finally:
        master_db.close()
