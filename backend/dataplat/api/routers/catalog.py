"""Catalog, lineage graph, ops dashboard and App Portal."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataplat.api.deps import (
    client_ip,
    current_principal,
    get_ctx,
    get_policy,
    get_session,
    require_roles,
    task_principal,
)
from dataplat.catalog import service as catalog
from dataplat.core import audit
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.db.models import (
    Dataset,
    DatasetColumn,
    DatasetProfile,
    GlossaryLink,
    GlossaryTerm,
    IngestionJob,
    IngestionRun,
    PortalApp,
    SchemaChange,
    TagAssignment,
)
from dataplat.lineage.columns import (
    annotate_governance,
    annotate_status,
    batch_trace,
    build_column_graph,
    impact,
    mask_graph,
    trace,
)
from dataplat.lineage.graph import build_graph, subgraph
from dataplat.quality.summary import dataset_dq
from dataplat.security.policy import PolicyEngine, mask_profile, read_secured

router = APIRouter(prefix="/api", tags=["catalog"])
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------- catalog


class DatasetOut(BaseModel):
    id: uuid.UUID
    layer: str
    name: str
    uri: str
    format: str
    description: str
    owner: str | None
    row_count: int | None
    size_bytes: int | None
    table_version: int | None
    last_loaded_at: datetime | None
    source_job_id: uuid.UUID | None
    freshness_sla_minutes: int | None = None
    domain: str | None = None
    columns: int = 0
    restricted: bool = False


class ColumnOut(BaseModel):
    name: str
    ordinal: int
    data_type: str
    nullable: bool
    description: str
    is_audit: bool
    removed_at: datetime | None


class DatasetDetail(DatasetOut):
    column_list: list[ColumnOut]
    schema_changes: list[dict[str, Any]]
    profile: dict[str, Any] | None
    source_job: str | None
    # governance: tags (active and suggested), glossary terms, and what this caller sees
    tags: list[dict[str, Any]] = []
    glossary: list[dict[str, Any]] = []
    masked_columns: dict[str, str] = {}
    row_filtered: bool = False
    dq: dict[str, Any] | None = None


def _ds_out(s: Session, d: Dataset, policy: PolicyEngine | None = None) -> DatasetOut:
    out = DatasetOut.model_validate(d, from_attributes=True)
    out.restricted = policy.restricted(d) if policy else False
    out.columns = s.scalar(
        select(func.count()).where(DatasetColumn.dataset_id == d.id, DatasetColumn.removed_at.is_(None))
    )
    return out


@router.get("/catalog/datasets", response_model=list[DatasetOut])
def search_datasets(
    q: str | None = None,
    layer: str | None = None,
    s: Session = Depends(get_session, scope="function"),
    policy: PolicyEngine = Depends(get_policy),
):
    readable = policy.readable_ids()
    return [_ds_out(s, d, policy) for d in catalog.search(s, q, layer) if readable is None or d.id in readable]


def _readable(s: Session, policy: PolicyEngine, dataset_id: uuid.UUID) -> Dataset:
    d = s.get(Dataset, dataset_id)
    if d is None or not policy.can_read(d):
        raise HTTPException(404, "dataset not found")  # restricted datasets aren't revealed
    return d


@router.get("/catalog/datasets/{dataset_id}", response_model=DatasetDetail)
def dataset_detail(
    dataset_id: uuid.UUID,
    s: Session = Depends(get_session, scope="function"),
    policy: PolicyEngine = Depends(get_policy),
):
    d = _readable(s, policy, dataset_id)
    pol = policy.for_dataset(d)
    cols = s.scalars(select(DatasetColumn).where(DatasetColumn.dataset_id == d.id).order_by(DatasetColumn.ordinal))
    changes = s.scalars(
        select(SchemaChange).where(SchemaChange.dataset_id == d.id).order_by(SchemaChange.ts.desc()).limit(50)
    )
    prof = s.scalars(
        select(DatasetProfile).where(DatasetProfile.dataset_id == d.id).order_by(DatasetProfile.ts.desc())
    ).first()
    job = s.get(IngestionJob, d.source_job_id) if d.source_job_id else None
    base = _ds_out(s, d, policy).model_dump()
    tags = [
        {
            "id": a.id,
            "tag": a.tag,
            "column": a.column or None,
            "status": a.status,
            "source": a.source,
            "confidence": a.confidence,
            "reason": a.reason,
        }
        for a in s.scalars(
            select(TagAssignment)
            .where(TagAssignment.dataset_id == d.id, TagAssignment.status != "rejected")
            .order_by(TagAssignment.column, TagAssignment.tag)
        )
    ]
    terms = [
        {"term_id": t.id, "name": t.name, "column": link.column or None, "status": t.status}
        for link, t in s.execute(
            select(GlossaryLink, GlossaryTerm)
            .join(GlossaryTerm, GlossaryTerm.id == GlossaryLink.term_id)
            .where(GlossaryLink.dataset_id == d.id)
        )
    ]
    return DatasetDetail(
        **base,
        column_list=[ColumnOut.model_validate(c, from_attributes=True) for c in cols],
        schema_changes=[{"ts": c.ts, "run_id": c.run_id, "changes": c.changes} for c in changes],
        profile=(
            {"ts": prof.ts, "row_count": prof.row_count, "columns": mask_profile(prof.columns, pol)} if prof else None
        ),
        source_job=job.name if job else None,
        tags=tags,
        glossary=terms,
        masked_columns=pol.masks,
        row_filtered=bool(pol.row_filters),
        dq=dataset_dq(s, d.id),
    )


class DatasetPatch(BaseModel):
    description: str | None = None
    # Minutes; 0 removes the SLA.
    freshness_sla_minutes: int | None = Field(default=None, ge=0, le=60 * 24 * 90)
    owner: str | None = None
    domain: str | None = Field(default=None, max_length=128)
    column_descriptions: dict[str, str] = {}


@router.patch("/catalog/datasets/{dataset_id}", response_model=DatasetOut)
def update_dataset(
    dataset_id: uuid.UUID,
    body: DatasetPatch,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(require_roles("engineer", "steward")),
):
    d = s.get(Dataset, dataset_id)
    if d is None:
        raise HTTPException(404, "dataset not found")
    if body.description is not None:
        d.description = body.description
    if body.owner is not None:
        d.owner = body.owner
    if body.domain is not None:
        if body.domain != d.domain and not actor.has_role("steward"):
            raise HTTPException(403, "only stewards change domains (they drive access grants)")
        d.domain = body.domain or None
    if body.freshness_sla_minutes is not None:
        d.freshness_sla_minutes = body.freshness_sla_minutes or None
    for name, desc in body.column_descriptions.items():
        col = s.get(DatasetColumn, (d.id, name))
        if col is None:
            raise HTTPException(422, f"no column {name!r}")
        col.description = desc
    audit.record(s, actor=actor.name, action="dataset.update", target=f"{d.layer}.{d.name}", ip=client_ip(request))
    s.flush()
    return _ds_out(s, d)


@router.get("/catalog/datasets/{dataset_id}/preview")
def preview_dataset(
    dataset_id: uuid.UUID,
    limit: int = 50,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(require_roles("engineer", "analyst", "steward")),
    policy: PolicyEngine = Depends(get_policy),
) -> dict[str, Any]:
    d = _readable(s, policy, dataset_id)
    pol = policy.for_dataset(d)
    table = read_secured(ctx, policy, d, limit=min(max(limit, 1), 500))
    audit.record(
        s,
        actor=actor.name,
        action="dataset.preview",
        target=f"{d.layer}.{d.name}",
        detail={"masked": sorted(pol.masks), "row_filtered": bool(pol.row_filters), "rows": table.num_rows},
    )
    return {
        "masked_columns": pol.masks,
        "row_filtered": bool(pol.row_filters),
        "columns": [{"name": f.name, "type": str(f.type)} for f in table.schema],
        "rows": [{k: _jsonable(v) for k, v in r.items()} for r in table.to_pylist()],
    }


@router.get("/catalog/datasets/{dataset_id}/profiles")
def profile_history(
    dataset_id: uuid.UUID,
    limit: int = 30,
    s: Session = Depends(get_session, scope="function"),
    policy: PolicyEngine = Depends(get_policy),
) -> list[dict[str, Any]]:
    _readable(s, policy, dataset_id)
    rows = s.scalars(
        select(DatasetProfile)
        .where(DatasetProfile.dataset_id == dataset_id)
        .order_by(DatasetProfile.ts.desc())
        .limit(limit)
    )
    return [{"ts": p.ts, "run_id": p.run_id, "row_count": p.row_count} for p in rows]


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, bool | int | float | str):
        return v
    return str(v)


class QueryIn(BaseModel):
    sql: str = Field(min_length=1, max_length=50_000)
    limit: int = Field(default=500, ge=1, le=5000)


@router.post("/query", status_code=202)
def query(
    body: QueryIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(require_roles("engineer", "analyst", "steward")),
) -> dict[str, Any]:
    """Runs a SELECT over lake tables on a worker, under the caller's data policies.

    Reference tables as in models: {{ source('gold', 'dim_customer') }}, {{ ref('model') }},
    {{ vault('hub_customer') }}. Poll /api/tasks/{task_id} for the result (handed out once).
    """
    task = ctx.jobs.enqueue(
        "data.query", {"sql": body.sql, "limit": body.limit, **task_principal(actor)}, max_attempts=1
    )
    audit.record(
        s, actor=actor.name, action="data.query", target=None, detail={"sql": body.sql[:2000]}, ip=client_ip(request)
    )
    return {"task_id": task, "kind": "data.query", "status": "queued"}


# ---------------------------------------------------------------- lineage


@router.get("/lineage/graph")
def lineage_graph(
    node: str | None = None,
    direction: Literal["upstream", "downstream", "both"] = "both",
    depth: int = 10,
    as_of: datetime | None = None,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    policy: PolicyEngine = Depends(get_policy),
) -> dict[str, Any]:
    lake = ctx.config.lake.model_dump()
    g = build_graph(s, lake, as_of)
    hidden = _hidden(g, policy)
    if node:
        if node in hidden or not any(n["id"] == node for n in g["nodes"]):
            raise HTTPException(404, "node not in the lineage graph")
        g = subgraph(g, node, direction, min(max(depth, 1), 50))
    g = annotate_status(s, g) if as_of is None else g
    return mask_graph(annotate_governance(s, g), hidden)


def _hidden(table_graph: dict[str, Any], policy: PolicyEngine) -> set[str]:
    """Dataset nodes the caller may not read: shown as anonymous placeholders."""
    readable = policy.readable_ids()
    if readable is None:
        return set()
    ids = {str(i) for i in readable}
    return {
        n["id"]
        for n in table_graph["nodes"]
        if n.get("type") == "dataset" and n.get("dataset_id") and n["dataset_id"] not in ids
    }


def _hidden_now(s: Session, ctx: PlatformContext, policy: PolicyEngine, as_of: datetime | None = None) -> set[str]:
    if policy.readable_ids() is None:
        return set()
    return _hidden(build_graph(s, ctx.config.lake.model_dump(), as_of), policy)


@router.get("/lineage/columns")
def lineage_columns(
    dataset: str,
    as_of: datetime | None = None,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    policy: PolicyEngine = Depends(get_policy),
) -> dict[str, Any]:
    """Column nodes of a dataset node, with their direct upstream/downstream columns."""
    hidden = _hidden_now(s, ctx, policy, as_of)
    if dataset in hidden:
        raise HTTPException(404, "node not in the lineage graph")
    g = mask_graph(build_column_graph(s, ctx.config.lake.model_dump(), as_of), hidden)
    cols = [n for n in g["nodes"] if n.get("dataset_node") == dataset]
    ids = {n["id"] for n in cols}
    return {
        "columns": cols,
        "upstream": [e for e in g["edges"] if e["target"] in ids],
        "downstream": [e for e in g["edges"] if e["source"] in ids],
    }


@router.get("/lineage/trace")
def lineage_trace(
    column: str,
    direction: Literal["upstream", "downstream"] = "upstream",
    as_of: datetime | None = None,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    policy: PolicyEngine = Depends(get_policy),
) -> dict[str, Any]:
    """How does this column get its value (upstream), or where does it flow (downstream)?"""
    hidden = _hidden_now(s, ctx, policy, as_of)
    g = build_column_graph(s, ctx.config.lake.model_dump(), as_of)
    target = next((n for n in g["nodes"] if n["id"] == column), None)
    if target is None or target["dataset_node"] in hidden:
        raise HTTPException(404, "column not in the lineage graph")
    return mask_graph(trace(g, column, direction), hidden)


@router.get("/lineage/impact")
def lineage_impact(
    node: str,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    policy: PolicyEngine = Depends(get_policy),
) -> dict[str, Any]:
    """What is affected if this source/table/column changes or fails."""
    lake = ctx.config.lake.model_dump()
    tg = annotate_status(s, build_graph(s, lake))
    cg = build_column_graph(s, lake)
    hidden = _hidden(tg, policy)
    col = next((n for n in cg["nodes"] if n["id"] == node), None)
    if node in hidden or (col is not None and col["dataset_node"] in hidden):
        raise HTTPException(404, "node not in the lineage graph")
    known = {n["id"] for n in tg["nodes"]} | {n["id"] for n in cg["nodes"]}
    if node not in known:
        raise HTTPException(404, "node not in the lineage graph")
    result = impact(tg, cg, node)
    if hidden:
        hidden_cols = {(n["dataset"], n["label"]) for n in cg["nodes"] if n["dataset_node"] in hidden}
        result["affected"] = [
            {**a, "id": "restricted", "name": "Restricted dataset"} if a["id"] in hidden else a
            for a in result["affected"]
        ]
        result["columns"] = [c for c in result["columns"] if tuple(c) not in hidden_cols]
    return result


@router.get("/lineage/batch/{batch_id}")
def lineage_batch(
    batch_id: str,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    policy: PolicyEngine = Depends(get_policy),
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", batch_id):
        raise HTTPException(422, "batch ids are 32 hex characters")
    result = batch_trace(s, ctx.tables, ctx.config.lake.model_dump(), batch_id)
    readable = policy.readable_ids()
    if readable is not None:
        ids = {str(i) for i in readable}
        result["datasets"] = [d for d in result["datasets"] if d["dataset_id"] in ids]
        origin = result.get("origin")
        if origin and origin.get("target"):
            layer, _, name = origin["target"].partition(".")
            ds = s.scalars(select(Dataset).where(Dataset.layer == layer, Dataset.name == name)).first()
            if ds is not None and str(ds.id) not in ids:
                result["origin"] = {"restricted": True, "status": origin["status"]}
    return result


# ---------------------------------------------------------------- ops dashboard


@router.get("/ops/summary")
def ops_summary(
    hours: int = 24, s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)
) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=min(max(hours, 1), 24 * 90))
    runs = list(
        s.execute(
            select(IngestionRun, IngestionJob.name)
            .join(IngestionJob, IngestionJob.id == IngestionRun.job_id)
            .where(IngestionRun.started_at >= since)
            .order_by(IngestionRun.started_at)
        )
    )
    done = [r for r, _ in runs if r.status in ("succeeded", "failed")]
    durations = sorted(r.duration_ms for r in done if r.duration_ms is not None)
    bucket_hours = 1 if hours <= 48 else 24

    def bucket(ts: datetime) -> datetime:
        ts = ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        return ts if bucket_hours == 1 else ts.replace(hour=0)

    # Every bucket in the range, including empty ones, so the time axis is honest.
    series: dict[str, dict[str, Any]] = {}
    t = bucket(since)
    end = datetime.now(UTC)
    while t <= end:
        series[t.isoformat()] = {"t": t.isoformat(), "succeeded": 0, "failed": 0, "running": 0, "rows": 0}
        t += timedelta(hours=bucket_hours)
    for r, _ in runs:
        key = bucket(r.started_at).isoformat()
        b = series.setdefault(key, {"t": key, "succeeded": 0, "failed": 0, "running": 0, "rows": 0})
        b[r.status] = b.get(r.status, 0) + 1
        b["rows"] += r.rows_written or 0
    per_job: dict[str, dict[str, Any]] = {}
    for r, name in runs:
        per_job[name] = {
            "job": name,
            "job_id": str(r.job_id),
            "last_status": r.status,
            "last_run": r.started_at,
            "rows_written": r.rows_written,
            "duration_ms": r.duration_ms,
        }
    return {
        "since": since,
        "totals": {
            "runs": len(runs),
            "succeeded": sum(r.status == "succeeded" for r, _ in runs),
            "failed": sum(r.status == "failed" for r, _ in runs),
            "running": sum(r.status == "running" for r, _ in runs),
            "rows_written": sum(r.rows_written or 0 for r, _ in runs),
            "bytes_read": sum(r.bytes_read or 0 for r, _ in runs),
            "files": sum(r.files or 0 for r, _ in runs),
            "p50_duration_ms": durations[len(durations) // 2] if durations else None,
            "max_duration_ms": durations[-1] if durations else None,
        },
        "series": list(series.values()),
        "jobs": sorted(per_job.values(), key=lambda j: j["job"]),
        "recent_failures": [
            {"run_id": str(r.id), "job": name, "started_at": r.started_at, "error": _ANSI.sub("", r.error or "")[:300]}
            for r, name in reversed(runs)
            if r.status == "failed"
        ][:20],
        "datasets": s.scalar(select(func.count()).select_from(Dataset)),
    }


# ---------------------------------------------------------------- app portal


class AppIn(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    url: HttpUrl
    description: str = ""
    category: Literal["report", "dashboard", "app", "notebook"] = "report"
    owner: str | None = None
    datasets: list[str] = Field(default=[], description="e.g. gold.dim_customer")


class AppOut(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    description: str
    category: str
    owner: str | None
    datasets: list[str]
    created_by: str | None
    created_at: datetime


@router.get("/portal/apps", response_model=list[AppOut])
def list_apps(s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)):
    return [
        AppOut.model_validate(a, from_attributes=True) for a in s.scalars(select(PortalApp).order_by(PortalApp.name))
    ]


def _check_datasets(refs: list[str]) -> None:
    for ref in refs:
        layer, _, name = ref.partition(".")
        if layer not in ("bronze", "silver", "gold") or not name:
            raise HTTPException(422, f"dataset references look like layer.name, got {ref!r}")


@router.post("/portal/apps", response_model=AppOut, status_code=201)
def create_app_entry(
    body: AppIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(require_roles("engineer", "analyst")),
):
    if body.url.scheme not in ("http", "https"):
        raise HTTPException(422, "only http(s) links")
    _check_datasets(body.datasets)
    app = PortalApp(**body.model_dump(mode="json"), created_by=actor.name)
    s.add(app)
    try:
        s.flush()
    except IntegrityError as e:
        raise HTTPException(409, "an app with that name exists") from e
    audit.record(s, actor=actor.name, action="portal.create", target=app.name, ip=client_ip(request))
    return AppOut.model_validate(app, from_attributes=True)


@router.put("/portal/apps/{app_id}", response_model=AppOut)
def update_app_entry(
    app_id: uuid.UUID,
    body: AppIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(require_roles("engineer", "analyst")),
):
    app = s.get(PortalApp, app_id)
    if app is None:
        raise HTTPException(404, "app not found")
    _check_datasets(body.datasets)
    for k, v in body.model_dump(mode="json").items():
        setattr(app, k, v)
    audit.record(s, actor=actor.name, action="portal.update", target=app.name, ip=client_ip(request))
    s.flush()
    return AppOut.model_validate(app, from_attributes=True)


@router.delete("/portal/apps/{app_id}", status_code=204)
def delete_app_entry(
    app_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(require_roles("engineer", "analyst")),
):
    app = s.get(PortalApp, app_id)
    if app is None:
        raise HTTPException(404, "app not found")
    s.delete(app)
    audit.record(s, actor=actor.name, action="portal.delete", target=app.name, ip=client_ip(request))
