from __future__ import annotations
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.core.security import get_current_user
from app.services.database import get_db
from app.db.company_models import Lead

router = APIRouter(tags=["Dashboard"])


@router.get("/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db), me=Depends(get_current_user)):
    total = db.query(func.count(Lead.id)).scalar() or 0
    hot = db.query(func.count(Lead.id)).filter(Lead.score >= 40).scalar() or 0
    warm = db.query(func.count(Lead.id)).filter(Lead.score.between(20, 39)).scalar() or 0
    cold = db.query(func.count(Lead.id)).filter(Lead.score < 20).scalar() or 0
    closed = db.query(func.count(Lead.id)).filter(Lead.status == "closed").scalar() or 0
    conversion_pct = round((closed / total) * 100, 2) if total else 0.0

    return {
        "total_leads": int(total),
        "hot_leads": int(hot),
        "warm_leads": int(warm),
        "cold_leads": int(cold),
        "conversion_pct": conversion_pct,
        "company_slug": getattr(me, "company_slug", None),
    }
