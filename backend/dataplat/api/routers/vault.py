"""Data Vault designer API: hubs, links, satellites, PIT/bridge tables, mappings and loads."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from dataplat.adapters.pg_queue import COALESCE_PREFIX, DuplicateJob
from dataplat.api.deps import client_ip, current_principal, get_ctx, get_session, require_roles
from dataplat.core import audit
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.db.models import Dataset, TransformRun, VaultLoadState, VaultMapping, VaultObject
from dataplat.vault import service
from dataplat.vault.loader import VaultError, resolve
from dataplat.vault.model import hash_key_column, mapping_keys

router = APIRouter(prefix="/api/vault", tags=["vault"])
engineer = require_roles("engineer")


class ObjectIn(BaseModel):
    kind: Literal["hub", "link", "sat", "pit", "bridge"]
    name: str
    definition: dict[str, Any]
    description: str = ""


class ObjectUpdate(BaseModel):
    definition: dict[str, Any] | None = None
    description: str | None = None


class MappingIn(BaseModel):
    source_layer: Literal["bronze", "silver"] = "bronze"
    source_dataset: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    target: str
    keys: dict[str, str]
    attributes: dict[str, str] = {}
    record_source: str | None = Field(default=None, max_length=256)


class MappingOut(BaseModel):
    id: uuid.UUID
    source: str
    target: str
    target_kind: str
    keys: dict[str, str]
    attributes: dict[str, str]
    record_source: str | None
    ingestion_job_id: uuid.UUID | None
    enabled: bool
    high_water: datetime | None
    last_loaded_at: datetime | None
    last_rows: int | None


class ObjectOut(BaseModel):
    id: uuid.UUID
    kind: str
    name: str
    definition: dict[str, Any]
    description: str
    hash_key: str | None
    required_keys: list[str]
    columns: list[str]
    rows: int | None
    last_loaded_at: datetime | None
    dataset_id: uuid.UUID | None
    mappings: int
    created_by: str | None
    created_at: datetime


def _status(e: VaultError) -> HTTPException:
    return HTTPException(422, str(e))


def _columns(obj: VaultObject, s: Session) -> list[str]:
    d = obj.definition
    base = ["load_date", "record_source", "_batch_id"]
    try:
        r = resolve(s, obj.name)
    except VaultError:
        return []
    if obj.kind == "hub":
        return [r.hk, *d["business_keys"], *base]
    if obj.kind == "link":
        return [r.hk, *(f"hk_{role}" for role in r.parts["roles"]), *base]
    if obj.kind == "sat":
        return [r.parts["parent"].hk, "load_date", "hashdiff", *d["attributes"], "record_source", "_batch_id"]
    if obj.kind == "pit":
        return [r.parts["hub"].hk, "snapshot_date", *(f"{m}_ldts" for m in d["satellites"])]
    return [r.parts["hub"].hk, *(hash_key_column(m) for m in d["links"])]


def _object_out(s: Session, obj: VaultObject, datasets: dict[str, Dataset], counts: dict[uuid.UUID, int]) -> ObjectOut:
    ds = datasets.get(obj.name)
    try:
        r = resolve(s, obj.name)
        owner = r.parts["parent"] if r.kind == "sat" else r
        required = mapping_keys(owner) if owner.kind in ("hub", "link") else []
        hk = r.hk if r.kind in ("hub", "link") else None
    except VaultError:
        required, hk = [], None
    return ObjectOut(
        id=obj.id,
        kind=obj.kind,
        name=obj.name,
        definition=obj.definition,
        description=obj.description,
        hash_key=hk,
        required_keys=required,
        columns=_columns(obj, s),
        rows=ds.row_count if ds else None,
        last_loaded_at=ds.last_loaded_at if ds else None,
        dataset_id=ds.id if ds else None,
        mappings=counts.get(obj.id, 0),
        created_by=obj.created_by,
        created_at=obj.created_at,
    )


def _lookups(s: Session) -> tuple[dict[str, Dataset], dict[uuid.UUID, int]]:
    datasets = {d.name: d for d in s.scalars(select(Dataset).where(Dataset.layer == "vault"))}
    counts: dict[uuid.UUID, int] = {}
    for m in s.scalars(select(VaultMapping)):
        counts[m.target_id] = counts.get(m.target_id, 0) + 1
    return datasets, counts


def _get(s: Session, name: str) -> VaultObject:
    obj = s.scalars(select(VaultObject).where(VaultObject.name == name)).first()
    if obj is None:
        raise HTTPException(404, "vault object not found")
    return obj


@router.get("/objects", response_model=list[ObjectOut])
def list_objects(s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)):
    datasets, counts = _lookups(s)
    order = {"hub": 0, "link": 1, "sat": 2, "pit": 3, "bridge": 4}
    objs = sorted(s.scalars(select(VaultObject)), key=lambda o: (order[o.kind], o.name))
    return [_object_out(s, o, datasets, counts) for o in objs]


@router.get("/objects/{name}")
def get_object(
    name: str, s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)
):
    obj = _get(s, name)
    datasets, counts = _lookups(s)
    runs = s.scalars(
        select(TransformRun)
        .where(TransformRun.kind.in_(["vault_load", "vault_build"]))
        .order_by(TransformRun.started_at.desc())
        .limit(200)
    )
    recent = [
        {
            "id": str(r.id),
            "kind": r.kind,
            "source": r.target,
            "status": r.status,
            "started_at": r.started_at,
            "duration_ms": r.duration_ms,
            "rows": next((t["rows"] for t in r.details.get("targets", []) if t["target"] == name), r.rows_written),
            "error": r.error,
        }
        for r in runs
        if r.target == f"vault.{name}" or any(t.get("target") == name for t in r.details.get("targets", []))
    ][:20]
    return {
        **_object_out(s, obj, datasets, counts).model_dump(),
        "mappings": [m.model_dump() for m in _mappings(s, target_id=obj.id)],
        "used_by": service.references(s, name),
        "runs": recent,
    }


@router.post("/objects", response_model=ObjectOut, status_code=201)
def create_object(
    body: ObjectIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(engineer),
):
    try:
        obj = service.create_object(s, body.kind, body.name, body.definition, body.description, actor.name)
    except (VaultError, ValueError) as e:
        raise HTTPException(422, str(e).splitlines()[-1] if "validation error" in str(e) else str(e)) from e
    audit.record(s, actor=actor.name, action="vault.create", target=obj.name, ip=client_ip(request))
    datasets, counts = _lookups(s)
    return _object_out(s, obj, datasets, counts)


@router.put("/objects/{name}", response_model=ObjectOut)
def update_object(
    name: str,
    body: ObjectUpdate,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(engineer),
):
    obj = _get(s, name)
    if body.description is not None:
        obj.description = body.description
    if body.definition is not None:
        loaded = ctx.tables.exists(ctx.config.lake.uri("vault", name))
        try:
            service.update_object(s, obj, body.definition, loaded)
        except (VaultError, ValueError) as e:
            raise HTTPException(422, str(e)) from e
    audit.record(s, actor=actor.name, action="vault.update", target=name, ip=client_ip(request))
    datasets, counts = _lookups(s)
    return _object_out(s, obj, datasets, counts)


@router.delete("/objects/{name}", status_code=204)
def delete_object(
    name: str,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(engineer),
):
    obj = _get(s, name)
    if refs := service.references(s, name):
        raise HTTPException(409, f"{name} is used by {', '.join(refs)}")
    s.delete(obj)  # mappings go with it; the Delta table is kept
    audit.record(s, actor=actor.name, action="vault.delete", target=name, ip=client_ip(request))


# ---------------------------------------------------------------- mappings


def _mappings(
    s: Session, target_id: uuid.UUID | None = None, source: tuple[str, str] | None = None
) -> list[MappingOut]:
    q = select(VaultMapping, VaultObject).join(VaultObject, VaultObject.id == VaultMapping.target_id)
    if target_id:
        q = q.where(VaultMapping.target_id == target_id)
    if source:
        q = q.where(VaultMapping.source_layer == source[0], VaultMapping.source_dataset == source[1])
    out = []
    for m, obj in s.execute(q.order_by(VaultMapping.source_dataset, VaultObject.name)):
        st = s.get(VaultLoadState, m.id)
        out.append(
            MappingOut(
                id=m.id,
                source=f"{m.source_layer}.{m.source_dataset}",
                target=obj.name,
                target_kind=obj.kind,
                keys=m.keys,
                attributes=m.attributes,
                record_source=m.record_source,
                ingestion_job_id=m.ingestion_job_id,
                enabled=m.enabled,
                high_water=st.high_water if st else None,
                last_loaded_at=st.last_loaded_at if st else None,
                last_rows=st.last_rows if st else None,
            )
        )
    return out


@router.get("/mappings", response_model=list[MappingOut])
def list_mappings(
    source: str | None = None,
    s: Session = Depends(get_session, scope="function"),
    _: Principal = Depends(current_principal),
):
    src = tuple(source.split(".", 1)) if source and "." in source else None
    return _mappings(s, source=src)  # type: ignore[arg-type]


@router.post("/mappings", response_model=MappingOut, status_code=201)
def upsert_mapping(
    body: MappingIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(engineer),
):
    target = _get(s, body.target)
    try:
        m = service.upsert_mapping(
            s,
            target,
            (body.source_layer, body.source_dataset),
            body.keys,
            body.attributes,
            record_source=body.record_source,
            user=actor.name,
        )
    except VaultError as e:
        raise _status(e) from e
    audit.record(
        s,
        actor=actor.name,
        action="vault.map",
        target=f"{body.source_layer}.{body.source_dataset} -> {target.name}",
        ip=client_ip(request),
    )
    return next(x for x in _mappings(s, target_id=target.id) if x.id == m.id)


@router.delete("/mappings/{mapping_id}", status_code=204)
def delete_mapping(
    mapping_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(engineer),
):
    m = s.get(VaultMapping, mapping_id)
    if m is None:
        raise HTTPException(404, "mapping not found")
    s.delete(m)
    audit.record(s, actor=actor.name, action="vault.unmap", target=str(mapping_id), ip=client_ip(request))


# ---------------------------------------------------------------- loads and diagram


class LoadIn(BaseModel):
    source_layer: Literal["bronze", "silver"] = "bronze"
    source_dataset: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


def _enqueue(ctx: PlatformContext, kind: str, payload: dict[str, Any], key: str) -> dict[str, Any]:
    try:
        task = ctx.jobs.enqueue(kind, payload, dedupe_key=f"{COALESCE_PREFIX}{key}")
    except DuplicateJob:
        return {"queued": False, "detail": "a run is already waiting"}
    return {"queued": True, "task_id": task}


@router.post("/load", status_code=202)
def load(body: LoadIn, ctx: PlatformContext = Depends(get_ctx), _: Principal = Depends(engineer)):
    return _enqueue(
        ctx,
        "vault.load",
        {"layer": body.source_layer, "dataset": body.source_dataset, "trigger": "manual"},
        f"vault:{body.source_layer}.{body.source_dataset}",
    )


@router.post("/objects/{name}/build", status_code=202)
def build(
    name: str,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    _: Principal = Depends(engineer),
):
    obj = _get(s, name)
    if obj.kind not in ("pit", "bridge"):
        raise HTTPException(422, "only PIT and bridge tables are built; hubs, links and satellites are loaded")
    return _enqueue(ctx, "vault.build", {"name": name, "trigger": "manual"}, f"vault-build:{name}")


@router.get("/diagram")
def diagram(
    s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)
) -> dict[str, Any]:
    """Objects as nodes; edges link->hub, sat->parent, pit/bridge->members, source->target."""
    nodes, edges = [], []
    for o in s.scalars(select(VaultObject)):
        nodes.append({"id": o.name, "kind": o.kind, "label": o.name})
        d = o.definition
        if o.kind == "link":
            edges += [{"source": o.name, "target": h["hub"], "kind": "references"} for h in d["hubs"]]
        elif o.kind == "sat":
            edges.append({"source": o.name, "target": d["parent"], "kind": "describes"})
        elif o.kind in ("pit", "bridge"):
            edges.append({"source": o.name, "target": d["hub"], "kind": "anchored"})
            for m in d.get("satellites", []) + d.get("links", []):
                edges.append({"source": o.name, "target": m, "kind": "includes"})
    sources = set()
    for m, obj in s.execute(
        select(VaultMapping, VaultObject).join(VaultObject, VaultObject.id == VaultMapping.target_id)
    ):
        src = f"{m.source_layer}.{m.source_dataset}"
        if src not in sources:
            nodes.append({"id": src, "kind": "source", "label": src})
            sources.add(src)
        edges.append({"source": src, "target": obj.name, "kind": "loads"})
    return {"nodes": nodes, "edges": edges}
