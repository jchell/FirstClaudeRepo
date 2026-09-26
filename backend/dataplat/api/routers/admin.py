"""Admin: users, groups, roles, service accounts, secrets (metadata only), audit, settings."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi import Path as PathParam
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataplat.api.deps import client_ip, get_ctx, get_session, require_roles
from dataplat.core import audit
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.core.secrets import SecretNotFound, WriteOnlySecret
from dataplat.db.models import (
    AuditLog,
    Group,
    GroupMember,
    GroupRole,
    PlatformSetting,
    Role,
    ServiceAccount,
    User,
    UserRole,
)
from dataplat.security import service_accounts as sa
from dataplat.security.auth import AuthService, user_roles
from dataplat.security.passwords import check_password_policy, hash_password

router = APIRouter(prefix="/api/admin", tags=["admin"])
admin_only = require_roles("admin")


# ---------------------------------------------------------------- users & roles


class RoleOut(BaseModel):
    name: str
    description: str


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str | None
    email: str | None
    is_active: bool
    roles: list[str]
    locked: bool
    last_login_at: datetime | None
    created_at: datetime


class UserCreate(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_.@-]{2,128}$")
    display_name: str | None = None
    email: str | None = None
    password: WriteOnlySecret
    roles: list[str] = ["viewer"]

    @field_validator("password")
    @classmethod
    def _policy(cls, v):
        check_password_policy(v.get_secret_value())
        return v


class UserUpdate(BaseModel):
    display_name: str | None = None
    email: str | None = None
    is_active: bool | None = None
    roles: list[str] | None = None
    password: WriteOnlySecret | None = None
    unlock: bool = False

    @field_validator("password")
    @classmethod
    def _policy(cls, v):
        if v is not None:
            check_password_policy(v.get_secret_value())
        return v


def _user_out(s: Session, u: User) -> UserOut:
    locked = bool(u.locked_until and u.locked_until.replace(tzinfo=u.locked_until.tzinfo or UTC) > datetime.now(UTC))
    return UserOut(
        id=u.id,
        username=u.username,
        display_name=u.display_name,
        email=u.email,
        is_active=u.is_active,
        roles=sorted(user_roles(s, u.id)),
        locked=locked,
        last_login_at=u.last_login_at,
        created_at=u.created_at,
    )


def _check_roles(s: Session, roles: list[str]) -> None:
    known = set(s.scalars(select(Role.name)))
    unknown = set(roles) - known
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown roles: {sorted(unknown)}")


@router.get("/roles", response_model=list[RoleOut])
def list_roles(
    s: Session = Depends(get_session, scope="function"),
    _: Principal = Depends(require_roles("admin", "viewer", "engineer", "steward", "analyst")),
):
    return [RoleOut(name=r.name, description=r.description) for r in s.scalars(select(Role).order_by(Role.name))]


@router.get("/users", response_model=list[UserOut])
def list_users(s: Session = Depends(get_session, scope="function"), _: Principal = Depends(admin_only)):
    return [_user_out(s, u) for u in s.scalars(select(User).order_by(User.username))]


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(
    body: UserCreate,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(admin_only),
):
    _check_roles(s, body.roles)
    user = User(
        username=body.username.lower(),
        display_name=body.display_name,
        email=body.email,
        password_hash=hash_password(body.password.get_secret_value()),
    )
    s.add(user)
    try:
        s.flush()
    except IntegrityError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "username already exists") from e
    for role in set(body.roles):
        s.add(UserRole(user_id=user.id, role=role))
    s.flush()
    audit.record(
        s,
        actor=actor.name,
        action="user.create",
        target=user.username,
        detail={"roles": body.roles},
        ip=client_ip(request),
    )
    return _user_out(s, user)


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(admin_only),
):
    user = s.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    changed: list[str] = []
    if body.display_name is not None:
        user.display_name = body.display_name
        changed.append("display_name")
    if body.email is not None:
        user.email = body.email
        changed.append("email")
    if body.is_active is not None:
        if not body.is_active and str(user.id) == actor.id:
            raise HTTPException(400, "you cannot deactivate yourself")
        user.is_active = body.is_active
        changed.append("is_active")
    if body.roles is not None:
        _check_roles(s, body.roles)
        if str(user.id) == actor.id and "admin" not in body.roles:
            raise HTTPException(400, "you cannot remove your own admin role")
        s.execute(delete(UserRole).where(UserRole.user_id == user.id))
        for role in set(body.roles):
            s.add(UserRole(user_id=user.id, role=role))
        changed.append("roles")
    if body.password is not None:
        user.password_hash = hash_password(body.password.get_secret_value())
        changed.append("password")
    if body.unlock:
        user.locked_until = None
        user.failed_logins = 0
        changed.append("unlock")
    if {"is_active", "roles", "password"} & set(changed):
        # Force re-login so new roles/deactivation take effect at the next refresh.
        AuthService(ctx.identity, ctx.config.auth).revoke_user_sessions(s, user.id)
    s.flush()
    audit.record(
        s,
        actor=actor.name,
        action="user.update",
        target=user.username,
        detail={"changed": changed},
        ip=client_ip(request),
    )
    return _user_out(s, user)


# ---------------------------------------------------------------- groups


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    roles: list[str]
    members: list[str]


class GroupIn(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_. -]{2,128}$")
    description: str = ""
    roles: list[str] = []
    members: list[str] = []  # usernames


def _group_out(s: Session, g: Group) -> GroupOut:
    roles = sorted(s.scalars(select(GroupRole.role).where(GroupRole.group_id == g.id)))
    members = sorted(
        s.scalars(
            select(User.username).join(GroupMember, GroupMember.user_id == User.id).where(GroupMember.group_id == g.id)
        )
    )
    return GroupOut(id=g.id, name=g.name, description=g.description, roles=roles, members=members)


def _set_group(s: Session, g: Group, body: GroupIn) -> None:
    _check_roles(s, body.roles)
    users = {u.username: u for u in s.scalars(select(User).where(User.username.in_([m.lower() for m in body.members])))}
    missing = {m.lower() for m in body.members} - set(users)
    if missing:
        raise HTTPException(422, f"unknown users: {sorted(missing)}")
    s.execute(delete(GroupRole).where(GroupRole.group_id == g.id))
    s.execute(delete(GroupMember).where(GroupMember.group_id == g.id))
    for role in set(body.roles):
        s.add(GroupRole(group_id=g.id, role=role))
    for u in users.values():
        s.add(GroupMember(group_id=g.id, user_id=u.id))


@router.get("/groups", response_model=list[GroupOut])
def list_groups(s: Session = Depends(get_session, scope="function"), _: Principal = Depends(admin_only)):
    return [_group_out(s, g) for g in s.scalars(select(Group).order_by(Group.name))]


@router.post("/groups", response_model=GroupOut, status_code=201)
def create_group(
    body: GroupIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(admin_only),
):
    g = Group(name=body.name, description=body.description)
    s.add(g)
    try:
        s.flush()
    except IntegrityError as e:
        raise HTTPException(409, "group already exists") from e
    _set_group(s, g, body)
    s.flush()
    audit.record(s, actor=actor.name, action="group.create", target=g.name, ip=client_ip(request))
    return _group_out(s, g)


@router.put("/groups/{group_id}", response_model=GroupOut)
def update_group(
    group_id: uuid.UUID,
    body: GroupIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(admin_only),
):
    g = s.get(Group, group_id)
    if g is None:
        raise HTTPException(404, "group not found")
    g.name, g.description = body.name, body.description
    _set_group(s, g, body)
    s.flush()
    audit.record(s, actor=actor.name, action="group.update", target=g.name, ip=client_ip(request))
    return _group_out(s, g)


# ---------------------------------------------------------------- service accounts


class ServiceAccountOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    vault_path: str
    vault_policy: str
    disabled: bool
    created_by: str | None
    created_at: datetime


class ServiceAccountIn(BaseModel):
    name: str
    description: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return sa.validate_name(v)


class SecretWrite(BaseModel):
    """Write-only: values go straight to Vault and are never returned."""

    values: dict[str, WriteOnlySecret]


class SecretMetadataOut(BaseModel):
    path: str
    current_version: int
    created_time: str
    updated_time: str
    versions: int
    refs: list[str] = []


def _sa_out(row: ServiceAccount) -> ServiceAccountOut:
    return ServiceAccountOut.model_validate(row, from_attributes=True)


@router.get("/service-accounts", response_model=list[ServiceAccountOut])
def list_service_accounts(
    s: Session = Depends(get_session, scope="function"), _: Principal = Depends(require_roles("engineer"))
):
    return [_sa_out(r) for r in s.scalars(select(ServiceAccount).order_by(ServiceAccount.name))]


@router.post("/service-accounts", response_model=ServiceAccountOut, status_code=201)
def create_service_account(
    body: ServiceAccountIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(admin_only),
):
    if s.scalars(select(ServiceAccount).where(ServiceAccount.name == body.name)).first():
        raise HTTPException(409, "service account already exists")
    policy = ctx.service_account_vault.create_policy(body.name)
    row = ServiceAccount(
        name=body.name,
        description=body.description,
        vault_path=f"{ctx.config.vault.kv_mount}/{sa.kv_path(ctx.config.vault, body.name)}",
        vault_policy=policy,
        created_by=actor.name,
    )
    s.add(row)
    s.flush()
    audit.record(
        s,
        actor=actor.name,
        action="service_account.create",
        target=body.name,
        detail={"policy": policy},
        ip=client_ip(request),
    )
    return _sa_out(row)


@router.delete("/service-accounts/{name}", status_code=204)
def delete_service_account(
    name: str,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(admin_only),
):
    row = s.scalars(select(ServiceAccount).where(ServiceAccount.name == name)).first()
    if row is None:
        raise HTTPException(404, "service account not found")
    base = f"service-accounts/{name}"
    for key in ctx.secrets.list(base):
        ctx.secrets.delete(f"{base}/{key.rstrip('/')}")
    ctx.service_account_vault.delete_policy(name)
    s.delete(row)
    audit.record(s, actor=actor.name, action="service_account.delete", target=name, ip=client_ip(request))


def _sa_or_404(s: Session, name: str) -> ServiceAccount:
    row = s.scalars(select(ServiceAccount).where(ServiceAccount.name == name)).first()
    if row is None:
        raise HTTPException(404, "service account not found")
    return row


_SECRET_NAME = r"^[a-z0-9][a-z0-9_-]{0,63}$"


@router.put("/service-accounts/{name}/secrets/{secret}", response_model=SecretMetadataOut)
def write_service_account_secret(
    name: str,
    body: SecretWrite,
    request: Request,
    secret: str = PathParam(pattern=_SECRET_NAME),
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(admin_only),
):
    """Creates or rotates (new version) a secret owned by the service account."""
    _sa_or_404(s, name)
    if not body.values:
        raise HTTPException(422, "no values")
    path = f"service-accounts/{name}/{secret}"
    ctx.secrets.write(path, {k: v.get_secret_value() for k, v in body.values.items()})
    audit.record(
        s,
        actor=actor.name,
        action="secret.write",
        target=path,
        detail={"keys": sorted(body.values)},
        ip=client_ip(request),
    )
    meta = ctx.secrets.metadata(path)
    return SecretMetadataOut(**meta, refs=[str(ctx.secrets.ref(path, k)) for k in sorted(body.values)])


@router.get("/service-accounts/{name}/secrets", response_model=list[SecretMetadataOut])
def list_service_account_secrets(
    name: str,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    _: Principal = Depends(admin_only),
):
    """Metadata only (path, versions, timestamps). Values are never readable here."""
    _sa_or_404(s, name)
    base = f"service-accounts/{name}"
    out = []
    for key in ctx.secrets.list(base):
        if key.endswith("/"):
            continue
        try:
            out.append(SecretMetadataOut(**ctx.secrets.metadata(f"{base}/{key}")))
        except SecretNotFound:
            continue
    return out


@router.delete("/service-accounts/{name}/secrets/{secret}", status_code=204)
def delete_service_account_secret(
    name: str,
    request: Request,
    secret: str = PathParam(pattern=_SECRET_NAME),
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(admin_only),
):
    _sa_or_404(s, name)
    path = f"service-accounts/{name}/{secret}"
    ctx.secrets.delete(path)
    audit.record(s, actor=actor.name, action="secret.delete", target=path, ip=client_ip(request))


# ---------------------------------------------------------------- audit & settings


class AuditOut(BaseModel):
    id: int
    ts: datetime
    actor: str
    action: str
    target: str | None
    outcome: str
    detail: dict[str, Any]
    ip: str | None


@router.get("/audit", response_model=list[AuditOut])
def list_audit(
    limit: int = 100,
    action: str | None = None,
    s: Session = Depends(get_session, scope="function"),
    _: Principal = Depends(admin_only),
):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(max(limit, 1), 1000))
    if action:
        q = q.where(AuditLog.action == action)
    return [AuditOut.model_validate(r, from_attributes=True) for r in s.scalars(q)]


class SettingIn(BaseModel):
    value: Any


@router.get("/settings")
def list_settings(
    s: Session = Depends(get_session, scope="function"), _: Principal = Depends(admin_only)
) -> dict[str, Any]:
    return {r.key: r.value for r in s.scalars(select(PlatformSetting).order_by(PlatformSetting.key))}


@router.put("/settings/{key}")
def put_setting(
    key: str,
    body: SettingIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(admin_only),
) -> dict[str, Any]:
    from dataplat.core.logging import redact

    # The settings registry is for non-secret config; refuse anything that looks like a secret.
    if redact(f"{key}={body.value}") != f"{key}={body.value}":
        raise HTTPException(422, "settings must not contain secrets; store them in Vault instead")
    row = s.get(PlatformSetting, key)
    if row is None:
        row = PlatformSetting(key=key, value=body.value, updated_by=actor.name)
        s.add(row)
    else:
        row.value, row.updated_by = body.value, actor.name
    audit.record(s, actor=actor.name, action="setting.update", target=key, ip=client_ip(request))
    return {key: body.value}
