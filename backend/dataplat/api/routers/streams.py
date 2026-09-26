"""Streams & Replication: continuous jobs, their metrics and controls; webhook ingest; alerts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from dataplat.adapters.debezium_capture import ChangeCaptureError
from dataplat.adapters.jwt_identity import InvalidToken
from dataplat.api.deps import client_ip, current_principal, get_ctx, get_session, require_roles
from dataplat.api.ratelimit import SlidingWindowLimiter
from dataplat.core import audit
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.db.models import Alert, Connection, IngestionJob, StreamMetric, StreamState, WebhookKey
from dataplat.ingestion.spec import is_continuous
from dataplat.streaming.debezium import connector_name

router = APIRouter(tags=["streams"])
engineer = require_roles("engineer")

HEARTBEAT_STALE = timedelta(seconds=30)


# ---------------------------------------------------------------- streams


class StreamOut(BaseModel):
    job_id: uuid.UUID
    name: str
    kind: str  # cdc | stream
    connection: str | None
    connection_type: str | None
    source: str | None
    target: str
    write_mode: str
    desired: str
    status: str
    topics: list[str]
    connector: dict[str, Any] | None
    lag: int | None
    latency_p50_ms: int | None
    latency_p95_ms: int | None
    records_per_minute: int
    totals: dict[str, Any]
    last_batch_at: datetime | None
    heartbeat_at: datetime | None
    last_error: str | None
    key_columns: list[str] | None


def _stream_out(s: Session, job: IngestionJob, st: StreamState | None) -> StreamOut:
    conn = s.get(Connection, job.connection_id)
    metrics = (st.metrics if st else None) or {}
    since = datetime.now(UTC) - timedelta(minutes=5)
    recent = list(s.scalars(select(StreamMetric).where(StreamMetric.job_id == job.id, StreamMetric.minute >= since)))
    status = st.status if st else "starting"
    if st and status == "running" and st.heartbeat_at and datetime.now(UTC) - st.heartbeat_at > HEARTBEAT_STALE:
        status = "stalled"  # the stream worker stopped reporting
    return StreamOut(
        job_id=job.id,
        name=job.name,
        kind=job.spec["load_mode"],
        connection=conn.name if conn else None,
        connection_type=conn.type if conn else None,
        source=job.spec["source"].get("object") or (conn.config.get("stream_name") if conn else None),
        target=f"{job.spec['target']['layer']}.{job.spec['target']['dataset']}",
        write_mode=job.spec.get("stream", {}).get("write_mode", "changelog"),
        desired=st.desired if st else "running",
        status=status if job.enabled else "paused",
        topics=st.topics if st else [],
        connector=metrics.get("connector"),
        lag=metrics.get("lag"),
        latency_p50_ms=metrics.get("latency_p50_ms"),
        latency_p95_ms=metrics.get("latency_p95_ms"),
        records_per_minute=round(sum(m.records for m in recent) / 5),
        totals=(st.totals if st else None) or {},
        last_batch_at=st.last_batch_at if st else None,
        heartbeat_at=st.heartbeat_at if st else None,
        last_error=st.last_error if st else None,
        key_columns=st.key_columns if st else None,
    )


def _streams(s: Session) -> list[StreamOut]:
    jobs = [j for j in s.scalars(select(IngestionJob).order_by(IngestionJob.name)) if is_continuous(j.spec)]
    return [_stream_out(s, j, s.get(StreamState, j.id)) for j in jobs]


@router.get("/api/streams", response_model=list[StreamOut])
def list_streams(s: Session = Depends(get_session), _: Principal = Depends(current_principal)):
    return _streams(s)


def _job(s: Session, job_id: uuid.UUID) -> IngestionJob:
    job = s.get(IngestionJob, job_id)
    if job is None or not is_continuous(job.spec):
        raise HTTPException(404, "stream not found")
    return job


@router.get("/api/streams/{job_id}/metrics")
def stream_metrics(
    job_id: uuid.UUID, minutes: int = 60, s: Session = Depends(get_session), _: Principal = Depends(current_principal)
) -> dict[str, Any]:
    """Current lag and latency plus a per-minute series (records, dead letters, latency, lag)."""
    job = _job(s, job_id)
    since = datetime.now(UTC) - timedelta(minutes=min(max(minutes, 1), 24 * 60))
    series = s.scalars(
        select(StreamMetric)
        .where(StreamMetric.job_id == job_id, StreamMetric.minute >= since)
        .order_by(StreamMetric.minute)
    )
    current = _stream_out(s, job, s.get(StreamState, job_id))
    return {
        "stream": current.model_dump(mode="json"),
        "lag": current.lag,
        "latency_p50_ms": current.latency_p50_ms,
        "latency_p95_ms": current.latency_p95_ms,
        "series": [
            {
                "minute": m.minute,
                "records": m.records,
                "batches": m.batches,
                "dlq": m.dlq,
                "latency_p50_ms": m.latency_p50_ms,
                "latency_p95_ms": m.latency_p95_ms,
                "lag": m.lag,
            }
            for m in series
        ],
    }


def _control(ctx: PlatformContext, s: Session, job: IngestionJob, desired: str) -> None:
    st = s.get(StreamState, job.id) or StreamState(job_id=job.id, status="starting", topics=[], metrics={}, totals={})
    s.add(st)
    st.desired = desired
    if job.spec["load_mode"] == "cdc":
        name = connector_name(job.id)
        try:
            ctx.change_capture.pause(name) if desired == "paused" else ctx.change_capture.resume(name)
        except (ChangeCaptureError, httpx.HTTPError) as e:
            raise HTTPException(502, f"Kafka Connect: {e}") from e


@router.post("/api/streams/{job_id}/pause", response_model=StreamOut)
def pause(
    job_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(engineer),
):
    job = _job(s, job_id)
    _control(ctx, s, job, "paused")
    audit.record(s, actor=actor.name, action="stream.pause", target=job.name, ip=client_ip(request))
    s.flush()
    return _stream_out(s, job, s.get(StreamState, job.id))


@router.post("/api/streams/{job_id}/resume", response_model=StreamOut)
def resume(
    job_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(engineer),
):
    job = _job(s, job_id)
    _control(ctx, s, job, "running")
    audit.record(s, actor=actor.name, action="stream.resume", target=job.name, ip=client_ip(request))
    s.flush()
    return _stream_out(s, job, s.get(StreamState, job.id))


@router.post("/api/streams/{job_id}/resnapshot", status_code=202)
def resnapshot(
    job_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(engineer),
) -> dict[str, str]:
    """CDC only: re-read the whole source table (e.g. after a long outage)."""
    job = _job(s, job_id)
    if job.spec["load_mode"] != "cdc":
        raise HTTPException(409, "only CDC streams can re-snapshot")
    try:
        ctx.change_capture.reset_offsets(connector_name(job.id))
    except (ChangeCaptureError, httpx.HTTPError) as e:
        raise HTTPException(502, f"Kafka Connect: {e}") from e
    audit.record(s, actor=actor.name, action="stream.resnapshot", target=job.name, ip=client_ip(request))
    return {"status": "snapshot restarted"}


@router.get("/api/streams/{job_id}/dlq")
def dead_letters(
    job_id: uuid.UUID,
    limit: int = 20,
    s: Session = Depends(get_session),
    ctx: PlatformContext = Depends(get_ctx),
    _: Principal = Depends(engineer),
) -> list[dict[str, Any]]:
    """The most recent records that couldn't be ingested, with the reason."""
    from confluent_kafka import Consumer, TopicPartition

    _job(s, job_id)
    topic = f"dlq.{job_id.hex[:12]}"
    c = Consumer(
        {
            "bootstrap.servers": ctx.config.event_bus.bootstrap_servers,
            "group.id": f"dataplat-dlq-peek-{uuid.uuid4().hex[:6]}",
            "enable.auto.commit": False,
        }
    )
    try:
        md = c.list_topics(topic, timeout=5).topics.get(topic)
        if md is None or md.error is not None:
            return []
        out: list[dict[str, Any]] = []
        for p in md.partitions:
            low, high = c.get_watermark_offsets(TopicPartition(topic, p), timeout=5)
            start = max(low, high - limit)
            if start >= high:
                continue
            c.assign([TopicPartition(topic, p, start)])
            for m in c.consume(num_messages=high - start, timeout=5):
                if m.error():
                    continue
                headers = {k: v.decode("utf-8", "replace") for k, v in (m.headers() or [])}
                out.append(
                    {
                        "offset": f"{p}:{m.offset()}",
                        "error": headers.get("error"),
                        "source_offset": headers.get("source_offset"),
                        "value": (m.value() or b"")[:300].decode("utf-8", "replace"),
                    }
                )
        return out[-limit:]
    finally:
        c.close()


