"""SQL generation for insert-only Data Vault loads and business-vault builds (DuckDB).

Relations: ``src`` is the source dataset (already limited to rows newer than the
mapping's high-water mark), ``tgt`` the current vault table. Every statement returns
exactly the rows to append, so loads are insert-only and re-running one is harmless:

  hubs, links  keys not already in the target (first sighting wins)
  satellites   rows whose hashdiff differs from the previous row for the same parent
               key (and multi-active key): the one before it in the batch, else the
               latest earlier row in the target (an ASOF join)

Hash keys are SHA-1 over the business keys, each cast to text, trimmed and upper-cased,
NULL as '', joined with '||'. Hashdiffs hash the attributes the same way but keep case,
and drop trailing empty values so adding a nullable attribute doesn't version every row.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from dataplat.vault.model import BATCH, HASHDIFF, LOAD_DATE, RECORD_SOURCE, Resolved, hash_key_column, key_parts

DELIM = "||"


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# ------------------------------------------------------------------ hashing


def _norm(expr: str, upper: bool) -> str:
    e = f"trim(cast({expr} as varchar))"
    return f"coalesce({'upper(' + e + ')' if upper else e}, '')"


def hash_expr(columns: Iterable[str]) -> str:
    parts = ", ".join(_norm(q(c), True) for c in columns)
    return f"sha1(concat_ws({lit(DELIM)}, {parts}))"


def hashdiff_expr(columns: Iterable[str]) -> str:
    parts = ", ".join(_norm(q(c), False) for c in columns)
    return f"sha1(rtrim(concat_ws({lit(DELIM)}, {parts}), '|'))"


def hash_key(*values: object) -> str:
    """Python reference for :func:`hash_expr` (tests, and trace lookups by business key)."""
    parts = ["" if v is None else str(v).strip().upper() for v in values]
    return hashlib.sha1(DELIM.join(parts).encode()).hexdigest()


def hashdiff(*values: object) -> str:
    parts = ["" if v is None else str(v).strip() for v in values]
    return hashlib.sha1(DELIM.join(parts).rstrip("|").encode()).hexdigest()


# ------------------------------------------------------------------ staging pieces


class Source:
    """What the loader knows about the source relation."""

    def __init__(self, columns: Iterable[str], name: str, record_source: str | None = None) -> None:
        self.columns = set(columns)
        self.name = name
        self.record_source = record_source

    def col(self, name: str) -> str:
        if name not in self.columns:
            raise KeyError(f"column {name!r} is not in {self.name}")
        return q(name)

    @property
    def load_date(self) -> str:
        # Change-data rows carry the source commit time; batch rows only the load time.
        if "_source_ts" in self.columns:
            return 'coalesce("_source_ts", "_load_ts")'
        return '"_load_ts"'

    @property
    def record_source_expr(self) -> str:
        if self.record_source:
            return lit(self.record_source)
        if "_source" in self.columns:
            return f'coalesce("_source", {lit(self.name)})'
        return lit(self.name)

    @property
    def batch(self) -> str:
        return q(BATCH) if BATCH in self.columns else "cast(null as varchar)"

    @property
    def has_ops(self) -> bool:
        return "_op" in self.columns


def _mapped(src: Source, keys: dict[str, str], wanted: list[str]) -> list[str]:
    missing = [k for k in wanted if k not in keys]
    if missing:
        raise ValueError(f"mapping is missing keys {missing}")
    for k in wanted:
        src.col(keys[k])  # raises if the column isn't in the source
    return [keys[k] for k in wanted]


def _hub_key_columns(obj: Resolved, keys: dict[str, str], src: Source, role: str = "") -> list[str]:
    bks = obj.definition["business_keys"] if obj.kind == "hub" else []
    return _mapped(src, keys, [f"{role}.{bk}" if role else bk for bk in bks])


def parent_key(obj: Resolved, keys: dict[str, str], src: Source) -> tuple[str, list[str]]:
    """(hash expression, source columns) for a hub or link key."""
    cols: list[str] = []
    for role, bks in key_parts(obj):
        cols += _mapped(src, keys, [f"{role}.{bk}" if role else bk for bk in bks])
    return hash_expr(cols), cols


def _not_null(cols: list[str]) -> str:
    return " and ".join(f"{q(c)} is not null" for c in cols) or "true"


# ------------------------------------------------------------------ loads


def hub_sql(hub: Resolved, keys: dict[str, str], src: Source, has_target: bool) -> tuple[str, dict[str, list[str]]]:
    """SQL and column lineage (output column -> source columns)."""
    hk = hub.hk
    bks = hub.definition["business_keys"]
    cols = _hub_key_columns(hub, keys, src)
    select_bks = ", ".join(f"trim(cast({q(c)} as varchar)) as {q(bk)}" for bk, c in zip(bks, cols, strict=True))
    sql = f"""
