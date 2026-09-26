"""Governance: tags and classifications, glossary, access grants, masking and row filters."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataplat.adapters.pg_queue import COALESCE_PREFIX, DuplicateJob
from dataplat.api.deps import client_ip, current_principal, get_ctx, get_session, require_roles
from dataplat.catalog.classify import CLASSIFICATIONS, ensure_classifications
from dataplat.core import audit
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.db.models import (
    AccessGrant,
    Dataset,
    DatasetColumn,
    GlossaryLink,
    GlossaryTerm,
    MaskingPolicy,
    Role,
    RowFilter,
    Tag,
    TagAssignment,
)
from dataplat.security.policy import MASK_METHODS, PolicyEngine, validate_predicate

router = APIRouter(prefix="/api/governance", tags=["governance"])
steward = require_roles("steward")
TAG_NAME = r"^[a-z][a-z0-9_.-]{0,127}$"


def _dataset(s: Session, dataset_id: uuid.UUID) -> Dataset:
    ds = s.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    return ds


def _column(s: Session, ds: Dataset, column: str | None) -> str:
    if not column:
        return ""
    if s.get(DatasetColumn, (ds.id, column)) is None:
        raise HTTPException(422, f"{ds.layer}.{ds.name} has no column {column!r}")
    return column


# ---------------------------------------------------------------- tags


class TagIn(BaseModel):
    name: str = Field(pattern=TAG_NAME)
    category: Literal["general", "classification"] = "general"
    description: str = ""


@router.get("/tags")
def list_tags(s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)):
    ensure_classifications(s)
    counts: dict[str, int] = {}
    for a in s.scalars(select(TagAssignment).where(TagAssignment.status == "active")):
        counts[a.tag] = counts.get(a.tag, 0) + 1
    policies = {m.tag: m.name for m in s.scalars(select(MaskingPolicy).where(MaskingPolicy.enabled))}
    return [
        {
            "name": t.name,
            "category": t.category,
            "description": t.description,
            "uses": counts.get(t.name, 0),
            "masking_policy": policies.get(t.name),
            "builtin": t.name in CLASSIFICATIONS,
        }
        for t in s.scalars(select(Tag).order_by(Tag.category, Tag.name))
    ]


@router.post("/tags", status_code=201)
def create_tag(
    body: TagIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    if s.get(Tag, body.name):
        raise HTTPException(409, "tag exists")
    s.add(Tag(name=body.name, category=body.category, description=body.description, created_by=actor.name))
    audit.record(s, actor=actor.name, action="tag.create", target=body.name, ip=client_ip(request))
    return body


@router.delete("/tags/{name}", status_code=204)
def delete_tag(
    name: str,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    tag = s.get(Tag, name)
    if tag is None:
        raise HTTPException(404, "tag not found")
    if name in CLASSIFICATIONS:
        raise HTTPException(409, "built-in classifications can't be deleted")
    s.delete(tag)
    audit.record(s, actor=actor.name, action="tag.delete", target=name, ip=client_ip(request))


class AssignIn(BaseModel):
    tag: str = Field(pattern=TAG_NAME)
    dataset_id: uuid.UUID
    column: str | None = None


def _assignment_out(a: TagAssignment, ds: Dataset | None) -> dict[str, Any]:
    return {
        "id": a.id,
        "tag": a.tag,
        "dataset_id": a.dataset_id,
        "dataset": f"{ds.layer}.{ds.name}" if ds else None,
        "column": a.column or None,
        "status": a.status,
        "source": a.source,
        "confidence": a.confidence,
        "reason": a.reason,
        "assigned_by": a.assigned_by,
        "updated_at": a.updated_at,
    }


@router.get("/assignments")
def list_assignments(
    status: Literal["active", "suggested", "rejected"] | None = None,
    dataset_id: uuid.UUID | None = None,
    tag: str | None = None,
    s: Session = Depends(get_session, scope="function"),
    _: Principal = Depends(current_principal),
):
    q = select(TagAssignment, Dataset).join(Dataset, Dataset.id == TagAssignment.dataset_id)
    if status:
        q = q.where(TagAssignment.status == status)
    if dataset_id:
        q = q.where(TagAssignment.dataset_id == dataset_id)
    if tag:
        q = q.where(TagAssignment.tag == tag)
    q = q.order_by(TagAssignment.confidence.desc().nulls_last(), Dataset.name, TagAssignment.column).limit(2000)
    return [_assignment_out(a, d) for a, d in s.execute(q)]


@router.post("/assignments", status_code=201)
def assign(
    body: AssignIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    ds = _dataset(s, body.dataset_id)
    column = _column(s, ds, body.column)
    if s.get(Tag, body.tag) is None:
        ensure_classifications(s)
        if s.get(Tag, body.tag) is None:
            raise HTTPException(422, f"unknown tag {body.tag!r}")
    a = s.scalars(
        select(TagAssignment).where(
            TagAssignment.tag == body.tag, TagAssignment.dataset_id == ds.id, TagAssignment.column == column
        )
    ).first()
    if a is None:
        a = TagAssignment(tag=body.tag, dataset_id=ds.id, column=column)
        s.add(a)
    a.status, a.source, a.assigned_by, a.reason = "active", "manual", actor.name, a.reason or ""
    s.flush()
    audit.record(
        s,
        actor=actor.name,
        action="tag.assign",
        target=f"{ds.layer}.{ds.name}.{column or '*'}",
        detail={"tag": body.tag},
        ip=client_ip(request),
    )
    return _assignment_out(a, ds)


class ReviewIn(BaseModel):
    status: Literal["active", "rejected"]


@router.patch("/assignments/{assignment_id}")
def review(
    assignment_id: int,
    body: ReviewIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    """Accepts or rejects a (suggested) classification."""
    a = s.get(TagAssignment, assignment_id)
    if a is None:
        raise HTTPException(404, "assignment not found")
    a.status, a.assigned_by = body.status, actor.name
    ds = s.get(Dataset, a.dataset_id)
    audit.record(
        s,
        actor=actor.name,
        action=f"tag.{'accept' if body.status == 'active' else 'reject'}",
        target=f"{ds.layer}.{ds.name}.{a.column or '*'}",
        detail={"tag": a.tag, "source": a.source},
        ip=client_ip(request),
    )
    s.flush()
    return _assignment_out(a, ds)


@router.delete("/assignments/{assignment_id}", status_code=204)
def unassign(
    assignment_id: int,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    a = s.get(TagAssignment, assignment_id)
    if a is None:
        raise HTTPException(404, "assignment not found")
    s.delete(a)
    audit.record(
        s,
        actor=actor.name,
        action="tag.unassign",
        target=str(assignment_id),
        detail={"tag": a.tag},
        ip=client_ip(request),
    )


@router.post("/classify", status_code=202)
def classify(ctx: PlatformContext = Depends(get_ctx), _: Principal = Depends(steward)):
    """Scans the catalog for PII (names, value patterns, lineage) on a worker."""
    try:
        return {
            "queued": True,
            "task_id": ctx.jobs.enqueue("governance.classify", {}, dedupe_key=f"{COALESCE_PREFIX}classify"),
        }
    except DuplicateJob:
        return {"queued": False, "detail": "a scan is already waiting"}


# ---------------------------------------------------------------- glossary


class TermIn(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    definition: str = ""
    synonyms: list[str] = []
    domain: str | None = None
    status: Literal["draft", "approved", "deprecated"] = "draft"


def _term_out(s: Session, t: GlossaryTerm) -> dict[str, Any]:
    links = [
        {"dataset_id": link.dataset_id, "dataset": f"{d.layer}.{d.name}", "column": link.column or None}
        for link, d in s.execute(
            select(GlossaryLink, Dataset)
            .join(Dataset, Dataset.id == GlossaryLink.dataset_id)
            .where(GlossaryLink.term_id == t.id)
        )
    ]
    return {
        "id": t.id,
        "name": t.name,
        "definition": t.definition,
        "synonyms": t.synonyms,
        "domain": t.domain,
        "owner": t.owner,
        "status": t.status,
        "links": links,
        "updated_at": t.updated_at,
    }


@router.get("/glossary")
def list_terms(
    q: str | None = None, s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)
):
    stmt = select(GlossaryTerm).order_by(GlossaryTerm.name)
    if q:
        stmt = stmt.where(GlossaryTerm.name.ilike(f"%{q}%") | GlossaryTerm.definition.ilike(f"%{q}%"))
    return [_term_out(s, t) for t in s.scalars(stmt)]


@router.post("/glossary", status_code=201)
def create_term(
    body: TermIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    t = GlossaryTerm(**body.model_dump(), owner=actor.name, created_by=actor.name)
    s.add(t)
    try:
        s.flush()
    except IntegrityError as e:
        raise HTTPException(409, "a term with that name exists") from e
    audit.record(s, actor=actor.name, action="glossary.create", target=t.name, ip=client_ip(request))
    return _term_out(s, t)


@router.put("/glossary/{term_id}")
def update_term(
    term_id: uuid.UUID,
    body: TermIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    t = s.get(GlossaryTerm, term_id)
    if t is None:
        raise HTTPException(404, "term not found")
    for k, v in body.model_dump().items():
        setattr(t, k, v)
    audit.record(s, actor=actor.name, action="glossary.update", target=t.name, ip=client_ip(request))
    s.flush()
    return _term_out(s, t)


@router.delete("/glossary/{term_id}", status_code=204)
def delete_term(
    term_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    t = s.get(GlossaryTerm, term_id)
    if t is None:
        raise HTTPException(404, "term not found")
    s.delete(t)
    audit.record(s, actor=actor.name, action="glossary.delete", target=t.name, ip=client_ip(request))


class LinkIn(BaseModel):
    dataset_id: uuid.UUID
    column: str | None = None


@router.post("/glossary/{term_id}/links", status_code=201)
def link_term(
    term_id: uuid.UUID,
    body: LinkIn,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(require_roles("steward", "engineer")),
):
    t = s.get(GlossaryTerm, term_id)
    if t is None:
        raise HTTPException(404, "term not found")
    ds = _dataset(s, body.dataset_id)
    column = _column(s, ds, body.column)
    if s.get(GlossaryLink, (t.id, ds.id, column)) is None:
        s.add(GlossaryLink(term_id=t.id, dataset_id=ds.id, column=column))
    s.flush()
    return _term_out(s, t)


@router.delete("/glossary/{term_id}/links", status_code=204)
def unlink_term(
    term_id: uuid.UUID,
    dataset_id: uuid.UUID,
    column: str | None = None,
    s: Session = Depends(get_session, scope="function"),
    _: Principal = Depends(require_roles("steward", "engineer")),
):
    s.execute(
        delete(GlossaryLink).where(
            GlossaryLink.term_id == term_id,
            GlossaryLink.dataset_id == dataset_id,
            GlossaryLink.column == (column or ""),
        )
    )


# ---------------------------------------------------------------- access grants


class GrantIn(BaseModel):
    target_type: Literal["dataset", "domain"]
    target: str = Field(min_length=1, max_length=256)  # dataset id or domain name
    principal_type: Literal["role", "user"]
    principal: str = Field(min_length=1, max_length=128)


@router.get("/grants")
def list_grants(s: Session = Depends(get_session, scope="function"), _: Principal = Depends(steward)):
    names = {str(d.id): f"{d.layer}.{d.name}" for d in s.scalars(select(Dataset))}
    return [
        {
            "id": g.id,
            "target_type": g.target_type,
            "target": g.target,
            "target_name": names.get(g.target, g.target) if g.target_type == "dataset" else g.target,
            "principal_type": g.principal_type,
            "principal": g.principal,
            "created_by": g.created_by,
            "created_at": g.created_at,
        }
        for g in s.scalars(select(AccessGrant).order_by(AccessGrant.target_type, AccessGrant.target))
    ]


@router.post("/grants", status_code=201)
def create_grant(
    body: GrantIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    if body.target_type == "dataset":
        try:
            _dataset(s, uuid.UUID(body.target))
        except ValueError as e:
            raise HTTPException(422, "dataset grants target a dataset id") from e
    if body.principal_type == "role" and s.get(Role, body.principal) is None:
        raise HTTPException(422, f"unknown role {body.principal!r}")
    g = AccessGrant(**body.model_dump(), created_by=actor.name)
    s.add(g)
    try:
        s.flush()
    except IntegrityError as e:
        raise HTTPException(409, "that grant exists") from e
    audit.record(
        s,
        actor=actor.name,
        action="grant.create",
        target=f"{body.target_type}:{body.target}",
        detail={"to": f"{body.principal_type}:{body.principal}"},
        ip=client_ip(request),
    )
    return {"id": g.id, **body.model_dump()}


@router.delete("/grants/{grant_id}", status_code=204)
def delete_grant(
    grant_id: int,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    g = s.get(AccessGrant, grant_id)
    if g is None:
        raise HTTPException(404, "grant not found")
    s.delete(g)
    audit.record(
        s,
        actor=actor.name,
        action="grant.delete",
        target=f"{g.target_type}:{g.target}",
        detail={"to": f"{g.principal_type}:{g.principal}"},
        ip=client_ip(request),
    )


# ---------------------------------------------------------------- masking and row filters


class MaskingIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    tag: str = Field(pattern=TAG_NAME)
    method: Literal[MASK_METHODS] = "redact"  # type: ignore[valid-type]
    exempt_roles: list[str] = []
    description: str = ""
    enabled: bool = True


def _check_roles(s: Session, roles: list[str]) -> None:
    known = {r.name for r in s.scalars(select(Role))}
    if unknown := [r for r in roles if r not in known]:
        raise HTTPException(422, f"unknown roles {unknown}")


def _masking_out(m: MaskingPolicy) -> dict[str, Any]:
    return {
        "id": m.id,
        "name": m.name,
        "tag": m.tag,
        "method": m.method,
        "exempt_roles": m.exempt_roles,
        "description": m.description,
        "enabled": m.enabled,
        "created_by": m.created_by,
    }


@router.get("/masking")
def list_masking(s: Session = Depends(get_session, scope="function"), _: Principal = Depends(current_principal)):
    return [_masking_out(m) for m in s.scalars(select(MaskingPolicy).order_by(MaskingPolicy.name))]


@router.post("/masking", status_code=201)
def create_masking(
    body: MaskingIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    ensure_classifications(s)
    if s.get(Tag, body.tag) is None:
        raise HTTPException(422, f"unknown tag {body.tag!r}")
    _check_roles(s, body.exempt_roles)
    m = MaskingPolicy(**body.model_dump(), created_by=actor.name)
    s.add(m)
    try:
        s.flush()
    except IntegrityError as e:
        raise HTTPException(409, "a policy with that name exists") from e
    audit.record(
        s, actor=actor.name, action="masking.create", target=m.name, detail=body.model_dump(), ip=client_ip(request)
    )
    return _masking_out(m)


@router.put("/masking/{policy_id}")
def update_masking(
    policy_id: uuid.UUID,
    body: MaskingIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    m = s.get(MaskingPolicy, policy_id)
    if m is None:
        raise HTTPException(404, "policy not found")
    if s.get(Tag, body.tag) is None:
        raise HTTPException(422, f"unknown tag {body.tag!r}")
    _check_roles(s, body.exempt_roles)
    for k, v in body.model_dump().items():
        setattr(m, k, v)
    audit.record(
        s, actor=actor.name, action="masking.update", target=m.name, detail=body.model_dump(), ip=client_ip(request)
    )
    s.flush()
    return _masking_out(m)


@router.delete("/masking/{policy_id}", status_code=204)
def delete_masking(
    policy_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    m = s.get(MaskingPolicy, policy_id)
    if m is None:
        raise HTTPException(404, "policy not found")
    s.delete(m)
    audit.record(s, actor=actor.name, action="masking.delete", target=m.name, ip=client_ip(request))


class RowFilterIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    dataset_id: uuid.UUID
    predicate: str = Field(min_length=1, max_length=2000)
    exempt_roles: list[str] = []
    description: str = ""
    enabled: bool = True


def _filter_out(s: Session, f: RowFilter) -> dict[str, Any]:
    ds = s.get(Dataset, f.dataset_id)
    return {
        "id": f.id,
        "name": f.name,
        "dataset_id": f.dataset_id,
        "dataset": f"{ds.layer}.{ds.name}" if ds else None,
        "predicate": f.predicate,
        "exempt_roles": f.exempt_roles,
        "description": f.description,
        "enabled": f.enabled,
        "created_by": f.created_by,
    }


def _check_predicate(s: Session, ctx: PlatformContext, ds: Dataset, predicate: str) -> str:
    """Valid syntax, and it runs against the dataset's columns."""
    from dataplat.transform.sandbox import sandbox

    try:
        predicate = validate_predicate(predicate)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if ctx.tables.exists(ds.uri):
        try:
            with sandbox({"src": ctx.tables.dataset(ds.uri)}) as sb:
                sb.query(f"select count(*) from (select * from src limit 0) src where ({predicate})")
        except Exception as e:
            raise HTTPException(
                422, f"the condition doesn't run on {ds.layer}.{ds.name}: {str(e).splitlines()[0]}"
            ) from e
    return predicate