@router.websocket("/api/ws/streams")
async def streams_ws(ws: WebSocket) -> None:
    """Live stream status. The first message must be {"token": "<access token>"}."""
    await ws.accept()
    ctx: PlatformContext = ws.app.state.ctx
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
        ctx.identity.verify_access_token(str(hello.get("token", "")))
    except (TimeoutError, InvalidToken, ValueError, WebSocketDisconnect):
        await ws.close(code=4401)
        return
    try:
        while True:

            def snapshot() -> list[dict[str, Any]]:
                with ctx.metadata.session() as s:
                    return [x.model_dump(mode="json") for x in _streams(s)]

            await ws.send_json({"type": "streams", "streams": await asyncio.to_thread(snapshot)})
            await asyncio.sleep(2)
    except (WebSocketDisconnect, RuntimeError):
        return


# ---------------------------------------------------------------- webhook ingest


class WebhookKeyOut(BaseModel):
    key: str
    hint: str
    endpoint: str
    note: str = "Shown once. Send it as the X-API-Key header; only its hash is stored."


@router.post("/api/connections/{connection_id}/webhook-key", response_model=WebhookKeyOut)
def new_webhook_key(
    connection_id: uuid.UUID, request: Request, s: Session = Depends(get_session), actor: Principal = Depends(engineer)
):
    conn = s.get(Connection, connection_id)
    if conn is None or conn.type != "webhook":
        raise HTTPException(404, "webhook connection not found")
    key = "whk_" + secrets.token_urlsafe(32)
    row = s.get(WebhookKey, conn.id) or WebhookKey(connection_id=conn.id)
    row.key_hash = hashlib.sha256(key.encode()).hexdigest()
    row.hint = key[-4:]
    row.created_by = actor.name
    row.created_at = datetime.now(UTC)
    s.add(row)
    audit.record(s, actor=actor.name, action="webhook.key_issued", target=conn.name, ip=client_ip(request))
    return WebhookKeyOut(key=key, hint=row.hint, endpoint=f"/ingest/events/{conn.config['stream_name']}")


