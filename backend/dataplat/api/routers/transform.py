"""Models (silver/gold), pipelines and their runs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataplat.adapters.pg_queue import COALESCE_PREFIX, DuplicateJob
from dataplat.api.deps import client_ip, current_principal, get_ctx, get_session, require_roles
from dataplat.core import audit
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.db.models import Dataset, Pipeline, Schedule, TransformModel, TransformModelVersion, TransformRun
from dataplat.transform.runner import ModelError, ModelSpec, dependencies, inputs, model_order
from dataplat.transform.service import DefinitionError, save_model, sync_pipeline_schedule

router = APIRouter(prefix="/api/transform", tags=["transform"])
engineer = require_roles("engineer")


class ModelIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    layer: Literal["silver", "gold"]
    kind: Literal["sql", "scd2_dimension", "fact", "date_dimension"] = "sql"
    sql: str = Field(default="", max_length=100_000)
    config: dict[str, Any] = {}
    description: str = ""


class ModelUpdate(BaseModel):
    sql: str | None = Field(default=None, max_length=100_000)
    config: dict[str, Any] | None = None
    description: str | None = None
    enabled: bool | None = None


class RunOut(BaseModel):
    id: uuid.UUID
    kind: str
    target: str
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    rows_written: int
    table_version: int | None
    pipeline_id: uuid.UUID | None
    parent_run_id: uuid.UUID | None
    details: dict[str, Any]
    error: str | None


class ModelOut(BaseModel):
    id: uuid.UUID
    name: str
    layer: str
    kind: str
    sql: str
    config: dict[str, Any]
    description: str
    version: int
    enabled: bool
    owner: str | None
    depends_on: list[str]
    inputs: list[str]
    used_by: list[str]
    dataset_id: uuid.UUID | None
    rows: int | None
    last_run: RunOut | None
    updated_at: datetime


def _specs(s: Session) -> dict[str, ModelSpec]:
    return {m.name: ModelSpec.of(m) for m in s.scalars(select(TransformModel))}


def _last_run(s: Session, target: str, kind: str = "model") -> TransformRun | None:
    return s.scalars(
        select(TransformRun)
        .where(TransformRun.kind == kind, TransformRun.target == target)
        .order_by(TransformRun.started_at.desc())
        .limit(1)
    ).first()


def _model_out(s: Session, m: TransformModel, specs: dict[str, ModelSpec]) -> ModelOut:
    ds = s.scalars(select(Dataset).where(Dataset.layer == m.layer, Dataset.name == m.name)).first()
    last = _last_run(s, f"{m.layer}.{m.name}")
    deps = dependencies(specs[m.name], specs) if m.name in specs else set()
    return ModelOut(
        id=m.id,
        name=m.name,
        layer=m.layer,
        kind=m.kind,
        sql=m.sql,
        config=m.config,
        description=m.description,
        version=m.version,
        enabled=m.enabled,
        owner=m.owner,
        depends_on=sorted(deps),
        inputs=inputs(specs[m.name], specs) if m.name in specs else [],
        used_by=sorted(n for n, sp in specs.items() if m.name in dependencies(sp, specs)),
        dataset_id=ds.id if ds else None,
        rows=ds.row_count if ds else None,
        last_run=RunOut.model_validate(last, from_attributes=True) if last else None,
        updated_at=m.updated_at,
    )


def _get_model(s: Session, name: str) -> TransformModel:
    m = s.scalars(select(TransformModel).where(TransformModel.name == name)).first()
    if m is None:
        raise HTTPException(404, "model not found")
    return m


@router.get("/models", response_model=list[ModelOut])
def list_models(s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)):
    specs = _specs(s)
    return [
        _model_out(s, m, specs)
        for m in s.scalars(select(TransformModel).order_by(TransformModel.layer, TransformModel.name))
    ]


@router.get("/models/{name}", response_model=ModelOut)
def get_model(
    name: str, s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)
):
    return _model_out(s, _get_model(s, name), _specs(s))


@router.get("/models/{name}/versions")
def model_versions(
    name: str, s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)
):
    m = _get_model(s, name)
    rows = s.scalars(
        select(TransformModelVersion)
        .where(TransformModelVersion.model_id == m.id)
        .order_by(TransformModelVersion.version.desc())
    )
    return [
        {"version": v.version, "sql": v.sql, "config": v.config, "created_by": v.created_by, "created_at": v.created_at}
        for v in rows
    ]


@router.post("/models", response_model=ModelOut, status_code=201)
def create_model(
    body: ModelIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(engineer),
):
    try:
        m = save_model(
            s,
            name=body.name,
            layer=body.layer,
            kind=body.kind,
            sql=body.sql,
            config=body.config,
            description=body.description,
            user=actor.name,
        )
    except DefinitionError as e:
        raise HTTPException(422, str(e)) from e
    audit.record(s, actor=actor.name, action="model.create", target=m.name, ip=client_ip(request))
    return _model_out(s, m, _specs(s))


@router.put("/models/{name}", response_model=ModelOut)
def update_model(
    name: str,
    body: ModelUpdate,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(engineer),
):
    m = _get_model(s, name)
    if body.enabled is not None:
        m.enabled = body.enabled
    try:
        save_model(
            s,
            name=m.name,
            layer=m.layer,
            kind=m.kind,
            sql=body.sql if body.sql is not None else m.sql,
            config=body.config if body.config is not None else m.config,
            description=body.description if body.description is not None else m.description,
            user=actor.name,
            existing=m,
        )
    except DefinitionError as e:
        raise HTTPException(422, str(e)) from e
    audit.record(
        s, actor=actor.name, action="model.update", target=name, detail={"version": m.version}, ip=client_ip(request)
    )
    return _model_out(s, m, _specs(s))


@router.delete("/models/{name}", status_code=204)
def delete_model(
    name: str,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(engineer),
):
    m = _get_model(s, name)
    specs = _specs(s)
    if users := [n for n, sp in specs.items() if name in dependencies(sp, specs)]:
        raise HTTPException(409, f"{name} is used by {', '.join(sorted(users))}")
    for p in s.scalars(select(Pipeline)):
        if name in (p.models or []):
            p.models = [x for x in p.models if x != name]
    s.delete(m)
    audit.record(s, actor=actor.name, action="model.delete", target=name, ip=client_ip(request))


class PreviewIn(BaseModel):
    name: str = Field(default="preview", pattern=r"^[a-z][a-z0-9_]{1,62}$")
    layer: Literal["silver", "gold"] = "silver"
    kind: Literal["sql", "scd2_dimension", "fact", "date_dimension"] = "sql"
    sql: str = Field(default="", max_length=100_000)
    config: dict[str, Any] = {}


@router.post("/preview", status_code=202)
def preview(body: PreviewIn, ctx: PlatformContext = Depends(get_ctx), _: Principal = Depends(engineer)):
    """Runs the model's query (first rows only) on a worker; poll /api/tasks/{id}."""
    task = ctx.jobs.enqueue("model.preview", body.model_dump(), max_attempts=1)
    return {"task_id": task, "kind": "model.preview", "status": "queued"}


