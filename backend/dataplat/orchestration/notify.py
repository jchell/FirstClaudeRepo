"""Alert notifications: webhook and email channels.

Every new alert queues an ``alert.notify`` job; the worker sends it to each enabled
channel whose kinds and minimum severity match. Channel secrets (the webhook URL,
which often embeds a token, and an optional bearer token) live in Vault under
``kv/dataplat/notifications/<channel id>``: the API can write them, only the worker
reads them.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import httpx
from sqlalchemy import select

from dataplat.core.context import PlatformContext
from dataplat.core.logging import redact
from dataplat.core.ports.orchestration import Job
from dataplat.core.ports.secrets import SecretStore
from dataplat.db.models import Alert, NotificationChannel
from dataplat.orchestration.handlers import handler

log = logging.getLogger(__name__)

SEVERITY = {"warning": 0, "serious": 1, "critical": 2}
CHANNEL_SECRETS = {"webhook": ("url", "token"), "email": ()}


def secret_ref(cfg: Any, channel_id: Any, field: str) -> str:
    v = cfg.vault
    return f"vault://{v.kv_mount}/{v.kv_prefix}/notifications/{channel_id}#{field}"


def payload(alert: Alert) -> dict[str, Any]:
    return {
        "type": "dataplat.alert",
        "id": alert.id,
        "kind": alert.kind,
        "severity": alert.severity,
        "target": alert.target,
        "message": alert.message,
        "opened_at": alert.opened_at.isoformat() if alert.opened_at else None,
        "details": alert.details,
    }


def matches(ch: NotificationChannel, alert: Alert) -> bool:
    return (
        ch.enabled
        and (not ch.kinds or alert.kind in ch.kinds)
        and SEVERITY.get(alert.severity, 0) >= SEVERITY.get(ch.min_severity, 0)
    )


def send_webhook(ctx: PlatformContext, ch: NotificationChannel, body: dict[str, Any]) -> None:
    url = ctx.secrets.resolve(ch.config["url"])
    headers = {"User-Agent": "dataplat-alerts"}
    if ch.config.get("token"):
        headers["Authorization"] = f"Bearer {ctx.secrets.resolve(ch.config['token'])}"
    r = httpx.post(url, json=body, headers=headers, timeout=ctx.config.notifications.timeout_seconds)
    if r.status_code >= 300:
        raise RuntimeError(f"webhook answered HTTP {r.status_code}")


def send_email(ctx: PlatformContext, ch: NotificationChannel, body: dict[str, Any]) -> None:
    smtp = ctx.config.notifications.smtp
    if not smtp.host:
        raise RuntimeError("no SMTP server configured (notifications.smtp.host)")
    msg = EmailMessage()
    msg["Subject"] = f"[dataplat {body['severity']}] {body['kind']}: {body['target']}"
    msg["From"] = smtp.sender
    msg["To"] = ", ".join(ch.config.get("recipients", []))
    msg.set_content(
        f"{body['message']}\n\nKind: {body['kind']}\nTarget: {body['target']}\n"
        f"Severity: {body['severity']}\nOpened: {body['opened_at']}\n"
    )
    with smtplib.SMTP(smtp.host, smtp.port, timeout=ctx.config.notifications.timeout_seconds) as c:
        if smtp.starttls:
            c.starttls()
        if smtp.username:
            c.login(smtp.username, ctx.secrets.resolve(smtp.password) if smtp.password else "")
        c.send_message(msg)


SENDERS = {"webhook": send_webhook, "email": send_email}


def deliver(ctx: PlatformContext, ch: NotificationChannel, body: dict[str, Any]) -> str | None:
    """Sends one notification; returns an error message or None."""
    try:
        SENDERS[ch.type](ctx, ch, body)
        return None
    except Exception as e:
        return redact(f"{type(e).__name__}: {e}")[:500]


@handler("alert.notify")
def notify(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    with ctx.metadata.session() as s:
        alert = s.get(Alert, job.payload["alert_id"])
        if alert is None:
            return {"skipped": "alert gone"}
        body = payload(alert)
        channels = [c for c in s.scalars(select(NotificationChannel)) if matches(c, alert)]
        results = {}
        for ch in channels:
            err = deliver(ctx, ch, body)
            ch.last_error = err
            if err is None:
                ch.last_sent_at = datetime.now(UTC)
            results[ch.name] = err or "sent"
    failed = [n for n, r in results.items() if r != "sent"]
    if failed and len(failed) == len(results):
        raise RuntimeError(f"every channel failed: {results}")
    return {"channels": results}


@handler("alert.test")
def test_channel(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    """Sends a test message through one channel (from the console)."""
    with ctx.metadata.session() as s:
        ch = s.get(NotificationChannel, job.payload["channel_id"])
        if ch is None:
            return {"ok": False, "error": "channel not found"}
        body = {
            "type": "dataplat.alert",
            "id": 0,
            "kind": "test",
            "severity": "warning",
            "target": f"channel:{ch.name}",
            "message": "Test notification from the data platform",
            "opened_at": datetime.now(UTC).isoformat(),
            "details": {},
        }
        err = deliver(ctx, ch, body)
        ch.last_error = err
        if err is None:
            ch.last_sent_at = datetime.now(UTC)
    return {"ok": err is None, "error": err}