_ingest_limiter = SlidingWindowLimiter(limit=600, window_seconds=60)
MAX_BODY = 1_000_000
MAX_EVENTS = 1000


@router.post("/ingest/events/{stream}", status_code=202)
async def ingest_events(stream: str, request: Request, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    """Accepts one JSON event or an array of events for a webhook stream."""
    ctx: PlatformContext = request.app.state.ctx
    if not x_api_key:
        raise HTTPException(401, "missing X-API-Key")
    digest = hashlib.sha256(x_api_key.encode()).hexdigest()

    def lookup() -> str | None:
        with ctx.metadata.session() as s:
            row = s.scalars(select(WebhookKey).where(WebhookKey.key_hash == digest)).first()
            if row is None:
                return None
            conn = s.get(Connection, row.connection_id)
            return conn.config.get("stream_name") if conn else None

    name = await asyncio.to_thread(lookup)
    if name is None or not secrets.compare_digest(name, stream):
        raise HTTPException(401, "invalid key for this stream")
    if not _ingest_limiter.allow(digest):
        raise HTTPException(429, "rate limit exceeded")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "body too large (max 1 MB)")
    try:
        payload = json.loads(body)
    except ValueError as e:
        raise HTTPException(400, "body must be JSON") from e
    events = payload if isinstance(payload, list) else [payload]
    if not events or len(events) > MAX_EVENTS or not all(isinstance(e, dict) for e in events):
        raise HTTPException(422, f"send a JSON object or an array of 1-{MAX_EVENTS} objects")
    topic = f"webhook.{stream}"

    def publish() -> None:
        ctx.events.ensure_topic(topic)
        for e in events:
            ctx.events.publish(topic, e)
        ctx.events.flush(10)

    await asyncio.to_thread(publish)
    return {"accepted": len(events)}


# ---------------------------------------------------------------- alerts


class AlertOut(BaseModel):
    id: int
    kind: str
    severity: str
    target: str
    message: str
    details: dict[str, Any]
    opened_at: datetime
    resolved_at: datetime | None


@router.get("/api/alerts", response_model=list[AlertOut])
def list_alerts(
    open_only: bool = True,
    limit: int = 100,
    s: Session = Depends(get_session),
    _: Principal = Depends(current_principal),
):
    q = select(Alert).order_by(Alert.opened_at.desc()).limit(min(max(limit, 1), 500))
    if open_only:
        q = q.where(Alert.resolved_at.is_(None))
    return [AlertOut.model_validate(a, from_attributes=True) for a in s.scalars(q)]


@router.post("/api/alerts/{alert_id}/resolve", response_model=AlertOut)
def resolve(alert_id: int, request: Request, s: Session = Depends(get_session), actor: Principal = Depends(engineer)):
    a = s.get(Alert, alert_id)
    if a is None:
        raise HTTPException(404, "alert not found")
    a.resolved_at = a.resolved_at or datetime.now(UTC)
    audit.record(s, actor=actor.name, action="alert.resolve", target=a.target, ip=client_ip(request))
    return AlertOut.model_validate(a, from_attributes=True)
