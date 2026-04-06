from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.security import get_current_user, require_roles, hash_password
from app.services.database import get_db
from app.db.company_models import User

router = APIRouter(tags=["Users"])

class UserCreate(BaseModel):
    email: str
    name: str
    role: str = "sales"
    password: str = "changeme"
    region: str | None = None
    industry: str | None = None

@router.get("/users")
def list_users(db: Session = Depends(get_db), me=Depends(require_roles("admin", "manager", "super_admin"))):
    users = db.query(User).order_by(User.created_at.desc()).all()
    return {"users": [{"id": u.id, "email": u.email, "name": u.name, "role": u.role,
                       "region": u.region, "industry": u.industry, "is_active": bool(u.is_active)} for u in users]}

@router.post("/users")
def create_user(payload: UserCreate, db: Session = Depends(get_db), me=Depends(require_roles("admin", "super_admin"))):
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(400, "Email already exists")
    u = User(email=payload.email, name=payload.name, role=payload.role,
             password_hash=hash_password(payload.password),
             region=payload.region, industry=payload.industry, is_active=1)
    db.add(u)
    db.commit()
    db.refresh(u)
    return {"user": {"id": u.id, "email": u.email, "name": u.name, "role": u.role}}

@router.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), me=Depends(require_roles("admin", "super_admin"))):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    u.is_active = 0
    db.commit()
    return {"ok": True}
