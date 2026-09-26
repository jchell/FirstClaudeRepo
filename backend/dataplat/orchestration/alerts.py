"""Operational alerts: stored in the metadata database and published on the bus.

Freshness SLAs are evaluated by the scheduler; failing streams are reported by the
stream worker. An alert stays open until its condition clears.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from dataplat.core.context import PlatformContext
from dataplat.db.models import Alert, Dataset

log = logging.getLogger(__name__)

ALERTS_TOPIC = "dataplat.alerts"


def _publish(ctx: PlatformContext, event: dict) -> None:
    try:
        ctx.events.publish(ALERTS_TOPIC, event, key=event.get("target"))
        ctx.events.flush(5)
    except Exception:
        log.warning("could not publish alert event", exc_info=True)


def open_alert(
    ctx: PlatformContext, kind: str, target: str, message: str, severity: str = "warning", details: dict | None = None
) -> bool:
    """Opens an alert unless one is already open for (kind, target). Returns True if new."""
    with ctx.metadata.session() as s:
        existing = s.scalars(
            select(Alert).where(Alert.kind == kind, Alert.target == target, Alert.resolved_at.is_(None))
        ).first()
        if existing is not None:
            existing.message = message
            return False
        alert = Alert(kind=kind, target=target, message=message[:2000], severity=severity, details=details or {})
        s.add(alert)
        s.flush()
        alert_id = alert.id
    try:
        # Notification channels (webhook, email) are served by the worker.
        ctx.jobs.enqueue("alert.notify", {"alert_id": alert_id}, max_attempts=3)
    except Exception:
        log.warning("could not queue alert notifications", exc_info=True)
    _publish(
        ctx, {"type": "alert.opened", "kind": kind, "target": target, "severity": severity, "message": message[:500]}
    )
    return True


def resolve_alert(ctx: PlatformContext, kind: str, target: str) -> bool:
    with ctx.metadata.session() as s:
        rows = list(
            s.scalars(select(Alert).where(Alert.kind == kind, Alert.target == target, Alert.resolved_at.is_(None)))
        )
        for a in rows:
            a.resolved_at = datetime.now(UTC)
    if rows:
        _publish(ctx, {"type": "alert.resolved", "kind": kind, "target": target})
    return bool(rows)


def check_freshness(ctx: PlatformContext) -> dict[str, int]:
    """Opens/resolves freshness alerts for datasets with an SLA."""
    now = datetime.now(UTC)
    opened = resolved = 0
    with ctx.metadata.session() as s:
        datasets = [
            (f"{d.layer}.{d.name}", d.freshness_sla_minutes, d.last_loaded_at)
            for d in s.scalars(select(Dataset).where(Dataset.freshness_sla_minutes.is_not(None)))
        ]
    for target, sla, last in datasets:
        age = (now - last) if last else None
        if age is None or age > timedelta(minutes=sla):
            late = f"{int(age.total_seconds() // 60)} min" if age else "never loaded"
            if open_alert(
                ctx,
                "freshness",
                target,
                f"{target} is stale: last load {late} ago, SLA {sla} min",
                details={"sla_minutes": sla, "last_loaded_at": last.isoformat() if last else None},
            ):
                opened += 1
        elif resolve_alert(ctx, "freshness", target):
            resolved += 1
    return {"opened": opened, "resolved": resolved}
