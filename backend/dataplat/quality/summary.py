"""DQ read models: latest result per rule, dataset summaries."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dataplat.db.models import DqResult, DqRule
from dataplat.quality.rules import scorecard_score


def latest_results(s: Session, rule_ids: list[uuid.UUID]) -> dict[uuid.UUID, DqResult]:
    if not rule_ids:
        return {}
    sub = (
        select(DqResult.rule_id, func.max(DqResult.ts).label("ts"))
        .where(DqResult.rule_id.in_(rule_ids))
        .group_by(DqResult.rule_id)
        .subquery()
    )
    rows = s.scalars(select(DqResult).join(sub, (DqResult.rule_id == sub.c.rule_id) & (DqResult.ts == sub.c.ts)))
    return {r.rule_id: r for r in rows}


def dataset_dq(s: Session, dataset_id: uuid.UUID) -> dict[str, Any] | None:
    """Score and rule counts of a dataset's rules (None when it has no rules)."""
    rules = list(s.scalars(select(DqRule).where(DqRule.dataset_id == dataset_id, DqRule.enabled)))
    if not rules:
        return None
    latest = latest_results(s, [r.id for r in rules])
    score, dims = scorecard_score(s, [r.id for r in rules])
    return {
        "score": score,
        "dimensions": dims,
        "rules": len(rules),
        "failing": sum(1 for r in rules if r.id in latest and not latest[r.id].passed),
        "unevaluated": sum(1 for r in rules if r.id not in latest),
    }
