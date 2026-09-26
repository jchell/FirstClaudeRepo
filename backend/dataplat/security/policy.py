"""Data access policies: dataset/domain grants, tag-based column masking, row filters.

Applied wherever a person reads data (catalog previews, profiles, DQ samples, the
query API, model previews). Platform jobs (ingestion, vault loads, model builds) read
raw data; policies govern people, not pipelines.

  access   A dataset with grants on itself or its domain is restricted: only its
           grantees (roles or users), admins and stewards may read it. Datasets
           without grants are readable by every signed-in user.
  masking  A column carrying a tag with an enabled masking policy is masked for every
           role not exempted by that policy (redact, partial, keyed hash, or null).
  filters  Enabled row filters on a dataset restrict non-exempt users to the rows
           matching all their predicates.

Masked and filtered data is computed in a locked sandbox that sees only the raw
table; a user's own SQL then runs in a second sandbox that sees only the result.
"""

from __future__ import annotations

import logging
import secrets as pysecrets
import threading
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
import sqlglot
from sqlalchemy import select
from sqlalchemy.orm import Session

from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.core.secrets import SecretNotFound
from dataplat.db.models import AccessGrant, Dataset, MaskingPolicy, RowFilter, TagAssignment
from dataplat.transform.sandbox import sandbox
from dataplat.vault.sql import q

log = logging.getLogger(__name__)

MASK_METHODS = ("partial", "hash", "redact", "null")  # weakest -> strongest
PRIVILEGED = {"admin", "steward"}
MASKING_KEY_REF = "vault://kv/dataplat/platform/masking#hmac_key"
# Interactive reads that need masking or filtering are computed in memory.
MAX_SECURED_ROWS = 2_000_000


class AccessDenied(PermissionError):
    pass


def mask_expr(column: str, method: str) -> tuple[str, int]:
    """SQL for a masked column and how many key parameters it takes."""
    c = f"cast({q(column)} as varchar)"
    if method == "null":
        return "cast(null as varchar)", 0
    if method == "redact":
        return f"case when {q(column)} is null then null else '****' end", 0
    if method == "partial":
        return (
            f"case when {q(column)} is null then null "
            f"else concat(repeat('*', greatest(length({c}) - 4, 0)), right({c}, 4)) end"
        ), 0
    if method == "hash":
        # keyed, so a masked value can't be matched by hashing guesses
        return f"case when {q(column)} is null then null else left(sha256(concat(?, {c})), 16) end", 1
    raise ValueError(f"unknown masking method {method!r}")


def mask_value(value: Any, method: str) -> Any:
    """Python counterpart for values held in metadata (profiles, DQ samples)."""
    if value is None or method == "null":
        return None
    if method == "partial":
        s = str(value)
        return "*" * max(len(s) - 4, 0) + s[-4:]
    return "****"  # redact, and hash (no key outside the sandbox)


def validate_predicate(predicate: str) -> str:
    """A row-filter predicate: one boolean expression over the dataset's own columns."""
    try:
        stmt = sqlglot.parse_one(f"select 1 from src where ({predicate})", dialect="duckdb")
    except sqlglot.errors.ParseError as e:
        raise ValueError(f"not a valid condition: {str(e).splitlines()[0]}") from e
    if (
        not isinstance(stmt, sqlglot.exp.Select)
        or any(t.name != "src" for t in stmt.find_all(sqlglot.exp.Table))
        or stmt.find(sqlglot.exp.Subquery)
        or ";" in predicate
    ):
        raise ValueError("a row filter is a condition on the dataset's own columns (no subqueries)")
    return predicate.strip()


_key_lock = threading.Lock()
_fallback_key: str | None = None


def masking_key(ctx: PlatformContext) -> str:
    global _fallback_key
    try:
        return ctx.secrets.resolve(MASKING_KEY_REF)
    except (SecretNotFound, PermissionError):
        with _key_lock:
            if _fallback_key is None:
                log.warning("no masking key in Vault; hash masks use a per-process key")
                _fallback_key = pysecrets.token_hex(32)
            return _fallback_key


@dataclass
class DatasetPolicy:
    readable: bool
    masks: dict[str, str] = field(default_factory=dict)  # column -> method
    row_filters: list[str] = field(default_factory=list)
    restricted: bool = False

    @property
    def open(self) -> bool:
        return self.readable and not self.masks and not self.row_filters