@router.get("/row-filters")
def list_filters(s: Session = Depends(get_session, scope="function"), _: Principal = Depends(steward)):
    return [_filter_out(s, f) for f in s.scalars(select(RowFilter).order_by(RowFilter.name))]


@router.post("/row-filters", status_code=201)
def create_filter(
    body: RowFilterIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(steward),
):
    ds = _dataset(s, body.dataset_id)
    _check_roles(s, body.exempt_roles)
    f = RowFilter(
        **{**body.model_dump(), "predicate": _check_predicate(s, ctx, ds, body.predicate)}, created_by=actor.name
    )
    s.add(f)
    try:
        s.flush()
    except IntegrityError as e:
        raise HTTPException(409, "a filter with that name exists") from e
    audit.record(
        s,
        actor=actor.name,
        action="row_filter.create",
        target=f.name,
        detail={"dataset": f"{ds.layer}.{ds.name}", "predicate": f.predicate},
        ip=client_ip(request),
    )
    return _filter_out(s, f)


@router.put("/row-filters/{filter_id}")
def update_filter(
    filter_id: uuid.UUID,
    body: RowFilterIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    actor: Principal = Depends(steward),
):
    f = s.get(RowFilter, filter_id)
    if f is None:
        raise HTTPException(404, "filter not found")
    ds = _dataset(s, body.dataset_id)
    _check_roles(s, body.exempt_roles)
    for k, v in body.model_dump().items():
        setattr(f, k, v)
    f.predicate = _check_predicate(s, ctx, ds, body.predicate)
    audit.record(
        s,
        actor=actor.name,
        action="row_filter.update",
        target=f.name,
        detail={"predicate": f.predicate},
        ip=client_ip(request),
    )
    s.flush()
    return _filter_out(s, f)


@router.delete("/row-filters/{filter_id}", status_code=204)
def delete_filter(
    filter_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    f = s.get(RowFilter, filter_id)
    if f is None:
        raise HTTPException(404, "filter not found")
    s.delete(f)
    audit.record(s, actor=actor.name, action="row_filter.delete", target=f.name, ip=client_ip(request))


@router.get("/effective")
def effective(
    dataset_id: uuid.UUID,
    role: str,
    s: Session = Depends(get_session, scope="function"),
    _: Principal = Depends(steward),
) -> dict[str, Any]:
    """What a user holding only ``role`` gets from a dataset (for checking policies)."""
    ds = _dataset(s, dataset_id)
    engine = PolicyEngine(s, Principal(id="preview", name=f"(any {role})", kind="user", roles=frozenset({role})))
    pol = engine.for_dataset(ds)
    return {
        "dataset": f"{ds.layer}.{ds.name}",
        "role": role,
        "readable": pol.readable,
        "restricted": pol.restricted,
        "masks": pol.masks,
        "row_filters": pol.row_filters,
    }
