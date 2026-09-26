"""Admin: alert notification channels, and the Vault audit log.

Channel secrets (a webhook's URL, which usually embeds a token, and an optional bearer
token) are written to Vault and never returned; the channel keeps vault:// references.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataplat.api.deps import client_ip, get_ctx, get_session, require_roles
from dataplat.core import audit
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.core.secrets import WriteOnlySecret
from dataplat.db.models import NotificationChannel
from dataplat.orchestration.notify import CHANNEL_SECRETS

router = APIRouter(prefix="/api/admin", tags=["admin"])
admin_only = require_roles()  # admins only
VAULT_AUDIT_LOG = Path(os.environ.get("DATAPLAT_VAULT_AUDIT_LOG", "/vault-logs/audit.log"))
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ChannelIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    type: Literal["webhook", "email"]
    recipients: list[str] = []  # email
    kinds: list[str] = []  # empty: every alert kind
    min_severity: Literal["warning", "serious", "critical"] = "warning"
    enabled: bool = True
    # webhook: {"url": ..., "token": ... (optional)}; stored in Vault only
    secrets: dict[str, WriteOnlySecret] = {}


class ChannelOut(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    recipients: list[str]
    kinds: list[str]
    min_severity: str
    enabled: bool
    secret_fields: list[str]
    last_sent_at: Any
    last_error: str | None


def _out(ch: NotificationChannel) -> ChannelOut:
    return ChannelOut(
        id=ch.id,
        name=ch.name,
        type=ch.type,
        recipients=ch.config.get("recipients", []),
        kinds=ch.kinds,
        min_severity=ch.min_severity,
        enabled=ch.enabled,
        secret_fields=[k for k in CHANNEL_SECRETS[ch.type] if ch.config.get(k)],
        last_sent_at=ch.last_sent_at,
        last_error=ch.last_error,
    )


def _apply(ctx: PlatformContext, ch: NotificationChannel, body: ChannelIn) -> None:
    if body.type == "email":
        if not body.recipients:
            raise HTTPException(422, "email channels need recipients")
        if bad := [r for r in body.recipients if not EMAIL.match(r)]:
            raise HTTPException(422, f"not email addresses: {bad}")
    allowed = CHANNEL_SECRETS[body.type]
    if extra := [k for k in body.secrets if k not in allowed]:
        raise HTTPException(422, f"unknown secret fields {extra}")
    config: dict[str, Any] = {"recipients": body.recipients} if body.type == "email" else {}
    if body.type == "webhook":
        if body.secrets:
            url = body.secrets.get("url")
            if url is None:
                raise HTTPException(422, "send the webhook url (with the token, if any) when changing its secrets")
            if not url.get_secret_value().startswith(("https://", "http://")):
                raise HTTPException(422, "the webhook url must be http(s)")
            path = f"notifications/{ch.id}"
            ctx.secrets.write(path, {k: v.get_secret_value() for k, v in body.secrets.items()})
            for k in body.secrets:
                config[k] = str(ctx.secrets.ref(path, k))
        else:
            config.update({k: v for k, v in (ch.config or {}).items() if k in allowed})
        if not config.get("url"):
            raise HTTPException(422, "webhook channels need a url (sent as a secret)")
    ch.type, ch.config, ch.kinds = body.type, config, body.kinds
    ch.min_severity, ch.enabled = body.min_severity, body.enabled


@router.get("/notifications", response_model=list[ChannelOut])
def list_channels(s: Session = Depends(get_session, scope="function"), _: Principal = Depends(admin_only)):
    return [_out(c) for c in s.scalars(select(NotificationChannel).order_by(NotificationChannel.name))]


@router.post("/notifications", response_model=ChannelOut, status_code=201)
def create_channel(
    body: ChannelIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(admin_only),
):
    ch = NotificationChannel(id=uuid.uuid4(), name=body.name, type=body.type, created_by=actor.name)
    _apply(ctx, ch, body)
    s.add(ch)
    try:
        s.flush()
    except IntegrityError as e:
        raise HTTPException(409, "a channel with that name exists") from e
    audit.record(s, actor=actor.name, action="notifications.create", target=ch.name, ip=client_ip(request))
    return _out(ch)


@router.put("/notifications/{channel_id}", response_model=ChannelOut)
def update_channel(
    channel_id: uuid.UUID,
    body: ChannelIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(admin_only),
):
    ch = s.get(NotificationChannel, channel_id)
    if ch is None:
        raise HTTPException(404, "channel not found")
    ch.name = body.name
    _apply(ctx, ch, body)
    audit.record(s, actor=actor.name, action="notifications.update", target=ch.name, ip=client_ip(request))
    s.flush()
    return _out(ch)


@router.delete("/notifications/{channel_id}", status_code=204)
def delete_channel(
    channel_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(admin_only),
):
    ch = s.get(NotificationChannel, channel_id)
    if ch is None:
        raise HTTPException(404, "channel not found")
    if ch.type == "webhook":
        try:
            ctx.secrets.delete(f"notifications/{ch.id}")
        except Exception:
            pass  # the channel goes regardless; an orphaned secret is harmless
    s.delete(ch)
    audit.record(s, actor=actor.name, action="notifications.delete", target=ch.name, ip=client_ip(request))


@router.post("/notifications/{channel_id}/test", status_code=202)
def test_channel(
    channel_id: uuid.UUID,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    _: Principal = Depends(admin_only),
):
    if s.get(NotificationChannel, channel_id) is None:
        raise HTTPException(404, "channel not found")
    return {"task_id": ctx.jobs.enqueue("alert.test", {"channel_id": str(channel_id)}, max_attempts=1)}


# ---------------------------------------------------------------- Vault audit log


def _vault_entry(e: dict[str, Any]) -> dict[str, Any] | None:
    """Who did what in Vault. Vault already HMACs secret values; we keep only metadata."""
    req = e.get("request") or {}
    auth = e.get("auth") or {}
    if e.get("type") != "response":
        return None  # each operation logs a request and a response; keep one
    meta = auth.get("metadata") or {}
    return {
        "time": e.get("time"),
        "who": auth.get("display_name") or meta.get("role_name") or "-",
        "policies": auth.get("policies") or [],
        "operation": req.get("operation"),
        "path": req.get("path"),
        "remote_address": req.get("remote_address"),
        "error": (e.get("error") or None) and str(e["error"])[:300],
    }


@router.get("/vault-audit")
def vault_audit(
    limit: int = 200,
    path: str | None = None,
    _: Principal = Depends(admin_only),
) -> dict[str, Any]:
    """The newest entries of Vault's own audit log (who accessed which secret, never values)."""
    if not VAULT_AUDIT_LOG.exists():
        return {"available": False, "entries": []}
    limit = min(max(limit, 1), 2000)
    tail: deque[str] = deque(maxlen=limit * 4)
    with VAULT_AUDIT_LOG.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            tail.append(line)
    entries = []
    for line in reversed(tail):
        try:
            item = _vault_entry(json.loads(line))
        except ValueError:
            continue
        if item and (not path or path in (item["path"] or "")):
            entries.append(item)
        if len(entries) >= limit:
            break
    return {"available": True, "entries": entries}
