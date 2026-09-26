"""Connections: configs in Postgres, secrets in Vault under the owning service account."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataplat.api.deps import client_ip, current_principal, get_ctx, get_session, require_roles
from dataplat.connectors.base import config_schema
from dataplat.connectors.registry import connector_types, get_connector_class
from dataplat.core import audit
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.core.secrets import SecretRef, WriteOnlySecret
from dataplat.db.models import Connection, IngestionJob, JobRow, ServiceAccount

router = APIRouter(prefix="/api", tags=["connections"])
engineer = require_roles("engineer")


class ConnectionTypeOut(BaseModel):
    type: str
    label: str
    category: str
    secret_fields: list[str]
    config_schema: dict[str, Any]


@router.get("/connection-types", response_model=list[ConnectionTypeOut])
def list_types(_: Principal = Depends(current_principal)):
    return [
        ConnectionTypeOut(
            type=c.type,
            label=c.label,
            category=c.category,
            secret_fields=list(c.secret_fields),
            config_schema=config_schema(c),
        )
        for c in connector_types().values()
    ]


class ConnectionIn(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]{1,127}$")
    type: str
    description: str = ""
    service_account: str | None = None
    config: dict[str, Any] = {}
    # Write-only: stored in Vault; the config keeps vault:// references.
    secrets: dict[str, WriteOnlySecret] = {}


class ConnectionUpdate(BaseModel):
    description: str | None = None
    config: dict[str, Any] | None = None
    secrets: dict[str, WriteOnlySecret] = {}


class ConnectionOut(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    category: str
    description: str
    service_account: str | None
    config: dict[str, Any]
    last_test_at: datetime | None
    last_test_ok: bool | None
    last_test_message: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


def _out(c: Connection) -> ConnectionOut:
    cls = connector_types().get(c.type)
    return ConnectionOut(
        id=c.id,
        name=c.name,
        type=c.type,
        category=cls.category if cls else "unknown",
        description=c.description,
        service_account=c.service_account,
        config=c.config,
        last_test_at=c.last_test_at,
        last_test_ok=c.last_test_ok,
        last_test_message=c.last_test_message,
        created_by=c.created_by,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def _set_path(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    *parents, leaf = dotted.split(".")
    for p in parents:
        cfg = cfg.setdefault(p, {})
    cfg[leaf] = value


def _get_path(cfg: dict[str, Any], dotted: str) -> Any:
    for p in dotted.split("."):
        if not isinstance(cfg, dict):
            return None
        cfg = cfg.get(p)
    return cfg


def _apply(
    ctx: PlatformContext, s: Session, conn: Connection, config: dict[str, Any], secrets: dict[str, Any]
) -> dict[str, Any]:
    """Validates config, stores secret values in Vault and returns config with references."""
    cls = get_connector_class(conn.type)
    allowed = set(cls.secret_fields)
    unknown = set(secrets) - allowed
    if unknown:
        raise HTTPException(422, f"not secret fields of {conn.type}: {sorted(unknown)}")
    for field in allowed:
        value = _get_path(config, field)
        if value not in (None, "") and not SecretRef.is_ref(value):
            raise HTTPException(422, f"{field} is a secret: send it in 'secrets', not in 'config'")
    if secrets:
        if not conn.service_account:
            raise HTTPException(422, "connections with secrets need a service account")
        # A Vault KV write replaces the whole secret, so every secret the connection
        # already has must be re-sent with it (we never read old values back).
        existing = {f for f in allowed if SecretRef.is_ref(_get_path(conn.config or {}, f))}
        if existing - set(secrets):
            raise HTTPException(422, f"re-enter all secrets when changing any of them: {sorted(existing)}")
        path = f"service-accounts/{conn.service_account}/connections/{conn.id}"
        ctx.secrets.write(path, {k: v.get_secret_value() for k, v in secrets.items()})
        for k in secrets:
            _set_path(config, k, str(ctx.secrets.ref(path, k)))
    try:
        cls.Config.model_validate(config)
    except ValidationError as e:
        raise HTTPException(
            422, "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        ) from e
    return config


@router.get("/connections", response_model=list[ConnectionOut])
def list_connections(s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)):
    return [_out(c) for c in s.scalars(select(Connection).order_by(Connection.name))]


@router.get("/connections/{connection_id}", response_model=ConnectionOut)
def get_connection(
    connection_id: uuid.UUID, s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)
):
    c = s.get(Connection, connection_id)
    if c is None:
        raise HTTPException(404, "connection not found")
    return _out(c)


@router.post("/connections", response_model=ConnectionOut, status_code=201)
def create_connection(
    body: ConnectionIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(engineer),
):
    try:
        get_connector_class(body.type)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if (
        body.service_account
        and not s.scalars(
            select(ServiceAccount).where(
                ServiceAccount.name == body.service_account, ServiceAccount.disabled.is_(False)
            )
        ).first()
    ):
        raise HTTPException(422, "unknown or disabled service account")
    conn = Connection(
        id=uuid.uuid4(),
        name=body.name,
        type=body.type,
        description=body.description,
        service_account=body.service_account,
        created_by=actor.name,
    )
    conn.config = _apply(ctx, s, conn, dict(body.config), body.secrets)
    s.add(conn)
    try:
        s.flush()
    except IntegrityError as e:
        raise HTTPException(409, "a connection with that name exists") from e
    audit.record(
        s,
        actor=actor.name,
        action="connection.create",
        target=conn.name,
        detail={"type": conn.type, "secret_keys": sorted(body.secrets)},
        ip=client_ip(request),
    )
    return _out(conn)


@router.put("/connections/{connection_id}", response_model=ConnectionOut)
def update_connection(
    connection_id: uuid.UUID,
    body: ConnectionUpdate,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(engineer),
):
    conn = s.get(Connection, connection_id)
    if conn is None:
        raise HTTPException(404, "connection not found")
    config = dict(body.config) if body.config is not None else dict(conn.config)
    # Keep existing secret references unless new values are sent.
    for field in get_connector_class(conn.type).secret_fields:
        if field not in body.secrets and SecretRef.is_ref(old := _get_path(conn.config, field)):
            _set_path(config, field, old)
    conn.config = _apply(ctx, s, conn, config, body.secrets)
    if body.description is not None:
        conn.description = body.description
    if body.secrets:
        # Debezium reads credentials when a connector starts: restart CDC connectors on rotation.
        from dataplat.streaming.debezium import connector_name

        for job in s.scalars(select(IngestionJob).where(IngestionJob.connection_id == conn.id)):
            if job.spec.get("load_mode") == "cdc" and job.enabled:
                try:
                    ctx.change_capture.restart(connector_name(job.id))
                except Exception:
                    pass  # the stream page shows the connector state
    audit.record(
        s,
        actor=actor.name,
        action="connection.update",
        target=conn.name,
        detail={"secret_keys": sorted(body.secrets)},
        ip=client_ip(request),
    )
    s.flush()
    return _out(conn)


@router.delete("/connections/{connection_id}", status_code=204)
def delete_connection(
    connection_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(engineer),
):
    conn = s.get(Connection, connection_id)
    if conn is None:
        raise HTTPException(404, "connection not found")
    if s.scalars(select(IngestionJob.id).where(IngestionJob.connection_id == conn.id)).first():
        raise HTTPException(409, "ingestion jobs still use this connection")
    if conn.service_account:
        ctx.secrets.delete(f"service-accounts/{conn.service_account}/connections/{conn.id}")
    s.delete(conn)
    audit.record(s, actor=actor.name, action="connection.delete", target=conn.name, ip=client_ip(request))


# ---------------------------------------------------------------- source tasks (run on the worker)


class DiscoverIn(BaseModel):
    pattern: str | None = None


class PreviewIn(BaseModel):
    object: str | None = None
    query: str | None = None
    path_template: str | None = None
    format: str | None = None
    format_options: dict[str, Any] = {}
    options: dict[str, Any] = {}


class TaskOut(BaseModel):
    task_id: int
    kind: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None


def _enqueue(ctx: PlatformContext, s: Session, connection_id: uuid.UUID, kind: str, payload: dict[str, Any]) -> TaskOut:
    conn = s.get(Connection, connection_id)
    if conn is None:
        raise HTTPException(404, "connection not found")
    task_id = ctx.jobs.enqueue(
        kind, {"connection_id": str(conn.id), **payload}, service_account=conn.service_account, max_attempts=1
    )
    return TaskOut(task_id=task_id, kind=kind, status="queued")


@router.post("/connections/{connection_id}/test", response_model=TaskOut, status_code=202)
def test_connection(
    connection_id: uuid.UUID,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    _: Principal = Depends(engineer),
):
    return _enqueue(ctx, s, connection_id, "connection.test", {})


@router.post("/connections/{connection_id}/discover", response_model=TaskOut, status_code=202)
def discover(
    connection_id: uuid.UUID,
    body: DiscoverIn,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    _: Principal = Depends(engineer),
):
    return _enqueue(ctx, s, connection_id, "connection.discover", {"pattern": body.pattern})


@router.post("/connections/{connection_id}/preview", response_model=TaskOut, status_code=202)
def preview(
    connection_id: uuid.UUID,
    body: PreviewIn,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    _: Principal = Depends(engineer),
):
    return _enqueue(ctx, s, connection_id, "connection.preview", {"request": body.model_dump(exclude_none=True)})


SOURCE_TASKS = {"connection.test", "connection.discover", "connection.preview"}


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int, s: Session = Depends(get_session, scope="function"), _: Principal = Depends(engineer)):
    row = s.get(JobRow, task_id)
    if row is None or row.kind not in SOURCE_TASKS:
        raise HTTPException(404, "task not found")
    out = TaskOut(
        task_id=row.id, kind=row.kind, status=row.status, result=row.result, error=_first_line(row.last_error)
    )
    if row.kind == "connection.preview" and row.status == "succeeded":
        # Source data doesn't stay in the metadata database: hand it out once.
        row.result = {"consumed": True}
    return out


def _first_line(err: str | None) -> str | None:
    return err.splitlines()[0][:500] if err else None
