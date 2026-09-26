"""Model kinds, their configuration, and the SQL the declarative builders generate.

  sql              user SQL (templated); materialized as a table or incrementally
  scd2_dimension   type-2 slowly changing dimension from a history source (a vault
                   satellite, or any table with a change timestamp): one row per version
                   with valid_from/valid_to/is_current and a stable surrogate key
  fact             user SQL whose rows are joined to dimensions as of the fact's time,
                   adding each dimension's surrogate key ('-1' when there is no match)
  date_dimension   a generated calendar

Declarative kinds compile to SQL too, so they run, lineage-parse and preview exactly
like SQL models.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from dataplat.transform.templating import relation_name
from dataplat.vault.model import LOAD_DATE
from dataplat.vault.sql import q

MODEL_NAME = r"^[a-z][a-z0-9_]{1,62}$"
Kind = Literal["sql", "scd2_dimension", "fact", "date_dimension"]


class Materialization(BaseModel):
    materialized: Literal["table", "incremental"] = "table"
    # incremental: rows are merged on these columns (else appended)
    unique_key: list[str] = []
    # replicate into the serving database after each build
    serve: bool = False


class SqlConfig(Materialization):
    # set on silver copies the ingestion wizard maintains ("promote to silver")
    promoted_from: str | None = None


class Scd2Config(BaseModel):
    # "<layer>.<dataset>", e.g. "vault.sat_customer_details" or "bronze.customers_changes"
    source: str = Field(pattern=r"^(bronze|silver|gold|vault)\.[a-z][a-z0-9_]*$")
    # For a vault satellite these default to its hub's business keys and its attributes.
    business_key: list[str] = []
    attributes: list[str] = []
    change_ts: str = LOAD_DATE
    # A status satellite (or table with a boolean flag) whose true rows close the version.
    deletes_source: str | None = Field(default=None, pattern=r"^(bronze|silver|gold|vault)\.[a-z][a-z0-9_]*$")
    deleted_flag: str = "is_deleted"
    surrogate_key: str = Field(default="sk", pattern=r"^[a-z][a-z0-9_]*$")
    serve: bool = False


class DimensionLookup(BaseModel):
    dimension: str  # an scd2_dimension model
    on: dict[str, str] = Field(min_length=1)  # fact column -> dimension business key
    as_of: str | None = None  # fact column with the event time (else: current version)
    key_name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")


class FactConfig(Materialization):
    dimensions: list[DimensionLookup] = []


class DateConfig(BaseModel):
    start: date = date(2000, 1, 1)
    end: date = date(2035, 12, 31)
    serve: bool = False

    @model_validator(mode="after")
    def _range(self) -> DateConfig:
        if self.end < self.start or (self.end - self.start).days > 50 * 366:
            raise ValueError("date range must be forward and at most 50 years")
        return self


CONFIGS: dict[str, type[BaseModel]] = {
    "sql": SqlConfig,
    "scd2_dimension": Scd2Config,
    "fact": FactConfig,
    "date_dimension": DateConfig,
}


def validate_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    if kind not in CONFIGS:
        raise ValueError(f"unknown model kind {kind!r}")
    return CONFIGS[kind].model_validate(config).model_dump(mode="json", exclude_none=True)


def serves(kind: str, config: dict[str, Any]) -> bool:
    return bool(config.get("serve"))


# ------------------------------------------------------------------ generated SQL


def _hash(cols: list[str], alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    parts = ", ".join(f"coalesce(trim(cast({p}{q(c)} as varchar)), '')" for c in cols)
    return f"md5(concat_ws('||', {parts}))"


def _with_hub_keys(rel: str, hub_table: str, bks: list[str], hk: str) -> str:
    """A satellite's rows with its hub's business keys joined on."""
    keys = ", ".join(f"h.{q(b)}" for b in bks)
    return f"select {keys}, s.* from {rel} s join {relation_name('vault', hub_table)} h using ({q(hk)})"


def scd2_sql(
    cfg: Scd2Config, source_schema: list[str], hub: tuple[str, list[str], str] | None
) -> tuple[str, list[str], list[str]]:
    """Returns (templated SQL, business key, attributes).

    ``hub`` is (hub table, business keys, hash key column) when the source is a
    satellite: business keys then come from the hub.
    """
    layer, name = cfg.source.split(".", 1)
    src_rel = relation_name(layer, name)
    audit = {
        LOAD_DATE,
        "hashdiff",
        "record_source",
        "_batch_id",
        "_load_ts",
        "_source",
        "_file",
        "_op",
        "_source_ts",
        "_offset",
    }
    if hub:
        hub_table, hub_bks, hk = hub
        bk = cfg.business_key or hub_bks
        base = _with_hub_keys(src_rel, hub_table, bk, hk)
        audit.add(hk)
    else:
        bk = cfg.business_key
        base = f"select * from {src_rel}"
    if not bk:
        raise ValueError("the dimension needs a business key")
    attrs = cfg.attributes or [c for c in source_schema if c not in audit and c not in bk and not c.startswith("hk_")]
    if not attrs:
        raise ValueError("the dimension needs at least one attribute")
    for c in [*([] if hub else bk), *attrs, cfg.change_ts]:
        if c not in source_schema:
            raise ValueError(f"{cfg.source} has no column {c!r}")
    bk_q = ", ".join(q(c) for c in bk)
    attr_q = ", ".join(q(c) for c in attrs)
    batch = "_batch_id" if "_batch_id" in source_schema else "cast(null as varchar)"
    deletes = ""
    if cfg.deletes_source:
        dl, dn = cfg.deletes_source.split(".", 1)
        drel = relation_name(dl, dn)
        if hub:
            dsrc = _with_hub_keys(drel, hub[0], bk, hub[2])
        else:
            dsrc = f"select * from {drel}"
        nulls = ", ".join(f"null as {q(a)}" for a in attrs)
        # Vault status satellites carry _batch_id; other delete sources may not.
        d_batch = "d_._batch_id" if hub else "cast(null as varchar)"
        deletes = f"""
  union all
  select {bk_q}, {nulls}, {q(cfg.change_ts)} as __ts, true as __deleted, {d_batch} as _batch_id
  from ({dsrc}) d_ where {q(cfg.deleted_flag)}"""
    sk = q(cfg.surrogate_key)
    sql = f"""with src as (
  select {bk_q}, {attr_q}, {q(cfg.change_ts)} as __ts, false as __deleted, {batch} as _batch_id
  from ({base}) b{deletes}
), hashed as (
  select *, case when __deleted then 'deleted' else {_hash(attrs)} end as row_hash from src
), dedup as (
  select * from hashed qualify row_number() over (partition by {bk_q}, __ts order by __deleted desc) = 1
), versions as (
  select *, lag(row_hash) over (partition by {bk_q} order by __ts) as __prev from dedup
), changes as (
  select *, lead(__ts) over (partition by {bk_q} order by __ts) as __next from versions
  where __prev is distinct from row_hash
)
select {_hash([*bk, "__ts"])} as {sk}, {bk_q}, {attr_q},
       __ts as valid_from, __next as valid_to, __next is null as is_current, row_hash, _batch_id
