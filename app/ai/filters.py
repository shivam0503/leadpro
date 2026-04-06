from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class FilterContext:
    lead: Dict[str, Any]
    inbound_text: Optional[str]
    extracted: Dict[str, Any]
    now: datetime


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def match_filters(filters: Dict[str, Any], ctx: FilterContext) -> bool:
    """Return True if ctx matches ALL provided filters.

    Supported filters:
      - lead_source: str|list[str]
      - industry: str|list[str]
      - intent: str|list[str]
      - confidence_gte: float
      - budget_gte: float|int
      - engagement_gte: int
      - region: str|list[str]
      - language: str|list[str]
      - time_hour_between: [start,end] in 0-23 (UTC)
      - status_in: list[str]
    """
    f = filters or {}
    lead = ctx.lead or {}
    ex = ctx.extracted or {}

    def _in_list(val: Any, allowed: Any) -> bool:
        if allowed is None:
            return True
        if isinstance(allowed, (list, tuple, set)):
            return _norm(val) in {_norm(x) for x in allowed}
        return _norm(val) == _norm(allowed)

    if not _in_list(lead.get("source"), f.get("lead_source")):
        return False
    if not _in_list(ex.get("industry") or lead.get("industry"), f.get("industry")):
        return False
    if not _in_list(ex.get("intent"), f.get("intent")):
        return False
    if not _in_list(ex.get("region") or lead.get("region"), f.get("region")):
        return False
    if not _in_list(ex.get("language") or lead.get("language"), f.get("language")):
        return False

    status_in = f.get("status_in")
    if status_in and _norm(lead.get("status")) not in {_norm(x) for x in status_in}:
        return False

    conf_gte = f.get("confidence_gte")
    if conf_gte is not None:
        try:
            if float(ex.get("confidence") or 0.0) < float(conf_gte):
                return False
        except Exception:
            return False

    budget_gte = f.get("budget_gte")
    if budget_gte is not None:
        try:
            if float(ex.get("budget") or 0.0) < float(budget_gte):
                return False
        except Exception:
            return False

    engagement_gte = f.get("engagement_gte")
    if engagement_gte is not None:
        try:
            if int(lead.get("engagement") or 0) < int(engagement_gte):
                return False
        except Exception:
            return False

    hour_between = f.get("time_hour_between")
    if hour_between and isinstance(hour_between, (list, tuple)) and len(hour_between) == 2:
        try:
            start, end = int(hour_between[0]), int(hour_between[1])
            h = int(ctx.now.hour)
            if start <= end:
                if not (start <= h <= end):
                    return False
            else:
                # wrap around midnight
                if not (h >= start or h <= end):
                    return False
        except Exception:
            return False

    return True
