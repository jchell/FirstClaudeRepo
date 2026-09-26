"""Vault design operations shared by the API and the ingestion wizard."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dataplat.db.models import Dataset, DatasetColumn, VaultMapping, VaultObject
from dataplat.vault.loader import VaultError, resolve
from dataplat.vault.model import RawVaultSpec, mapping_keys, validate_object


def references(s: Session, name: str) -> list[str]:
    """Objects whose definition mentions ``name``."""
    out = []
    for o in s.scalars(select(VaultObject)):
        d = o.definition
        refs = {d.get("parent"), d.get("hub"), *(h["hub"] for h in d.get("hubs", []))}
        refs |= set(d.get("satellites", [])) | set(d.get("links", []))
        if name in refs and o.name != name:
            out.append(o.name)
    return out


def create_object(
    s: Session, kind: str, name: str, definition: dict[str, Any], description: str = "", user: str | None = None
) -> VaultObject:
    definition = validate_object(kind, name, definition)
    if s.scalars(select(VaultObject).where(VaultObject.name == name)).first():
        raise VaultError(f"{name} already exists")
    obj = VaultObject(kind=kind, name=name, definition=definition, description=description, created_by=user)
    s.add(obj)
    s.flush()
    try:
        resolve(s, name)  # referenced objects exist and have the right kinds
        if kind == "pit":
            for sat in definition["satellites"]:
                parent = resolve(s, sat).parts.get("parent")
                if parent is None or parent.name != definition["hub"]:
                    raise VaultError(f"{sat} is not a satellite of {definition['hub']}")
    except VaultError:
        s.delete(obj)
        s.flush()
        raise
    return obj


def update_object(s: Session, obj: VaultObject, definition: dict[str, Any], loaded: bool) -> None:
    """Hubs and links are fixed once loaded; satellites may only gain attributes."""
    new = validate_object(obj.kind, obj.name, definition)
    if loaded and obj.kind in ("hub", "link") and new != obj.definition:
        raise VaultError(f"{obj.name} already has data; its keys can't change")
    if loaded and obj.kind == "sat":
        old = obj.definition
        if (
            new["attributes"][: len(old["attributes"])] != old["attributes"]
            or new["parent"] != old["parent"]
            or (new.get("multi_active_key", []) != old.get("multi_active_key", []))
        ):
            raise VaultError(f"{obj.name} already has data; attributes can only be appended")
    obj.definition = new
    resolve(s, obj.name)


def check_mapping(
    s: Session, target: VaultObject, keys: dict[str, str], attributes: dict[str, str], source: tuple[str, str]
) -> None:
    r = resolve(s, target.name)
    owner = r.parts["parent"] if r.kind == "sat" else r
    if r.kind in ("pit", "bridge"):
        raise VaultError("PIT and bridge tables are built from the vault, not mapped from sources")
    need = mapping_keys(owner)
    if missing := [k for k in need if not keys.get(k)]:
        raise VaultError(f"map the keys {missing}")
    if extra := [k for k in keys if k not in need]:
        raise VaultError(f"unknown keys {extra}; expected {need}")
    if r.kind == "sat" and not r.definition.get("status"):
        if missing := [a for a in r.definition["attributes"] if not attributes.get(a)]:
            raise VaultError(f"map the attributes {missing}")
    elif attributes:
        raise VaultError(f"{target.kind}s have no attributes")
    cols = source_columns(s, *source)
    if cols is not None:
        if unknown := sorted({*keys.values(), *attributes.values()} - cols):
            raise VaultError(f"{source[0]}.{source[1]} has no columns {unknown}")


def source_columns(s: Session, layer: str, dataset: str) -> set[str] | None:
    ds = s.scalars(select(Dataset).where(Dataset.layer == layer, Dataset.name == dataset)).first()
    if ds is None:
        return None  # not loaded yet: checked again when the load runs
    return {
        c.name
        for c in s.scalars(
            select(DatasetColumn).where(DatasetColumn.dataset_id == ds.id, DatasetColumn.removed_at.is_(None))
        )
    }


def upsert_mapping(
    s: Session,
    target: VaultObject,
    source: tuple[str, str],
    keys: dict[str, str],
    attributes: dict[str, str] | None = None,
    *,
    record_source: str | None = None,
    ingestion_job_id: uuid.UUID | None = None,
    user: str | None = None,
) -> VaultMapping:
    attributes = attributes or {}
    check_mapping(s, target, keys, attributes, source)
    m = s.scalars(
        select(VaultMapping).where(
            VaultMapping.source_layer == source[0],
            VaultMapping.source_dataset == source[1],
            VaultMapping.target_id == target.id,
        )
    ).first()
    if m is None:
        m = VaultMapping(source_layer=source[0], source_dataset=source[1], target_id=target.id, created_by=user)
        s.add(m)
    m.keys, m.attributes, m.record_source, m.enabled = keys, attributes, record_source, True
    if ingestion_job_id:
        m.ingestion_job_id = ingestion_job_id
    s.flush()
    return m


def _hub(s: Session, name: str, bks: list[str], user: str | None) -> VaultObject:
    hub = s.scalars(select(VaultObject).where(VaultObject.name == name)).first()
    if hub is None:
        return create_object(s, "hub", name, {"business_keys": bks}, user=user)
    if hub.kind != "hub" or hub.definition["business_keys"] != bks:
        raise VaultError(f"{name} exists with business keys {hub.definition.get('business_keys')}")
    return hub


def apply_raw_vault(
    s: Session, spec: RawVaultSpec, dataset: str, job_id: uuid.UUID | None, user: str | None
) -> list[str]:
    """Creates (or reuses) the hub, satellites and links the wizard described, and maps them."""
    source = ("bronze", dataset)
    touched: list[str] = []
    hub = _hub(s, spec.hub.name, list(spec.hub.keys), user)
    upsert_mapping(s, hub, source, dict(spec.hub.keys), ingestion_job_id=job_id, user=user)
    touched.append(hub.name)
    if spec.attributes and spec.satellite:
        sat = s.scalars(select(VaultObject).where(VaultObject.name == spec.satellite)).first()
        attrs = list(spec.attributes)
        if sat is None:
            sat = create_object(s, "sat", spec.satellite, {"parent": hub.name, "attributes": attrs}, user=user)
        elif sat.definition.get("parent") != hub.name:
            raise VaultError(f"{spec.satellite} belongs to {sat.definition.get('parent')}")
        else:
            merged = sat.definition["attributes"] + [a for a in attrs if a not in sat.definition["attributes"]]
            update_object(s, sat, {**sat.definition, "attributes": merged}, loaded=True)
        if missing := [a for a in sat.definition["attributes"] if a not in attrs]:
            raise VaultError(f"{spec.satellite} also has attributes {missing}, which this source doesn't provide")
        upsert_mapping(
            s,
            sat,
            source,
            dict(spec.hub.keys),
            {a: a for a in sat.definition["attributes"]},
            ingestion_job_id=job_id,
            user=user,
        )
        touched.append(sat.name)
    if spec.track_deletes:
        name = f"sat_{hub.name.removeprefix('hub_')}_status"[:62]
        status = s.scalars(select(VaultObject).where(VaultObject.name == name)).first() or create_object(
            s, "sat", name, {"parent": hub.name, "status": True}, user=user
        )
        upsert_mapping(s, status, source, dict(spec.hub.keys), ingestion_job_id=job_id, user=user)
        touched.append(status.name)
    for ql in spec.links:
        other = _hub(s, ql.hub.name, list(ql.hub.keys), user)
        upsert_mapping(s, other, source, dict(ql.hub.keys), ingestion_job_id=job_id, user=user)
        link = s.scalars(select(VaultObject).where(VaultObject.name == ql.name)).first()
        if link is None:
            link = create_object(s, "link", ql.name, {"hubs": [{"hub": hub.name}, {"hub": other.name}]}, user=user)
        r = resolve(s, link.name)
        keys: dict[str, str] = {}
        for role, (hub_name, bks) in r.parts["roles"].items():
            cols = spec.hub.keys if hub_name == hub.name else ql.hub.keys if hub_name == other.name else None
            if cols is None:
                raise VaultError(f"{ql.name} links {hub_name}, which this source doesn't provide")
            keys |= {f"{role}.{bk}": cols[bk] for bk in bks}
        upsert_mapping(s, link, source, keys, ingestion_job_id=job_id, user=user)
        touched += [other.name, link.name]
    return touched