with stg as (
  select {hash_expr(cols)} as {q(hk)}, {select_bks}, {src.load_date} as {LOAD_DATE},
         {src.record_source_expr} as {RECORD_SOURCE}, {src.batch} as {BATCH}
  from src where {_not_null(cols)}
), firsts as (
  select * from stg qualify row_number() over (partition by {q(hk)} order by {LOAD_DATE}, {BATCH}) = 1
)
select * from firsts f
{f"where not exists (select 1 from tgt t where t.{q(hk)} = f.{q(hk)})" if has_target else ""}
order by {LOAD_DATE}
"""
    lineage = {hk: cols, **{bk: [c] for bk, c in zip(bks, cols, strict=True)}}
    return sql, lineage


def link_sql(link: Resolved, keys: dict[str, str], src: Source, has_target: bool) -> tuple[str, dict[str, list[str]]]:
    hk = link.hk
    link_hash, all_cols = parent_key(link, keys, src)
    hub_hks, lineage = [], {hk: all_cols}
    for role, bks in key_parts(link):
        cols = _mapped(src, keys, [f"{role}.{bk}" for bk in bks])
        hub_hks.append(f"{hash_expr(cols)} as {q('hk_' + role)}")
        lineage["hk_" + role] = cols
    sql = f"""
with stg as (
  select {link_hash} as {q(hk)}, {", ".join(hub_hks)}, {src.load_date} as {LOAD_DATE},
         {src.record_source_expr} as {RECORD_SOURCE}, {src.batch} as {BATCH}
  from src where {_not_null(all_cols)}
), firsts as (
  select * from stg qualify row_number() over (partition by {q(hk)} order by {LOAD_DATE}, {BATCH}) = 1
)
select * from firsts f
{f"where not exists (select 1 from tgt t where t.{q(hk)} = f.{q(hk)})" if has_target else ""}
order by {LOAD_DATE}
"""
    return sql, lineage


def sat_sql(
    sat: Resolved, keys: dict[str, str], attributes: dict[str, str], src: Source, has_target: bool
) -> tuple[str, dict[str, list[str]]]:
    parent: Resolved = sat.parts["parent"]
    hk = parent.hk
    d = sat.definition
    attrs: list[str] = d["attributes"]
    mak: list[str] = d.get("multi_active_key", [])
    key_hash, key_cols = parent_key(parent, keys, src)

    if d.get("status"):
        is_del = "(\"_op\" = 'd')" if src.has_ops else "false"
        attr_select = f"{is_del} as is_deleted"
        diff = f"sha1(cast({is_del} as varchar))"
        where_ops = ""
        lineage_attrs = {"is_deleted": ["_op"] if src.has_ops else []}
    else:
        cols = _mapped(src, attributes, attrs)
        attr_select = ", ".join(f"{q(c)} as {q(a)}" for a, c in zip(attrs, cols, strict=True))
        diff = hashdiff_expr(cols)
        # A delete carries no attribute values; the status satellite records it instead.
        where_ops = " and coalesce(\"_op\", '') <> 'd'" if src.has_ops else ""
        lineage_attrs = {a: [c] for a, c in zip(attrs, cols, strict=True)}

    part = ", ".join([q(hk), *(q(m) for m in mak)])
    mak_eq = "".join(f" and t.{q(m)} is not distinct from s.{q(m)}" for m in mak)
    sql = f"""
