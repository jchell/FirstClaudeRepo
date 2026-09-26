"""Catalog registration, schema-drift detection and search."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from dataplat.db.models import Dataset, DatasetColumn, SchemaChange

AUDIT_COLUMNS = ("_load_ts", "_source", "_batch_id", "_file")


def register(
    s: Session,
    *,
    layer: str,
    name: str,
    uri: str,
    schema: pa.Schema,
    run_id: uuid.UUID | None = None,
    source_job_id: uuid.UUID | None = None,
    owner: str | None = None,
    stats: dict[str, int] | None = None,
    table_version: int | None = None,
) -> tuple[Dataset, list[dict[str, Any]]]:
    """Creates or refreshes a dataset and its columns. Returns (dataset, schema changes)."""
    now = datetime.now(UTC)
    ds = s.scalars(select(Dataset).where(Dataset.layer == layer, Dataset.name == name)).first()
    if ds is None:
        ds = Dataset(layer=layer, name=name, uri=uri, format="delta", owner=owner, source_job_id=source_job_id)
        s.add(ds)
        s.flush()
    ds.uri = uri
    ds.last_loaded_at = now
    ds.table_version = table_version
    if stats:
        ds.row_count, ds.size_bytes = stats.get("rows"), stats.get("bytes")

    existing = {c.name: c for c in s.scalars(select(DatasetColumn).where(DatasetColumn.dataset_id == ds.id))}
    had_schema = any(c.removed_at is None for c in existing.values())
    changes: list[dict[str, Any]] = []
    seen = set()
    for i, field in enumerate(schema):
        seen.add(field.name)
        dtype = str(field.type)
        col = existing.get(field.name)
        if col is None:
            s.add(
                DatasetColumn(
                    dataset_id=ds.id,
                    name=field.name,
                    ordinal=i,
                    data_type=dtype,
                    nullable=field.nullable,
                    is_audit=field.name in AUDIT_COLUMNS,
                )
            )
            if had_schema:
                changes.append({"change": "added", "column": field.name, "type": dtype})
        else:
            if col.removed_at is not None:
                changes.append({"change": "re-added", "column": field.name, "type": dtype})
                col.removed_at = None
            if col.data_type != dtype:
                changes.append({"change": "type_changed", "column": field.name, "from": col.data_type, "to": dtype})
                col.data_type = dtype
            col.ordinal, col.nullable = i, field.nullable
    for name_, col in existing.items():
        if name_ not in seen and col.removed_at is None:
            col.removed_at = now
            changes.append({"change": "removed", "column": name_, "type": col.data_type})
    if changes:
        s.add(SchemaChange(dataset_id=ds.id, run_id=run_id, changes=changes))
    s.flush()
    return ds, changes


def search(s: Session, q: str | None = None, layer: str | None = None, limit: int = 100) -> list[Dataset]:
    stmt = select(Dataset).order_by(Dataset.layer, Dataset.name).limit(limit)
    if layer:
        stmt = stmt.where(Dataset.layer == layer)
    if q:
        like = f"%{q.lower()}%"
        col_match = select(DatasetColumn.dataset_id).where(DatasetColumn.name.ilike(like))
        stmt = stmt.where(or_(Dataset.name.ilike(like), Dataset.description.ilike(like), Dataset.id.in_(col_match)))
    return list(s.scalars(stmt))
