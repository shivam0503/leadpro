"""
core/security.py — Multi-tenant JWT auth
─────────────────────────────────────────
JWT payload structure:
  {
    "sub": "user@email.com",
    "role": "admin",                  # admin|manager|sales
    "company_slug": "careerlauncher", # which company DB to use
    "is_super": false,                # true = super admin
    "exp": ...
  }

Super admin token:
  {
    "sub": "superadmin@leadpro.ai",
    "role": "super_admin",
    "is_super": true,
    "company_slug": null
  }
"""

from __future__ import annotations
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import bcrypt
from jose import jwt, JWTError
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.core.config import settings

bearer_scheme = HTTPBearer(auto_error=False)


# ── Password hashing (direct bcrypt, bypasses passlib's broken bcrypt backend) ─

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw[:72].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw[:72].encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ── JWT ────────────────────────────────────────────────────────────────────────

def create_access_token(data: Dict[str, Any], expires_minutes: Optional[int] = None) -> str:
    to_encode = dict(data)
    expire = datetime.utcnow() + timedelta(
        minutes=expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALG)


def decode_token(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALG])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# ── Token extraction ───────────────────────────────────────────────────────────

def _extract_token(request: Request, creds: HTTPAuthorizationCredentials | None) -> str | None:
    if creds and creds.credentials:
        return creds.credentials
    # Also check Authorization header directly (for webhooks)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


# ── Token payload ──────────────────────────────────────────────────────────────

class TokenPayload:
    """Parsed JWT payload."""
    def __init__(self, data: dict):
        self.sub: str = data.get("sub", "")
        self.role: str = data.get("role", "sales")
        self.company_slug: str | None = data.get("company_slug")
        self.is_super: bool = data.get("is_super", False)

    @property
    def is_admin(self) -> bool:
        return self.role in ("admin", "super_admin") or self.is_super


def get_token_payload(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> TokenPayload:
    """Extract and validate JWT. Injects company_slug into request.state."""
    token = _extract_token(request, creds)

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = decode_token(token)
    tp = TokenPayload(payload)

    # Inject company slug into request state for get_db() to pick up
    if tp.company_slug:
        request.state.company_slug = tp.company_slug

    return tp


# ── User resolution ────────────────────────────────────────────────────────────

def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
):
    """
    Returns the current user object from the company DB.
    Also validates the user still exists and is active.
    """
    tp = get_token_payload(request, creds)

    if tp.is_super:
        # Super admin — return a mock user object
        class SuperUser:
            id = 0
            email = tp.sub
            name = "Super Admin"
            role = "super_admin"
            is_active = 1
            company_slug = None
        return SuperUser()

    # Regular company user — look up in company DB
    from app.services.database import _company_sessions
    slug = tp.company_slug
    if not slug or slug not in _company_sessions:
        raise HTTPException(status_code=401, detail="Company not found")

    from app.db.company_models import User
    session_factory = _company_sessions[slug]
    db = session_factory()
    try:
        u = db.query(User).filter(User.email == tp.sub).first()
        if not u or not u.is_active:
            raise HTTPException(status_code=401, detail="User inactive or not found")
        u.company_slug = slug  # attach for downstream use
        return u
    finally:
        db.close()


# ── Role guards ────────────────────────────────────────────────────────────────

def require_roles(*roles: str):
    def _dep(user=Depends(get_current_user)):
        if getattr(user, "role", "") not in roles and getattr(user, "role", "") != "super_admin":
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return _dep


def require_admin(user=Depends(get_current_user)):
    if user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def require_super_admin(user=Depends(get_current_user)):
    if not getattr(user, "role", "") == "super_admin":
        raise HTTPException(status_code=403, detail="Super admin only")
    return user