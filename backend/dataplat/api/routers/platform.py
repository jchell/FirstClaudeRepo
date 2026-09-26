"""Health, jobs and lineage endpoints."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from dataplat.adapters.pg_queue import DuplicateJob
from dataplat.api.deps import client_ip, current_principal, get_ctx, get_session, require_roles
from dataplat.core import audit
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.db.models import JobRow, ServiceAccount
from dataplat.orchestration.handlers import HANDLERS

router = APIRouter(prefix="/api", tags=["platform"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: the API process is up."""
    return {"status": "ok"}


def _check(name: str, fn: Any) -> dict[str, Any]:
    start = time.monotonic()
    try:
        ok = bool(fn())
        error = None
    except Exception as e:  # health must never raise
        ok, error = False, type(e).__name__
    return {"name": name, "ok": ok, "latency_ms": round((time.monotonic() - start) * 1000, 1), "error": error}


@router.get("/health/components")
def component_health(
    ctx: PlatformContext = Depends(get_ctx), _: Principal = Depends(current_principal)
) -> dict[str, Any]:
    """Readiness of every configured adapter, for the Ops dashboard."""
    checks = {
        "secret_store": lambda: ctx.secrets.health(),
        "metadata_store": lambda: ctx.metadata.health(),
        "object_store": lambda: ctx.objects.health(),
        "query_engine": lambda: ctx.query_engine.health(),
        "event_bus": lambda: ctx.events.health(),
        "knowledge_graph": lambda: ctx.knowledge_graph.health(),
    }
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        results = list(pool.map(lambda kv: _check(*kv), checks.items()))
    for r in results:
        r["adapter"] = ctx.config.adapters.get(r["name"])
    return {"ok": all(r["ok"] for r in results), "components": results}


# ---------------------------------------------------------------- jobs


class JobIn(BaseModel):
    kind: str
    payload: dict[str, Any] = {}
    service_account: str | None = None
    dedupe_key: str | None = Field(default=None, max_length=256)


class JobOut(BaseModel):
    id: int
    kind: str
    status: str
    attempts: int
    max_attempts: int
    service_account: str | None
    last_error: str | None
    result: dict[str, Any] | None
    created_at: Any
    finished_at: Any


@router.post("/jobs", response_model=JobOut, status_code=201)
def submit_job(
    body: JobIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(require_roles("engineer")),
):
    if body.kind not in HANDLERS:
        raise HTTPException(422, f"unknown job kind; available: {sorted(HANDLERS)}")
    if body.service_account:
        row = s.scalars(select(ServiceAccount).where(ServiceAccount.name == body.service_account)).first()
        if row is None or row.disabled:
            raise HTTPException(422, "unknown or disabled service account")
    # The API cannot mint service-account tokens; the scheduler's dispatcher attaches
    # one to the queued job, so the API never gains read access to those secrets.
    try:
        job_id = ctx.jobs.enqueue(
            body.kind, body.payload, dedupe_key=body.dedupe_key, service_account=body.service_account
        )
    except DuplicateJob as e:
        raise HTTPException(409, "an identical job is already queued or running") from e
    audit.record(
        s,
        actor=actor.name,
        action="job.submit",
        target=f"job:{job_id}",
        detail={"kind": body.kind, "service_account": body.service_account},
        ip=client_ip(request),
    )
    return _job_out(s.get(JobRow, job_id))


def _job_out(row: JobRow) -> JobOut:
    return JobOut.model_validate(row, from_attributes=True)


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(
    limit: int = 50, s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)
):
    q = select(JobRow).order_by(JobRow.id.desc()).limit(min(max(limit, 1), 500))
    return [_job_out(r) for r in s.scalars(q)]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: int, s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)
):
    row = s.get(JobRow, job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    return _job_out(row)


# ---------------------------------------------------------------- lineage


@router.get("/lineage/events")
def lineage_events(
    job_name: str | None = None,
    limit: int = 100,
    ctx: PlatformContext = Depends(get_ctx),
    _: Principal = Depends(current_principal),
) -> list[dict[str, Any]]:
    return ctx.lineage.events(job_name=job_name, limit=min(max(limit, 1), 1000))
