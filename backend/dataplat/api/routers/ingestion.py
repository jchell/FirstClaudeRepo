"""Ingestion jobs (versioned JobSpecs), runs and schedules."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataplat.adapters.debezium_capture import ChangeCaptureError
from dataplat.adapters.pg_queue import DuplicateJob
from dataplat.api.deps import client_ip, current_principal, get_ctx, get_session, require_roles
from dataplat.connectors.registry import get_connector_class
from dataplat.core import audit
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.db.models import (
    Connection,
    IngestedFile,
    IngestionJob,
    IngestionJobVersion,
    IngestionRun,
    IngestionState,
    Schedule,
    StreamState,
)
from dataplat.ingestion.spec import JobSpec, decode_watermark, is_continuous, validate_for_category
from dataplat.streaming.debezium import build_config, connector_name
from dataplat.transform.service import DefinitionError, ensure_promotion
from dataplat.vault.loader import VaultError
from dataplat.vault.service import apply_raw_vault

router = APIRouter(prefix="/api/ingestion", tags=["ingestion"])
engineer = require_roles("engineer")


class JobIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,127}$")
    description: str = ""
    connection_id: uuid.UUID
    spec: dict[str, Any]
    enabled: bool = True


class JobUpdate(BaseModel):
    description: str | None = None
    spec: dict[str, Any] | None = None
    enabled: bool | None = None


class RunOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    job_name: str | None = None
    job_version: int
    batch_id: str
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    rows_read: int
    rows_written: int
    bytes_read: int
    files: int
    table_version: int | None
    dataset_id: uuid.UUID | None
    details: dict[str, Any]
    error: str | None


class JobOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    connection_id: uuid.UUID
    connection_name: str | None
    connection_type: str | None
    spec: dict[str, Any]
    version: int
    enabled: bool
    target: str
    watermark: Any
    last_run: RunOut | None
    next_run_at: datetime | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


def _run_out(r: IngestionRun, job_name: str | None = None) -> RunOut:
    out = RunOut.model_validate(r, from_attributes=True)
    out.job_name = job_name
    return out


def _job_out(s: Session, j: IngestionJob) -> JobOut:
    conn = s.get(Connection, j.connection_id)
    state = s.get(IngestionState, j.id)
    last = s.scalars(
        select(IngestionRun).where(IngestionRun.job_id == j.id).order_by(IngestionRun.started_at.desc())
    ).first()
    sched = s.get(Schedule, j.schedule_id) if j.schedule_id else None
    wm = decode_watermark(state.watermark) if state else None
    return JobOut(
        id=j.id,
        name=j.name,
        description=j.description,
        connection_id=j.connection_id,
        connection_name=conn.name if conn else None,
        connection_type=conn.type if conn else None,
        spec=j.spec,
        version=j.version,
        enabled=j.enabled,
        target=f"{j.spec['target']['layer']}.{j.spec['target']['dataset']}",
        watermark=str(wm) if wm is not None else None,
        last_run=_run_out(last, j.name) if last else None,
        next_run_at=sched.next_run_at if sched and sched.enabled else None,
        created_by=j.created_by,
        created_at=j.created_at,
        updated_at=j.updated_at,
    )


def _validate(s: Session, connection_id: uuid.UUID, raw: dict[str, Any], job_id: uuid.UUID | None = None) -> JobSpec:
    conn = s.get(Connection, connection_id)
    if conn is None:
        raise HTTPException(422, "connection not found")
    try:
        spec = JobSpec.model_validate(raw)
        validate_for_category(spec, get_connector_class(conn.type).category, conn.type)
    except (ValidationError, ValueError) as e:
        msg = (
            "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            if isinstance(e, ValidationError)
            else str(e)
        )
        raise HTTPException(422, msg) from e
    # One job owns each bronze dataset, so runs never overwrite each other's data.
    for other in s.scalars(select(IngestionJob).where(IngestionJob.id != job_id if job_id else True)):
        t = other.spec.get("target", {})
        if t.get("layer") == spec.target.layer and t.get("dataset") == spec.target.dataset:
            raise HTTPException(409, f"{spec.target.layer}.{spec.target.dataset} is already loaded by job {other.name}")
    return spec


def _sync_stream(ctx: PlatformContext, s: Session, job: IngestionJob, spec: JobSpec, conn: Connection) -> None:
    """Creates/updates the stream state and, for CDC, the Debezium connector."""
    state = s.get(StreamState, job.id)
    if not is_continuous(spec):
        if state is not None:
            s.delete(state)
        return
    if state is None:
        state = StreamState(job_id=job.id, status="starting", topics=[], metrics={}, totals={})
        s.add(state)
    state.desired = "running" if job.enabled else "paused"
    if spec.load_mode == "cdc":
        name = connector_name(job.id)
        state.connector_name = name
        try:
            ctx.change_capture.create_connector(
                name, build_config(ctx.config, job.id, conn.type, conn.name, conn.config, spec)
            )
            if not job.enabled:
                ctx.change_capture.pause(name)
            elif ctx.change_capture.status(name).get("state") == "PAUSED":
                ctx.change_capture.resume(name)
        except (ChangeCaptureError, httpx.HTTPError) as e:
            raise HTTPException(502, f"Kafka Connect: {e}") from e


def _sync_transform(s: Session, job: IngestionJob, spec: JobSpec, user: str) -> None:
    """The wizard's "Add to Raw Vault" and "promote to silver" options."""
    try:
        if spec.raw_vault is not None:
            apply_raw_vault(s, spec.raw_vault, spec.target.dataset, job.id, user)
        if spec.promote_to_silver:
            ensure_promotion(s, spec.target.dataset, job.id, user)
    except (VaultError, DefinitionError) as e:
        raise HTTPException(422, str(e)) from e