def _queue(ctx: PlatformContext, kind: str, payload: dict[str, Any], key: str) -> dict[str, Any]:
    try:
        return {
            "queued": True,
            "task_id": ctx.jobs.enqueue(kind, payload, dedupe_key=f"{COALESCE_PREFIX}{key}", max_attempts=1),
        }
    except DuplicateJob:
        return {"queued": False, "detail": "a run is already waiting"}


@router.post("/models/{name}/run", status_code=202)
def run_model(
    name: str,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    _: Principal = Depends(engineer),
):
    _get_model(s, name)
    return _queue(ctx, "model.run", {"model": name, "trigger": "manual"}, f"model:{name}")


# ---------------------------------------------------------------- pipelines


class ScheduleIn(BaseModel):
    type: Literal["none", "cron", "interval"] = "none"
    cron: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60)

    @model_validator(mode="after")
    def _check(self) -> ScheduleIn:
        if self.type == "cron":
            if not self.cron:
                raise ValueError("cron schedules need a cron expression")
            CronTrigger.from_crontab(self.cron)
        if self.type == "interval" and not self.interval_seconds:
            raise ValueError("interval schedules need interval_seconds")
        return self


class PipelineIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,127}$")
    description: str = ""
    models: list[str] = Field(min_length=1)
    schedule: ScheduleIn = ScheduleIn()
    trigger_datasets: list[str] = []
    enabled: bool = True


class PipelineUpdate(BaseModel):
    description: str | None = None
    models: list[str] | None = None
    schedule: ScheduleIn | None = None
    trigger_datasets: list[str] | None = None
    enabled: bool | None = None


def _check_pipeline(s: Session, models: list[str], triggers: list[str]) -> list[str]:
    specs = _specs(s)
    if unknown := [m for m in models if m not in specs]:
        raise HTTPException(422, f"unknown models {unknown}")
    for t in triggers:
        layer, _, name = t.partition(".")
        if layer not in ("bronze", "silver", "gold", "vault") or not name:
            raise HTTPException(422, f"trigger datasets look like <layer>.<name>, not {t!r}")
    try:
        return model_order(models, specs)
    except ModelError as e:
        raise HTTPException(422, str(e)) from e