from changes
where not __deleted
order by {bk_q}, valid_from"""
    return sql, bk, attrs


def fact_sql(base_sql: str, lookups: list[tuple[DimensionLookup, str, str]]) -> str:
    """``lookups``: (lookup, dimension layer, dimension surrogate key column)."""
    select = ["f.*"]
    joins = []
    for i, (lk, layer, sk) in enumerate(lookups):
        d = f"d{i}"
        on = " and ".join(f"{d}.{q(dim_col)} = cast(f.{q(fact_col)} as varchar)" for fact_col, dim_col in lk.on.items())
        if lk.as_of:
            on += f" and f.{q(lk.as_of)} >= {d}.valid_from and (f.{q(lk.as_of)} < {d}.valid_to or {d}.valid_to is null)"
        else:
            on += f" and {d}.is_current"
        select.append(f"coalesce({d}.{q(sk)}, '-1') as {q(lk.key_name or lk.dimension + '_' + sk)}")
        joins.append(f"left join {relation_name(layer, lk.dimension)} {d} on {on}")
    return f"select {', '.join(select)}\nfrom ({base_sql}) f\n" + "\n".join(joins)


def date_sql(cfg: DateConfig) -> str:
    return f"""select cast(strftime(d, '%Y%m%d') as integer) as date_key, cast(d as date) as date,
       year(d) as year, quarter(d) as quarter, month(d) as month, monthname(d) as month_name,
       day(d) as day, isodow(d) as day_of_week, dayname(d) as day_name, weekofyear(d) as week_of_year,
       isodow(d) >= 6 as is_weekend
from range(date '{cfg.start.isoformat()}', date '{cfg.end.isoformat()}' + interval 1 day, interval 1 day) t(d)
order by d"""
