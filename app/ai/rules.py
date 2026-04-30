from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.ai.filters import FilterContext, match_filters
from app.db.models import AutomationRule


@dataclass
class RuleMatch:
    rule_id: int
    name: str
    priority: int
    filters: Dict[str, Any]
    actions: List[Dict[str, Any]]


def list_active_rules(db: Session) -> List[AutomationRule]:
    return (
        db.query(AutomationRule)
        .filter(AutomationRule.is_active == True)  # noqa: E712
        .order_by(AutomationRule.priority.desc(), AutomationRule.updated_at.desc())
        .all()
    )


def first_matching_rule(
    db: Session,
    ctx: FilterContext,
) -> Optional[RuleMatch]:
    for r in list_active_rules(db):
        try:
            filters = json.loads(r.filters_json or "{}")
        except Exception:
            filters = {}
        if not match_filters(filters, ctx):
            continue
        try:
            actions = json.loads(r.actions_json or "[]")
        except Exception:
            actions = []
        return RuleMatch(
            rule_id=r.id,
            name=r.name,
            priority=r.priority,
            filters=filters,
            actions=actions if isinstance(actions, list) else [],
        )
    return None