def _pipeline_out(s: Session, p: Pipeline) -> dict[str, Any]:
    specs = _specs(s)
    order = [m for m in model_order(p.models or [], specs)] if p.models else []
    sch = s.scalars(select(Schedule).where(Schedule.name == f"pipeline:{p.id}")).first()
    last = s.scalars(
        select(TransformRun)
        .where(TransformRun.kind == "pipeline", TransformRun.pipeline_id == p.id)
        .order_by(TransformRun.started_at.desc())
        .limit(1)
    ).first()
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "models": p.models,
        "order": order,
        "edges": [
            {"source": d, "target": m} for m in order for d in sorted(dependencies(specs[m], specs)) if d in order
        ],
        "layers": {m: specs[m].layer for m in order},
        "kinds": {m: specs[m].kind for m in order},
        "schedule": p.schedule or {"type": "none"},
        "trigger_datasets": p.trigger_datasets,
        "enabled": p.enabled,
        "next_run_at": sch.next_run_at if sch else None,
        "last_run": RunOut.model_validate(last, from_attributes=True).model_dump() if last else None,
        "created_by": p.created_by,
        "updated_at": p.updated_at,
    }


def _get_pipeline(s: Session, pipeline_id: uuid.UUID) -> Pipeline:
    p = s.get(Pipeline, pipeline_id)
    if p is None:
        raise HTTPException(404, "pipeline not found")
    return p


@router.get("/pipelines")
def list_pipelines(s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)):
    return [_pipeline_out(s, p) for p in s.scalars(select(Pipeline).order_by(Pipeline.name))]


@router.get("/pipelines/{pipeline_id}")
def get_pipeline(
    pipeline_id: uuid.UUID,
    s: Session = Depends(get_session, scope="function"),
    _: Principal = Depends(current_principal),
):
    return _pipeline_out(s, _get_pipeline(s, pipeline_id))


@router.post("/pipelines", status_code=201)
def create_pipeline(
    body: PipelineIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(engineer),
):
    _check_pipeline(s, body.models, body.trigger_datasets)
    p = Pipeline(
        name=body.name,
        description=body.description,
        models=body.models,
        schedule=body.schedule.model_dump(exclude_none=True),
        trigger_datasets=body.trigger_datasets,
        enabled=body.enabled,
        created_by=actor.name,
    )
    s.add(p)
    try:
        s.flush()
    except IntegrityError as e:
        raise HTTPException(409, "a pipeline with that name exists") from e
    sync_pipeline_schedule(s, p)
    audit.record(s, actor=actor.name, action="pipeline.create", target=p.name, ip=client_ip(request))
    s.flush()
    return _pipeline_out(s, p)


@router.put("/pipelines/{pipeline_id}")
def update_pipeline(
    pipeline_id: uuid.UUID,
    body: PipelineUpdate,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(engineer),
):
    p = _get_pipeline(s, pipeline_id)
    models = body.models if body.models is not None else p.models
    triggers = body.trigger_datasets if body.trigger_datasets is not None else p.trigger_datasets
    _check_pipeline(s, models, triggers)
    p.models, p.trigger_datasets = models, triggers
    if body.description is not None:
        p.description = body.description
    if body.schedule is not None:
        p.schedule = body.schedule.model_dump(exclude_none=True)
    if body.enabled is not None:
        p.enabled = body.enabled
    sync_pipeline_schedule(s, p)
    audit.record(s, actor=actor.name, action="pipeline.update", target=p.name, ip=client_ip(request))
    s.flush()
    return _pipeline_out(s, p)


@router.delete("/pipelines/{pipeline_id}", status_code=204)
def delete_pipeline(
    pipeline_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(engineer),
):
    p = _get_pipeline(s, pipeline_id)
    p.enabled = False
    sync_pipeline_schedule(s, p)
    s.delete(p)
    audit.record(s, actor=actor.name, action="pipeline.delete", target=p.name, ip=client_ip(request))


@router.post("/pipelines/{pipeline_id}/run", status_code=202)
def run_pipeline(
    pipeline_id: uuid.UUID,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    _: Principal = Depends(engineer),
):
    p = _get_pipeline(s, pipeline_id)
    return _queue(ctx, "pipeline.run", {"pipeline_id": str(p.id), "trigger": "manual"}, f"pipeline:{p.id}")


# ---------------------------------------------------------------- runs


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    kind: str | None = None,
    target: str | None = None,
    pipeline_id: uuid.UUID | None = None,
    parent_run_id: uuid.UUID | None = None,
    limit: int = 100,
    s: Session = Depends(get_session, scope="function"),
    _: Principal = Depends(current_principal),
):
    q = select(TransformRun).order_by(TransformRun.started_at.desc()).limit(min(max(limit, 1), 500))
    if kind:
        q = q.where(TransformRun.kind == kind)
    if target:
        q = q.where(TransformRun.target == target)
    if pipeline_id:
        q = q.where(TransformRun.pipeline_id == pipeline_id)
    if parent_run_id:
        q = q.where(TransformRun.parent_run_id == parent_run_id)
    return list(s.scalars(q))
