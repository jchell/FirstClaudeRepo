"""Ingestion runner against a real Postgres (metadata + a source database) and a local Delta lake."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text

from dataplat.core.context import PlatformContext
from dataplat.core.ports.orchestration import Job
from dataplat.db.models import (
    Connection,
    Dataset,
    DatasetColumn,
    DatasetProfile,
    IngestedFile,
    IngestionJob,
    IngestionRun,
    IngestionState,
    SchemaChange,
)
from dataplat.ingestion.runner import IngestionRunner
from dataplat.lineage.graph import build_graph, dataset_node, subgraph
from dataplat.orchestration.handlers import HANDLERS


def _job(ctx: PlatformContext, conn_type: str, config: dict, spec: dict, sa: str | None = None) -> uuid.UUID:
    with ctx.metadata.session() as s:
        conn = Connection(name=f"c-{uuid.uuid4().hex[:6]}", type=conn_type, config=config, service_account=sa)
        s.add(conn)
        s.flush()
        job = IngestionJob(name=f"j{uuid.uuid4().hex[:6]}", connection_id=conn.id, spec=spec, version=1)
        s.add(job)
        s.flush()
        return job.id


@pytest.fixture
def landing(tmp_path: Path) -> Path:
    d = tmp_path / "landing"
    d.mkdir()
    (d / "orders_1.csv").write_text("order_id,customer,amount\n1,ann,10.5\n2,bob,3\n")
    return d


def _files_spec(dataset: str, mode: str = "full") -> dict:
    return {
        "source": {"path_template": "/orders_*.csv"},
        "load_mode": mode,
        "target": {"layer": "bronze", "dataset": dataset},
    }


def test_full_file_load_writes_bronze_catalog_profile_and_lineage(ctx: PlatformContext, landing: Path) -> None:
    job_id = _job(ctx, "local_files", {"base_path": str(landing)}, _files_spec("orders"))
    result = IngestionRunner(ctx).run(job_id, None)
    assert result["status"] == "succeeded" and result["rows_written"] == 2 and result["files"] == 1

    uri = ctx.config.lake.uri("bronze", "orders")
    table = ctx.tables.read(uri)
    assert set(table.column_names) == {"order_id", "customer", "amount", "_load_ts", "_source", "_batch_id", "_file"}
    assert set(table.column("_batch_id").to_pylist()) == {result["batch_id"]}
    assert table.column("_file").to_pylist() == ["/orders_1.csv"] * 2

    with ctx.metadata.session() as s:
        ds = s.scalars(select(Dataset).where(Dataset.name == "orders")).one()
        assert ds.layer == "bronze" and ds.row_count == 2 and ds.table_version == 0
        cols = {c.name: c for c in s.scalars(select(DatasetColumn).where(DatasetColumn.dataset_id == ds.id))}
        assert cols["_batch_id"].is_audit and not cols["amount"].is_audit
        prof = s.scalars(select(DatasetProfile).where(DatasetProfile.dataset_id == ds.id)).one()
        amount = next(c for c in prof.columns if c["name"] == "amount")
        assert prof.row_count == 2 and amount["min"] == 3 and amount["max"] == 10.5
        assert not any(c["name"].startswith("_") for c in prof.columns)
        run = s.scalars(select(IngestionRun)).one()
        assert run.details["files"][0]["sha256"] and run.duration_ms is not None

    events = ctx.lineage.events()
    complete = next(e for e in events if e["eventType"] == "COMPLETE")
    assert complete["inputs"][0]["name"] == "/orders_1.csv"
    assert complete["inputs"][0]["facets"]["dataplat_file"]["rows"] == 2
    out = complete["outputs"][0]
    assert (
        out["name"] == "orders"
        and out["facets"]["columnLineage"]["fields"]["amount"]["inputFields"][0]["field"] == "amount"
    )


def test_full_reload_overwrites(ctx: PlatformContext, landing: Path) -> None:
    job_id = _job(ctx, "local_files", {"base_path": str(landing)}, _files_spec("orders_full"))
    IngestionRunner(ctx).run(job_id, None)
    (landing / "orders_1.csv").write_text("order_id,customer,amount\n7,cy,1\n")
    IngestionRunner(ctx).run(job_id, None)
    assert ctx.tables.read(ctx.config.lake.uri("bronze", "orders_full")).column("order_id").to_pylist() == [7]


def test_incremental_files_append_only_new_files_and_record_drift(ctx: PlatformContext, landing: Path) -> None:
    job_id = _job(ctx, "local_files", {"base_path": str(landing)}, _files_spec("orders_inc", "incremental"))
    runner = IngestionRunner(ctx)
    assert runner.run(job_id, None)["rows_written"] == 2
    nothing = runner.run(job_id, None)
    assert nothing["status"] == "succeeded" and nothing["rows_written"] == 0 and nothing["files"] == 0

    (landing / "orders_2.csv").write_text("order_id,customer,amount,channel\n3,cy,9,web\n")
    assert runner.run(job_id, None)["rows_written"] == 1
    table = ctx.tables.read(ctx.config.lake.uri("bronze", "orders_inc"))
    assert sorted(table.column("order_id").to_pylist()) == [1, 2, 3]
    assert table.column("channel").to_pylist().count(None) == 2  # older rows get nulls

    with ctx.metadata.session() as s:
        assert {f.path for f in s.scalars(select(IngestedFile).where(IngestedFile.job_id == job_id))} == {
            "/orders_1.csv",
            "/orders_2.csv",
        }
        change = s.scalars(select(SchemaChange)).one()
        assert change.changes == [{"change": "added", "column": "channel", "type": "string"}]


@pytest.fixture
def source_db(pg_url: str) -> str:
    """A 'source system' schema in the test database, read by its own least-privilege login."""
    from sqlalchemy.engine import make_url

    user, password = f"src_{uuid.uuid4().hex[:8]}", f"Src-{uuid.uuid4().hex}"
    eng = create_engine(pg_url)
    with eng.begin() as c:
        c.execute(text("drop schema if exists sales cascade; create schema sales"))
        c.execute(
            text(
                "create table sales.customers (id int primary key, name text, email text, updated_at timestamptz);"
                "insert into sales.customers values (1,'Ann','ann@x.io','2026-01-01T00:00:00Z'),"
                "(2,'Bob','bob@y.org','2026-01-02T00:00:00Z')"
            )
        )
        c.execute(text(f"create role {user} login password '{password}'"))
        c.execute(text(f"grant usage on schema sales to {user}; grant select on all tables in schema sales to {user}"))
    eng.dispose()
    return make_url(pg_url).set(username=user, password=password).render_as_string(hide_password=False)


def _pg_config(url: str) -> tuple[dict, str]:
    from sqlalchemy.engine import make_url

    u = make_url(url)
    return (
        {
            "host": u.host,
            "port": u.port,
            "database": u.database,
            "username": u.username,
            "password": "vault://kv/dataplat/service-accounts/etl/connections/src#password",
        },
        u.password,
    )


def test_postgres_incremental_watermark(ctx: PlatformContext, source_db: str, pg_url: str) -> None:
    config, password = _pg_config(source_db)
    ctx.secrets.write("service-accounts/etl/connections/src", {"password": password})
    spec = {
        "source": {"object": "sales.customers"},
        "load_mode": "incremental",
        "watermark_column": "updated_at",
        "target": {"layer": "bronze", "dataset": "crm_customers"},
    }
    job_id = _job(ctx, "postgres", config, spec, sa="etl")
    runner = IngestionRunner(ctx)
    assert runner.run(job_id, ctx.secrets)["rows_written"] == 2
    with ctx.metadata.session() as s:
        assert s.get(IngestionState, job_id).watermark["type"] == "datetime"

    eng = create_engine(pg_url)
    with eng.begin() as c:
        c.execute(text("insert into sales.customers values (3,'Cy','cy@z.com','2026-02-01T00:00:00Z')"))
    eng.dispose()
    second = runner.run(job_id, ctx.secrets)
    assert second["rows_written"] == 1
    ids = ctx.tables.read(ctx.config.lake.uri("bronze", "crm_customers")).column("id").to_pylist()
    assert sorted(ids) == [1, 2, 3]

    complete = [e for e in ctx.lineage.events() if e["eventType"] == "COMPLETE"][0]
    assert (
        complete["inputs"][0]["namespace"].startswith("postgres://")
        and complete["inputs"][0]["name"] == "sales.customers"
    )
    assert "sales" in complete["job"]["facets"]["sql"]["query"]
    assert password not in str(ctx.lineage.events())  # no credentials in lineage


def test_failed_run_is_recorded_with_fail_lineage(ctx: PlatformContext, source_db: str) -> None:
    config, password = _pg_config(source_db)
    ctx.secrets.write("service-accounts/etl/connections/src", {"password": password})
    spec = {"source": {"query": "select * from sales.nope"}, "target": {"layer": "bronze", "dataset": "broken"}}
    job_id = _job(ctx, "postgres", config, spec, sa="etl")
    with pytest.raises(Exception, match="nope"):
        IngestionRunner(ctx).run(job_id, ctx.secrets)
    with ctx.metadata.session() as s:
        run = s.scalars(select(IngestionRun).where(IngestionRun.job_id == job_id)).one()
        assert run.status == "failed" and "nope" in run.error and password not in run.error
    assert [e["eventType"] for e in reversed(ctx.lineage.events())][-1] == "FAIL"


def test_lineage_graph_links_source_job_dataset_and_app(ctx: PlatformContext, landing: Path) -> None:
    from dataplat.db.models import PortalApp

    job_id = _job(ctx, "local_files", {"base_path": str(landing)}, _files_spec("orders_g"))
    IngestionRunner(ctx).run(job_id, None)
    with ctx.metadata.session() as s:
        s.add(PortalApp(name="Sales report", url="https://bi.example/sales", datasets=["bronze.orders_g"]))
    with ctx.metadata.session() as s:
        g = build_graph(s, ctx.config.lake.model_dump())
    out = dataset_node(ctx.config.lake.bronze, "orders_g")
    up = subgraph(g, "app:Sales report", "upstream")
    ids = {n["id"] for n in up["nodes"]}
    assert out in ids and "dataset:file://|/orders_1.csv" in ids and any(i.startswith("job:ingest.") for i in ids)
    node = next(n for n in g["nodes"] if n["id"] == out)
    assert node["layer"] == "bronze" and node["row_count"] == 2


def test_handlers_test_discover_and_preview_local_files(ctx: PlatformContext, landing: Path) -> None:
    job_id = _job(ctx, "local_files", {"base_path": str(landing)}, _files_spec("orders_h"))
    with ctx.metadata.session() as s:
        cid = str(s.get(IngestionJob, job_id).connection_id)

    def call(kind: str, payload: dict) -> dict:
        return HANDLERS[kind](
            ctx, Job(id=1, kind=kind, payload={"connection_id": cid, **payload}, attempts=1, max_attempts=1), None
        )

    assert call("connection.test", {})["ok"] is True
    assert [o["name"] for o in call("connection.discover", {})["objects"]] == ["/orders_1.csv"]
    prev = call("connection.preview", {"request": {"path_template": "/orders_*.csv"}})
    assert [c["name"] for c in prev["columns"]] == ["order_id", "customer", "amount"] and len(prev["rows"]) == 2
    with ctx.metadata.session() as s:
        assert s.get(Connection, uuid.UUID(cid)).last_test_ok is True


def test_connection_test_reports_failure_without_raising(ctx: PlatformContext) -> None:
    job_id = _job(ctx, "local_files", {"base_path": "/does/not/exist"}, _files_spec("nope"))
    with ctx.metadata.session() as s:
        cid = str(s.get(IngestionJob, job_id).connection_id)
    res = HANDLERS["connection.test"](
        ctx, Job(id=1, kind="connection.test", payload={"connection_id": cid}, attempts=1, max_attempts=1), None
    )
    assert res["ok"] is False and res["message"]


def test_lineage_keeps_inputs_from_earlier_runs(ctx: PlatformContext, landing: Path) -> None:
    job_id = _job(ctx, "local_files", {"base_path": str(landing)}, _files_spec("orders_u", "incremental"))
    runner = IngestionRunner(ctx)
    runner.run(job_id, None)
    runner.run(job_id, None)  # nothing new: a COMPLETE event with no inputs
    with ctx.metadata.session() as s:
        g = build_graph(s, ctx.config.lake.model_dump())
    up = subgraph(g, dataset_node(ctx.config.lake.bronze, "orders_u"), "upstream")
    assert "dataset:file://|/orders_1.csv" in {n["id"] for n in up["nodes"]}
