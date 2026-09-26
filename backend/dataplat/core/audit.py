"""Audit trail: who did what, to what, and whether it worked. Never holds secret values."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from dataplat.db.models import AuditLog

log = logging.getLogger("dataplat.audit")


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    target: str | None = None,
    outcome: str = "success",
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    session.add(AuditLog(actor=actor, action=action, target=target, outcome=outcome, detail=detail or {}, ip=ip))
    log.info("audit actor=%s action=%s target=%s outcome=%s", actor, action, target, outcome)