def _sync_schedule(s: Session, job: IngestionJob, spec: JobSpec, conn: Connection) -> None:
    sched = s.get(Schedule, job.schedule_id) if job.schedule_id else None
    if spec.schedule.type == "none" or is_continuous(spec):
        if sched is not None:
            job.schedule_id = None
            s.flush()
            s.delete(sched)
        return
    if sched is None:
        sched = Schedule(name=f"ingest:{job.id}", kind="ingestion.run")
        s.add(sched)
        s.flush()
        job.schedule_id = sched.id
    watch = spec.schedule.type == "file_arrival"
    # File arrival: a light "watch" job polls the source and starts a run when files appear.
    sched.kind = "ingestion.watch" if watch else "ingestion.run"
    sched.payload = {"ingestion_job_id": str(job.id), "trigger": "schedule"}
    sched.service_account = conn.service_account
    sched.cron = spec.schedule.cron if spec.schedule.type == "cron" else None
    sched.interval_seconds = (
        spec.schedule.poll_seconds
        if watch
        else spec.schedule.interval_seconds
        if spec.schedule.type == "interval"
        else None
    )
    sched.enabled = job.enabled
    sched.next_run_at = None  # scheduler computes the next fire time from the new definition


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)):
    return [_job_out(s, j) for j in s.scalars(select(IngestionJob).order_by(IngestionJob.name))]


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: uuid.UUID, s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)
):
    j = s.get(IngestionJob, job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    return _job_out(s, j)


@router.post("/jobs", response_model=JobOut, status_code=201)
def create_job(
    body: JobIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(engineer),
):
    spec = _validate(s, body.connection_id, body.spec)
    job = IngestionJob(
        name=body.name,
        description=body.description,
        connection_id=body.connection_id,
        spec=spec.model_dump(mode="json"),
        version=1,
        enabled=body.enabled,
        created_by=actor.name,
    )
    s.add(job)
    try:
        s.flush()
    except IntegrityError as e:
        raise HTTPException(409, "a job with that name exists") from e
    s.add(IngestionJobVersion(job_id=job.id, version=1, spec=job.spec, created_by=actor.name))
    conn = s.get(Connection, body.connection_id)
    _sync_transform(s, job, spec, actor.name)
    _sync_schedule(s, job, spec, conn)
    _sync_stream(ctx, s, job, spec, conn)
    audit.record(s, actor=actor.name, action="ingestion_job.create", target=job.name, ip=client_ip(request))
    s.flush()
    return _job_out(s, job)


@router.put("/jobs/{job_id}", response_model=JobOut)
def update_job(
    job_id: uuid.UUID,
    body: JobUpdate,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(engineer),
):
    job = s.get(IngestionJob, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    spec = _validate(s, job.connection_id, body.spec if body.spec is not None else job.spec, job.id)
    if body.description is not None:
        job.description = body.description
    if body.enabled is not None:
        job.enabled = body.enabled
    new_spec = spec.model_dump(mode="json")
    if new_spec != job.spec:
        job.version += 1
        job.spec = new_spec
        s.add(IngestionJobVersion(job_id=job.id, version=job.version, spec=new_spec, created_by=actor.name))
    conn = s.get(Connection, job.connection_id)
    _sync_transform(s, job, spec, actor.name)
    _sync_schedule(s, job, spec, conn)
    _sync_stream(ctx, s, job, spec, conn)
    audit.record(
        s,
        actor=actor.name,
        action="ingestion_job.update",
        target=job.name,
        detail={"version": job.version},
        ip=client_ip(request),
    )
    s.flush()
    return _job_out(s, job)


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(
    job_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(engineer),
):
    job = s.get(IngestionJob, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.spec.get("load_mode") == "cdc":
        try:
            ctx.change_capture.delete_connector(connector_name(job.id))
        except (ChangeCaptureError, httpx.HTTPError) as e:
            raise HTTPException(502, f"Kafka Connect: {e}") from e
        conn = s.get(Connection, job.connection_id)
        # The source keeps a replication slot/publication for the connector: drop them
        # from the worker (it runs as the service account that can read the password).
        ctx.jobs.enqueue(
            "cdc.cleanup",
            {"connection_type": conn.type, "config": dict(conn.config), "slot": f"dataplat_{job.id.hex[:12]}"},
            service_account=conn.service_account,
            max_attempts=5,
        )
    sched_id = job.schedule_id
    s.delete(job)
    s.flush()
    if sched_id:
        s.execute(delete(Schedule).where(Schedule.id == sched_id))
    audit.record(s, actor=actor.name, action="ingestion_job.delete", target=job.name, ip=client_ip(request))


@router.get("/jobs/{job_id}/versions")
def job_versions(
    job_id: uuid.UUID, s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)
):
    rows = s.scalars(
        select(IngestionJobVersion)
        .where(IngestionJobVersion.job_id == job_id)
        .order_by(IngestionJobVersion.version.desc())
    )
    return [
        {"version": v.version, "spec": v.spec, "created_by": v.created_by, "created_at": v.created_at} for v in rows
    ]


@router.post("/jobs/{job_id}/run", status_code=202)
def run_now(
    job_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(engineer),
) -> dict[str, Any]:
    job = s.get(IngestionJob, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if is_continuous(job.spec):
        raise HTTPException(409, "this job runs continuously; pause or resume it on the Streams page")
    conn = s.get(Connection, job.connection_id)
    try:
        task = ctx.jobs.enqueue(
            "ingestion.run",
            {"ingestion_job_id": str(job.id), "trigger": "manual"},
            dedupe_key=f"ingest:{job.id}",
            service_account=conn.service_account,
        )
    except DuplicateJob as e:
        raise HTTPException(409, "this job is already queued or running") from e
    audit.record(s, actor=actor.name, action="ingestion_job.run", target=job.name, ip=client_ip(request))
    return {"task_id": task}


@router.post("/jobs/{job_id}/reset-state", status_code=204)
def reset_state(
    job_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(engineer),
):
    """Forgets the watermark and ingested-file list, so the next run starts from scratch."""
    job = s.get(IngestionJob, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    s.execute(delete(IngestionState).where(IngestionState.job_id == job_id))
    s.execute(delete(IngestedFile).where(IngestedFile.job_id == job_id))
    audit.record(s, actor=actor.name, action="ingestion_job.reset_state", target=job.name, ip=client_ip(request))


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    job_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 100,
    s: Session = Depends(get_session, scope="function"),
    _: Principal = Depends(current_principal),
):
    q = (
        select(IngestionRun, IngestionJob.name)
        .join(IngestionJob, IngestionJob.id == IngestionRun.job_id)
        .order_by(IngestionRun.started_at.desc())
        .limit(min(max(limit, 1), 1000))
    )
    if job_id:
        q = q.where(IngestionRun.job_id == job_id)
    if status:
        q = q.where(IngestionRun.status == status)
    return [_run_out(r, name) for r, name in s.execute(q)]


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(
    run_id: uuid.UUID, s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)
):
    r = s.get(IngestionRun, run_id)
    if r is None:
        raise HTTPException(404, "run not found")
    job = s.get(IngestionJob, r.job_id)
    return _run_out(r, job.name if job else None)
