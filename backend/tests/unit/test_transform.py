"""SQL models, templating, the sandbox, SCD2/fact/date builders, pipelines and serving."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest
from sqlalchemy import select, text

from dataplat.core.context import PlatformContext
from dataplat.db.models import JobRow, LineageEvent, Pipeline, TransformModel, TransformRun
from dataplat.transform.column_lineage import column_lineage
from dataplat.transform.runner import ModelError, ModelRunner, ModelSpec, PipelineRunner, model_order
from dataplat.transform.sandbox import sandbox
from dataplat.transform.templating import TemplateError, check_select, render
from dataplat.transform.triggers import notify_updated
from dataplat.vault.loader import VaultLoader
from tests.unit.test_vault import T0, _bronze, _map, _obj

# ------------------------------------------------------------------ templating and sandbox


def test_render_resolves_references_and_incremental_blocks() -> None:
    sql = (
        "select * from {{ ref('stg_orders') }} o join {{ source('bronze', 'customers') }} c using (id) "
        "join {{ vault('hub_customer') }} h on true "
        "{% if is_incremental() %}where o.ts > (select max(ts) from {{ this }}){% else %}where true{% endif %}"
    )
    full = render(sql, {"stg_orders": "silver"}, incremental=False, this=("gold", "x"))
    assert "silver__stg_orders" in full.sql and "bronze__customers" in full.sql and "vault__hub_customer" in full.sql
    assert full.sql.endswith("where true") and not full.uses_this
    inc = render(sql, {"stg_orders": "silver"}, incremental=True, this=("gold", "x"))
    assert "__this" in inc.sql and inc.uses_this
    assert full.refs == ["stg_orders"]


@pytest.mark.parametrize(
    ("sql", "error"),
    [
        ("select * from {{ ref('nope') }}", "no such model"),
        ("select * from {{ source('secret', 'x') }}", "layer must be"),
        ("select {{ config.password }}", "unsupported template"),
        ("select 1; select 2", "exactly one"),
        ("drop table x", "must be a SELECT"),
        ("copy (select 1) to '/tmp/x'", "must be a SELECT"),
        ("selec 1 frm", "syntax error|must be a SELECT"),
    ],
)
def test_bad_models_are_rejected(sql: str, error: str) -> None:
    with pytest.raises(TemplateError, match=error):
        check_select(render(sql, {}).sql)


@pytest.mark.parametrize(
    "sql",
    [
        "select * from read_csv('/etc/passwd')",
        "select * from read_text('/run/secrets/dataplat/role_id')",
        "select * from read_parquet('s3://bronze/x/*.parquet')",
        "select current_setting('enable_external_access')::varchar as x where false union all "
        "select * from read_json('http://example.com/x')",
    ],
)
def test_sandbox_blocks_files_network_and_settings(sql: str) -> None:
    with sandbox({"t": pa.table({"a": [1]})}) as sb:
        with pytest.raises(Exception, match="(?i)permission|disabled|external"):
            sb.query(sql)
        with pytest.raises(Exception, match="(?i)lock|configuration"):
            sb.query("SET enable_external_access=true")
        assert sb.query("select count(*) as n from t").column("n")[0].as_py() == 1


def test_column_lineage_through_ctes_and_expressions() -> None:
    sql = (
        "with c as (select h.customer_id, upper(s.city) as city, s.name from vault__sat s "
        "join vault__hub h using (hk_customer)) select customer_id, city || ' ' || name as label, 1 as one from c"
    )
    schemas = {
        "vault__sat": pa.schema([("hk_customer", pa.string()), ("city", pa.string()), ("name", pa.string())]),
        "vault__hub": pa.schema([("hk_customer", pa.string()), ("customer_id", pa.string())]),
    }
    rels = {"vault__sat": ("vault", "sat_c"), "vault__hub": ("vault", "hub_c")}
    lin = column_lineage(sql, schemas, rels, ["customer_id", "label", "one"])
    assert lin["customer_id"] == [("vault", "hub_c", "customer_id")]
    assert set(lin["label"]) == {("vault", "sat_c", "city"), ("vault", "sat_c", "name")}
    assert lin["one"] == []


# ------------------------------------------------------------------ models


def _model(ctx: PlatformContext, name: str, layer: str, kind: str = "sql", sql: str = "", **config) -> None:
    with ctx.metadata.session() as s:
        s.add(TransformModel(name=name, layer=layer, kind=kind, sql=sql, config=config))


def _read(ctx: PlatformContext, layer: str, name: str) -> list[dict]:
    return ctx.tables.read(ctx.config.lake.uri(layer, name)).to_pylist()


@pytest.fixture
def customers(ctx: PlatformContext) -> PlatformContext:
    """bronze.customers loaded into hub_customer + details/status satellites, with history."""
    hub = _obj(ctx, "hub", "hub_customer", {"business_keys": ["customer_id"]})
    sat = _obj(ctx, "sat", "sat_customer_details", {"parent": "hub_customer", "attributes": ["name", "city"]})
    status = _obj(ctx, "sat", "sat_customer_status", {"parent": "hub_customer", "status": True})
    _map(ctx, "customers", hub, {"customer_id": "id"})
    _map(ctx, "customers", sat, {"customer_id": "id"}, {"name": "name", "city": "city"})
    _map(ctx, "customers", status, {"customer_id": "id"})
    rows = [
        {"id": 1, "name": "Ann", "city": "Oslo", "_op": "r", "_source_ts": T0},
        {"id": 2, "name": "Bob", "city": "Rome", "_op": "r", "_source_ts": T0},
        {"id": 2, "name": "Bob", "city": "Paris", "_op": "u", "_source_ts": T0 + timedelta(days=10)},
        {"id": 3, "name": "Cy", "city": "Lima", "_op": "r", "_source_ts": T0},
        {"id": 3, "name": None, "city": None, "_op": "d", "_source_ts": T0 + timedelta(days=5)},
    ]
    _bronze(ctx, "customers", rows, T0 + timedelta(days=20))
    VaultLoader(ctx).load_source("bronze", "customers")
    return ctx


def test_scd2_dimension_from_vault_with_deletes(customers: PlatformContext) -> None:
    ctx = customers
    _model(
        ctx,
        "dim_customer",
        "gold",
        "scd2_dimension",
        source="vault.sat_customer_details",
        deletes_source="vault.sat_customer_status",
        surrogate_key="customer_sk",
        serve=True,
    )
    res = ModelRunner(ctx).run_model("dim_customer")
    dim = sorted(_read(ctx, "gold", "dim_customer"), key=lambda r: (r["customer_id"], r["valid_from"]))
    assert res["rows"] == 4
    by = {(r["customer_id"], r["city"]): r for r in dim}
    assert by[("1", "Oslo")]["is_current"] and by[("1", "Oslo")]["valid_to"] is None
    rome, paris = by[("2", "Rome")], by[("2", "Paris")]
    assert rome["valid_to"] == paris["valid_from"] == T0 + timedelta(days=10) and not rome["is_current"]
    assert paris["is_current"]
    lima = by[("3", "Lima")]  # deleted on day 5: closed, no current row
    assert lima["valid_to"] == T0 + timedelta(days=5) and not lima["is_current"]
    assert len({r["customer_sk"] for r in dim}) == 4

    # Rebuilding gives the same surrogate keys (they're derived from the key and valid_from).
    ModelRunner(ctx).run_model("dim_customer")
    assert {r["customer_sk"] for r in _read(ctx, "gold", "dim_customer")} == {r["customer_sk"] for r in dim}

    # Replicated to the serving database
    served = ctx.serving.read("gold", "dim_customer")
    assert len(served) == 4 and {r["city"] for r in served} == {"Oslo", "Rome", "Paris", "Lima"}

    # Column lineage runs through the vault back to bronze
    with ctx.metadata.session() as s:
        ev = (
            s.scalars(
                select(LineageEvent).where(
                    LineageEvent.job_name == "model.dim_customer", LineageEvent.event_type == "COMPLETE"
                )
            )
            .first()
            .event
        )
    fields = ev["outputs"][0]["facets"]["columnLineage"]["fields"]
    assert {"name": "hub_customer", "field": "customer_id"} in [
        {k: f[k] for k in ("name", "field")} for f in fields["customer_id"]["inputFields"]
    ]
    assert [f["name"] for f in fields["city"]["inputFields"]] == ["sat_customer_details"]


def test_incremental_sql_model_merges_on_unique_key(ctx: PlatformContext) -> None:
    _bronze(ctx, "orders", [{"order_id": 1, "amount": 10.0}, {"order_id": 2, "amount": 5.0}], T0)
    _model(
        ctx,
        "orders",
        "silver",
        "sql",
        "select order_id, amount, _load_ts from {{ source('bronze', 'orders') }} "
        "{% if is_incremental() %}where _load_ts > (select max(_load_ts) from {{ this }}){% endif %}",
        materialized="incremental",
        unique_key=["order_id"],
        serve=True,
    )
    runner = ModelRunner(ctx)
    assert runner.run_model("orders")["mode"] == "overwrite"  # first build: full
    _bronze(ctx, "orders", [{"order_id": 2, "amount": 7.5}, {"order_id": 3, "amount": 1.0}], T0 + timedelta(hours=1))
    res = runner.run_model("orders")
    assert res["mode"] == "merge" and res["rows"] == 2
    rows = {r["order_id"]: r["amount"] for r in _read(ctx, "silver", "orders")}
    assert rows == {1: 10.0, 2: 7.5, 3: 1.0}
    assert {r["order_id"]: r["amount"] for r in ctx.serving.read("silver", "orders")} == rows
    with ctx.metadata.session() as s:
        last = s.scalars(select(TransformRun).order_by(TransformRun.started_at.desc())).first()
    assert last.details["serving"]["mode"] == "upsert"


def test_fact_joins_dimensions_as_of_event_time(customers: PlatformContext) -> None:
    ctx = customers
    _model(ctx, "dim_customer", "gold", "scd2_dimension", source="vault.sat_customer_details")
    _bronze(
        ctx,
        "sales",
        [
            {"sale_id": 1, "cust": 2, "sold_at": T0 + timedelta(days=1), "amount": 3},  # Bob in Rome
            {"sale_id": 2, "cust": 2, "sold_at": T0 + timedelta(days=15), "amount": 4},  # Bob in Paris
            {"sale_id": 3, "cust": 99, "sold_at": T0, "amount": 1},  # unknown customer
        ],
        T0,
    )
    _model(
        ctx,
        "fct_sales",
        "gold",
        "fact",
        "select sale_id, cust, sold_at, amount from {{ source('bronze', 'sales') }}",
        dimensions=[
            {"dimension": "dim_customer", "on": {"cust": "customer_id"}, "as_of": "sold_at", "key_name": "customer_sk"}
        ],
    )
    models = {m: ModelSpec.of(x) for m, x in _models(ctx).items()}
    assert model_order(["fct_sales", "dim_customer"], models) == ["dim_customer", "fct_sales"]
    ModelRunner(ctx).run_model("dim_customer")
    ModelRunner(ctx).run_model("fct_sales")
    dim = {r["sk"]: r["city"] for r in _read(ctx, "gold", "dim_customer")}
    fact = {r["sale_id"]: r["customer_sk"] for r in _read(ctx, "gold", "fct_sales")}
    assert dim[fact[1]] == "Rome" and dim[fact[2]] == "Paris" and fact[3] == "-1"


def _models(ctx: PlatformContext) -> dict[str, TransformModel]:
    with ctx.metadata.session() as s:
        return {m.name: m for m in s.scalars(select(TransformModel))}


def test_date_dimension(ctx: PlatformContext) -> None:
    _model(ctx, "dim_date", "gold", "date_dimension", start="2024-02-27", end="2024-03-02")
    ModelRunner(ctx).run_model("dim_date")
    rows = _read(ctx, "gold", "dim_date")
    assert [r["date_key"] for r in rows] == [20240227, 20240228, 20240229, 20240301, 20240302]
    assert rows[3]["month_name"] == "March" and rows[4]["is_weekend"]


def test_pipeline_orders_models_and_skips_downstream_of_failures(ctx: PlatformContext) -> None:
    _bronze(ctx, "orders", [{"order_id": 1, "amount": 10.0}], T0)
    _model(ctx, "stg_orders", "silver", "sql", "select order_id, amount from {{ source('bronze', 'orders') }}")
    _model(ctx, "order_totals", "gold", "sql", "select sum(amount) as total from {{ ref('stg_orders') }}")
    _model(ctx, "broken", "silver", "sql", "select no_such_column from {{ source('bronze', 'orders') }}")
    _model(ctx, "after_broken", "gold", "sql", "select * from {{ ref('broken') }}")
    _model(ctx, "no_input", "silver", "sql", "select * from {{ source('bronze', 'missing') }}")
    with ctx.metadata.session() as s:
        # a long name: model runs record "pipeline:<name>" as their trigger
        p = Pipeline(name="p" * 120, models=["order_totals", "after_broken", "stg_orders", "broken", "no_input"])
        s.add(p)
        s.flush()
        pid = p.id
    with pytest.raises(ModelError, match="broken"):
        PipelineRunner(ctx).run(pid)
    with ctx.metadata.session() as s:
        runs = {r.target: r.status for r in s.scalars(select(TransformRun).where(TransformRun.kind == "model"))}
        prun = s.scalars(select(TransformRun).where(TransformRun.kind == "pipeline")).one()
    assert runs == {
        "silver.stg_orders": "succeeded",
        "gold.order_totals": "succeeded",
        "silver.broken": "failed",
        "silver.no_input": "skipped",
    }
    assert prun.status == "failed" and prun.details["models"]["after_broken"]["status"] == "skipped"
    assert _read(ctx, "gold", "order_totals") == [{"total": 10.0}]


def test_cycles_are_rejected(ctx: PlatformContext) -> None:
    _model(ctx, "a", "silver", "sql", "select * from {{ ref('b') }}")
    _model(ctx, "b", "silver", "sql", "select * from {{ ref('a') }}")
    models = {n: ModelSpec.of(m) for n, m in _models(ctx).items()}
    with pytest.raises(ModelError, match="cycle"):
        model_order(["a", "b"], models)


def test_new_data_triggers_vault_loads_and_pipelines(customers: PlatformContext) -> None:
    ctx = customers
    with ctx.metadata.session() as s:
        s.add(Pipeline(name="dims", models=[], trigger_datasets=["vault.sat_customer_details"]))
    q1 = notify_updated(ctx, ["bronze.customers", "bronze.unmapped"])
    q2 = notify_updated(ctx, ["bronze.customers"])  # coalesced with the waiting one
    assert q1["vault_loads"] == ["bronze.customers"] and q2["vault_loads"] == []
    assert notify_updated(ctx, ["vault.sat_customer_details"])["pipelines"] == ["dims"]
    with ctx.metadata.session() as s:
        kinds = sorted(j.kind for j in s.scalars(select(JobRow)))
    assert kinds == ["pipeline.run", "vault.load"]
    # Once a load starts, a new trigger may queue a follow-up (changes mid-run aren't lost).
    job = ctx.jobs.claim("w", ["vault.load"])
    assert job is not None
    assert notify_updated(ctx, ["bronze.customers"])["vault_loads"] == ["bronze.customers"]


def test_preview_returns_rows_lineage_and_dependencies(customers: PlatformContext) -> None:
    ctx = customers
    spec = ModelSpec(
        "customer_cities",
        "silver",
        "sql",
        "select h.customer_id, s.city from {{ vault('sat_customer_details') }} s "
        "join {{ vault('hub_customer') }} h using (hk_customer)",
        {},
    )
    out = ModelRunner(ctx).preview(spec)
    assert {c["name"] for c in out["columns"]} == {"customer_id", "city"}
    assert out["lineage"]["city"] == ["vault.sat_customer_details.city"]
    assert len(out["rows"]) == 4
    with pytest.raises(ModelError, match="no data"):
        ModelRunner(ctx).preview(ModelSpec("x", "silver", "sql", "select * from {{ source('bronze','nope') }}", {}))


def test_serving_replace_is_atomic_and_typed(ctx: PlatformContext) -> None:
    t = pa.table(
        {
            "id": pa.array([1, 2], pa.int64()),
            "ts": pa.array([datetime(2026, 1, 1, tzinfo=UTC), None], pa.timestamp("us", tz="UTC")),
            "tags": pa.array([["a", "b"], []]),
            "note": ['x,"y"', None],
        }
    )
    ctx.serving.replace("gold", "t", t)
    ctx.serving.replace("gold", "t", t.slice(0, 1))
    rows = ctx.serving.read("gold", "t")
    assert len(rows) == 1 and rows[0]["note"] == 'x,"y"' and rows[0]["ts"] == datetime(2026, 1, 1, tzinfo=UTC)
    with ctx.metadata.engine.connect() as c:
        types = dict(
            c.execute(
                text("select column_name, data_type from information_schema.columns where table_name = 't'")
            ).all()
        )
    assert types["id"] == "bigint" and types["ts"] == "timestamp with time zone"