with stg as (
  select {key_hash} as {q(hk)}, {src.load_date} as {LOAD_DATE}, {diff} as {HASHDIFF}, {attr_select},
         {src.record_source_expr} as {RECORD_SOURCE}, {src.batch} as {BATCH}
  from src where {_not_null(key_cols)}{where_ops}
), dedup as (
  select * from stg qualify row_number() over (partition by {part}, {LOAD_DATE} order by {BATCH} desc) = 1
), fresh as (
  select * from dedup s
  {
        f"where not exists (select 1 from tgt t where t.{q(hk)} = s.{q(hk)} and t.{LOAD_DATE} = s.{LOAD_DATE}{mak_eq})"
        if has_target
        else ""
    }
), seq as (
  select *, lag({HASHDIFF}) over (partition by {part} order by {LOAD_DATE}) as __prev from fresh
), cmp as (
  {
        f'''select s.*, t.{HASHDIFF} as __prior from seq s
  asof left join tgt t on s.{q(hk)} = t.{q(hk)}{mak_eq} and s.{LOAD_DATE} > t.{LOAD_DATE}'''
        if has_target
        else "select s.*, cast(null as varchar) as __prior from seq s"
    }
)
select * exclude (__prev, __prior) from cmp
where coalesce(__prev, __prior) is distinct from {HASHDIFF}
order by {LOAD_DATE}
"""
    lineage = {hk: key_cols, HASHDIFF: [c for cs in lineage_attrs.values() for c in cs], **lineage_attrs}
    return sql, lineage


# ------------------------------------------------------------------ business vault


def pit_sql(hub: Resolved, sats: list[Resolved]) -> str:
    """One row per hub key and change date, pointing at each satellite's row as of then.

    Relations: ``hub`` and ``s0``, ``s1``... for the satellites (all present).
    """
    hk = hub.hk
    dates = " union ".join(f"select {q(hk)}, {LOAD_DATE} from s{i}" for i in range(len(sats)))
    cols = ", ".join(f"s{i}.{LOAD_DATE} as {q(s.name + '_ldts')}" for i, s in enumerate(sats))
    joins = "\n".join(
        f"asof left join (select distinct {q(hk)}, {LOAD_DATE} from s{i}) s{i} "
        f"on d.{q(hk)} = s{i}.{q(hk)} and d.snapshot_date >= s{i}.{LOAD_DATE}"
        for i in range(len(sats))
    )
    return f"""
with d as (
  select distinct {q(hk)}, {LOAD_DATE} as snapshot_date from ({dates}) x
  where {q(hk)} in (select {q(hk)} from hub)
)
select d.{q(hk)}, d.snapshot_date, {cols}
from d
{joins}
order by d.{q(hk)}, d.snapshot_date
"""


def bridge_sql(hub: Resolved, links: list[Resolved]) -> str:
    """Hub keys joined through a path of links. Relations: ``hub``, ``l0``, ``l1``...

    Each link joins on a hub already on the path; its other hub keys are added.
    """
    hk = hub.hk
    present = {hub.name: f"hub.{q(hk)}"}
    select = [f"hub.{q(hk)}"]
    joins = []
    for i, link in enumerate(links):
        roles: dict[str, tuple[str, list[str]]] = link.parts["roles"]
        on = next(((r, h) for r, (h, _) in roles.items() if h in present), None)
        if on is None:
            raise ValueError(f"{link.name} does not connect to the hubs before it in the bridge")
        role, hub_name = on
        joins.append(f"join l{i} on l{i}.{q('hk_' + role)} = {present[hub_name]}")
        select.append(f"l{i}.{q(link.hk)}")
        for r, (h, _) in roles.items():
            if r != role and h not in present:
                present[h] = f"l{i}.{q('hk_' + r)}"
                select.append(f"l{i}.{q('hk_' + r)} as {q(hash_key_column(h))}")
    return f"select distinct {', '.join(select)}\nfrom hub\n" + "\n".join(joins)