class PolicyEngine:
    """Policy decisions for one principal (loaded once per request)."""

    def __init__(self, s: Session, principal: Principal) -> None:
        self.s = s
        self.p = principal
        self.roles = set(principal.roles)
        grants = list(s.scalars(select(AccessGrant)))
        self._grants: dict[tuple[str, str], list[AccessGrant]] = {}
        for g in grants:
            self._grants.setdefault((g.target_type, g.target), []).append(g)
        self._masking = [m for m in s.scalars(select(MaskingPolicy).where(MaskingPolicy.enabled))]
        self._cache: dict[Any, DatasetPolicy] = {}

    def _grantee(self, g: AccessGrant) -> bool:
        return (g.principal_type == "role" and g.principal in self.roles) or (
            g.principal_type == "user" and g.principal == self.p.name
        )

    def restricted(self, ds: Dataset) -> bool:
        return bool(
            self._grants.get(("dataset", str(ds.id))) or (ds.domain and self._grants.get(("domain", ds.domain)))
        )

    def can_read(self, ds: Dataset) -> bool:
        if self.roles & PRIVILEGED or not self.restricted(ds):
            return True
        grants = self._grants.get(("dataset", str(ds.id)), []) + (
            self._grants.get(("domain", ds.domain), []) if ds.domain else []
        )
        return any(self._grantee(g) for g in grants)

    def for_dataset(self, ds: Dataset) -> DatasetPolicy:
        if ds.id in self._cache:
            return self._cache[ds.id]
        pol = DatasetPolicy(readable=self.can_read(ds), restricted=self.restricted(ds))
        # A policy on a tag also covers its sub-tags: "pii" masks "pii.email" too.
        applicable = [m for m in self._masking if not (set(m.exempt_roles) & self.roles)]
        if applicable:
            rows = self.s.scalars(
                select(TagAssignment).where(
                    TagAssignment.dataset_id == ds.id,
                    TagAssignment.status == "active",
                    TagAssignment.column != "",
                )
            )
            for a in rows:
                for m in applicable:
                    if a.tag != m.tag and not a.tag.startswith(m.tag + "."):
                        continue
                    current = pol.masks.get(a.column)
                    if current is None or MASK_METHODS.index(m.method) > MASK_METHODS.index(current):
                        pol.masks[a.column] = m.method
        for f in self.s.scalars(select(RowFilter).where(RowFilter.dataset_id == ds.id, RowFilter.enabled)):
            if not (set(f.exempt_roles) & self.roles):
                pol.row_filters.append(f.predicate)
        self._cache[ds.id] = pol
        return pol

    def readable_ids(self) -> set[Any] | None:
        """Ids of readable datasets, or None when everything is readable."""
        if self.roles & PRIVILEGED or not self._grants:
            return None
        return {d.id for d in self.s.scalars(select(Dataset)) if self.can_read(d)}


# ------------------------------------------------------------------ secured reads


def dataset_of(s: Session, layer: str, name: str) -> Dataset:
    ds = s.scalars(select(Dataset).where(Dataset.layer == layer, Dataset.name == name)).first()
    if ds is None:
        raise LookupError(f"{layer}.{name} is not in the catalog")
    return ds


def _select(schema: pa.Schema, pol: DatasetPolicy, key: str) -> tuple[str, list[Any]]:
    cols, params = [], []
    for f in schema:
        if f.name in pol.masks:
            expr, n = mask_expr(f.name, pol.masks[f.name])
            cols.append(f"{expr} as {q(f.name)}")
            params += [key] * n
        else:
            cols.append(q(f.name))
    where = " and ".join(f"({p})" for p in pol.row_filters) or "true"
    return f"select {', '.join(cols)} from src where {where}", params


def read_secured(ctx: PlatformContext, engine: PolicyEngine, ds: Dataset, limit: int | None = None) -> pa.Table:
    """Rows of a dataset as this principal may see them."""
    pol = engine.for_dataset(ds)
    if not pol.readable:
        raise AccessDenied(f"no access to {ds.layer}.{ds.name}")
    raw = ctx.tables.dataset(ds.uri)
    if pol.open:
        return raw.head(limit) if limit is not None else raw.to_table()
    sql, params = _select(raw.schema, pol, masking_key(ctx) if "hash" in pol.masks.values() else "")
    cap = limit if limit is not None else MAX_SECURED_ROWS + 1
    with sandbox({"src": raw}) as sb:
        table = sb.query(f"{sql} limit {int(cap)}", params)
    if limit is None and table.num_rows > MAX_SECURED_ROWS:
        raise AccessDenied(f"{ds.layer}.{ds.name} has masking or row filters and is too large to query interactively")
    return table


def secured_relations(
    ctx: PlatformContext, engine: PolicyEngine, relations: dict[str, tuple[str, str]]
) -> dict[str, Any]:
    """Arrow relations for a user's query: raw where no policy applies, else secured copies."""
    out: dict[str, Any] = {}
    for rel, (layer, name) in relations.items():
        ds = dataset_of(engine.s, layer, name)
        pol = engine.for_dataset(ds)
        if not pol.readable:
            raise AccessDenied(f"no access to {layer}.{name}")
        out[rel] = ctx.tables.dataset(ds.uri) if pol.open else read_secured(ctx, engine, ds)
    return out


def mask_profile(columns: list[dict[str, Any]], pol: DatasetPolicy) -> list[dict[str, Any]]:
    """Profiles hold real values (min/max, top values): mask them like the data."""
    out = []
    for c in columns:
        method = pol.masks.get(c.get("name"))
        if method is None:
            out.append(c)
            continue
        c = {**c, "masked": method}
        for k in ("min", "max", "mean", "stddev"):
            c.pop(k, None)
        if "top_values" in c:
            c["top_values"] = [{**t, "value": mask_value(t["value"], method)} for t in c["top_values"]]
        out.append(c)
    return out


def only_readable(stmt: Any, engine: PolicyEngine) -> Any:
    """Restricts a Dataset query to the datasets the principal may read."""
    ids = engine.readable_ids()
    return stmt if ids is None else stmt.where(Dataset.id.in_(ids))
