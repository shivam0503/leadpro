"""
api/v1/auth.py — Multi-tenant authentication
─────────────────────────────────────────────
POST /auth/login          — company user login (requires company_slug)
POST /auth/super-login    — super admin login
POST /auth/bootstrap      — create first super admin (run once)
GET  /auth/me             — current user info
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.security import (
    verify_password, create_access_token, hash_password,
    get_current_user, bearer_scheme
)
from app.services.database import get_master_db
from app.db.master_models import SuperAdmin, Company

router = APIRouter(tags=["Auth"])


class LoginIn(BaseModel):
    email: str
    password: str
    company_slug: str          # required for company users


class SuperLoginIn(BaseModel):
    email: str
    password: str


class BootstrapIn(BaseModel):
    email: str = "superadmin@leadpro.ai"
    password: str = "LeadPro@2024!"
    name: str = "Super Admin"


@router.post("/auth/login")
def login(payload: LoginIn, master_db: Session = Depends(get_master_db)):
    """Company user login. Returns JWT with company_slug embedded."""

    # Verify company exists and is active
    company = master_db.query(Company).filter(
        Company.slug == payload.company_slug,
        Company.is_active == True,
    ).first()
    if not company:
        raise HTTPException(401, "Company not found or inactive")

    # Look up user in company DB
    from app.services.database import _get_company_engine, _company_sessions
    from app.db.company_models import User

    _get_company_engine(payload.company_slug)
    session_factory = _company_sessions[payload.company_slug]
    company_db = session_factory()

    try:
        u = company_db.query(User).filter(User.email == payload.email).first()
        if not u or not verify_password(payload.password, u.password_hash):
            raise HTTPException(401, "Invalid credentials")
        if not u.is_active:
            raise HTTPException(403, "Account deactivated")

        token = create_access_token({
            "sub": u.email,
            "role": u.role,
            "company_slug": payload.company_slug,
            "is_super": False,
        })

        return {
            "access_token": token,
            "token_type": "bearer",
            "user": {
                "id": u.id,
                "email": u.email,
                "name": u.name,
                "role": u.role,
                "company_slug": payload.company_slug,
                "company_name": company.name,
            }
        }
    finally:
        company_db.close()


@router.post("/auth/super-login")
def super_login(payload: SuperLoginIn, db: Session = Depends(get_master_db)):
    """Super admin login — gets access to all companies."""
    admin = db.query(SuperAdmin).filter(SuperAdmin.email == payload.email).first()
    if not admin or not verify_password(payload.password, admin.password_hash):
        raise HTTPException(401, "Invalid super admin credentials")
    if not admin.is_active:
        raise HTTPException(403, "Super admin account deactivated")

    token = create_access_token({
        "sub": admin.email,
        "role": "super_admin",
        "is_super": True,
        "company_slug": None,
    })

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": admin.id,
            "email": admin.email,
            "name": admin.name,
            "role": "super_admin",
            "is_super": True,
        }
    }


@router.post("/auth/bootstrap")
def bootstrap(payload: BootstrapIn, db: Session = Depends(get_master_db)):
    """Create the first super admin. Idempotent — safe to call multiple times."""
    existing = db.query(SuperAdmin).filter(SuperAdmin.email == payload.email).first()
    if existing:
        return {"ok": True, "message": "Super admin already exists", "email": payload.email}

    admin = SuperAdmin(
        email=payload.email,
        name=payload.name,
        password_hash=hash_password(payload.password),
        is_active=True
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)

    return {
        "ok": True,
        "message": "Super admin created!",
        "email": admin.email,
        "login": {
            "endpoint": "POST /api/v1/auth/super-login",
            "body": {"email": payload.email, "password": payload.password}
        }
    }


@router.get("/auth/me")
def me(user=Depends(get_current_user)):
    """Returns current user info from JWT."""
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "company_slug": getattr(user, "company_slug", None),
        "is_super": user.role == "super_admin",
    }

