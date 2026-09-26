"""Phase 1 end to end: every connector type against the dev test sources, through the API.

Needs the dev profile running and seeded:
    docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
    docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm seed
Set DATAPLAT_TEST_MSSQL=1 when the mssql profile is running too.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.integration.conftest import compose, wait_for_job

pytestmark = pytest.mark.integration

STAMP = datetime.now(UTC).strftime("%Y%m%d")
DEV_PW = "devsource"


def wait_task(api, task_id: int, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = api.get(f"/api/tasks/{task_id}").json()
        if t["status"] in ("succeeded", "failed"):
            return t
        time.sleep(0.5)
    raise TimeoutError(f"task {task_id} did not finish")


@pytest.fixture(scope="module")
def sa(api) -> str:
    name = f"it-etl-{uuid.uuid4().hex[:6]}"
    assert api.post("/api/admin/service-accounts", json={"name": name}).status_code == 201
    yield name


def _make(api, sa: str, type_: str, config: dict, secrets: dict | None = None) -> str:
    r = api.post(
        "/api/connections",
        json={
            "name": f"it-{type_}-{uuid.uuid4().hex[:6]}",
            "type": type_,
            "service_account": sa if secrets else None,
            "config": config,
            "secrets": secrets or {},
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    test = wait_task(api, api.post(f"/api/connections/{cid}/test").json()["task_id"])
    assert test["status"] == "succeeded" and test["result"]["ok"], test
    return cid


def _ingest(api, cid: str, spec: dict) -> tuple[dict, dict]:
    dataset = spec["target"]["dataset"]
    r = api.post("/api/ingestion/jobs", json={"name": f"it_{dataset}", "connection_id": cid, "spec": spec})
    assert r.status_code == 201, r.text
    job = r.json()
    task = api.post(f"/api/ingestion/jobs/{job['id']}/run").json()["task_id"]
    done = wait_for_job(api, task, timeout=180)
    assert done["status"] == "succeeded", done
    run = api.get("/api/ingestion/runs", params={"job_id": job["id"], "limit": 1}).json()[0]
    assert run["status"] == "succeeded", run
    return job, run


def _uniq(name: str) -> str:
    return f"{name}_{uuid.uuid4().hex[:5]}"


def _dataset(api, name: str) -> dict:
    found = [d for d in api.get("/api/catalog/datasets", params={"q": name}).json() if d["name"] == name]
    assert found, f"{name} not in catalog"
    return api.get(f"/api/catalog/datasets/{found[0]['id']}").json()


# ---------------------------------------------------------------- plan verification steps 2-4


def test_postgres_table_job_incremental(api, sa) -> None:
    cid = _make(
        api, sa, "postgres", {"host": "src-postgres", "database": "sales", "username": "dev"}, {"password": DEV_PW}
    )
    objects = wait_task(api, api.post(f"/api/connections/{cid}/discover", json={}).json()["task_id"])["result"][
        "objects"
    ]
    assert {"crm.customers", "crm.orders"} <= {o["name"] for o in objects}
    preview = wait_task(
        api, api.post(f"/api/connections/{cid}/preview", json={"object": "crm.customers"}).json()["task_id"]
    )
    assert len(preview["result"]["rows"]) == 20

    source_rows = int(
        compose(
            "exec",
            "-T",
            "src-postgres",
            "psql",
            "-U",
            "dev",
            "-d",
            "sales",
            "-Atc",
            "select count(*) from crm.customers",
        )
    )
    ds = _uniq("crm_customers")
    job, run = _ingest(
        api,
        cid,
        {
            "source": {"object": "crm.customers"},
            "load_mode": "incremental",
            "watermark_column": "updated_at",
            "target": {"layer": "bronze", "dataset": ds},
        },
    )
    assert run["rows_written"] == source_rows
    detail = _dataset(api, ds)
    assert detail["row_count"] == source_rows and detail["profile"]["row_count"] == source_rows
    email = next(c for c in detail["profile"]["columns"] if c["name"] == "email")
    assert email["patterns"]["email"] == 100.0  # feeds PII suggestions in Phase 3

    # New and changed source rows arrive on the next run; nothing else is re-read.
    later = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    new_id = 100_000 + int(uuid.uuid4().int % 100_000)
    existing = compose(
        "exec",
        "-T",
        "src-postgres",
        "psql",
        "-U",
        "dev",
        "-d",
        "sales",
        "-Atc",
        "select min(id) from crm.customers where id > 10",
    ).strip()
    compose(
        "exec",
        "-T",
        "src-postgres",
        "psql",
        "-U",
        "dev",
        "-d",
        "sales",
        "-c",
        f"insert into crm.customers values ({new_id},'New','Row','new@example.com','+47 1','Oslo','NO','{later}');"
        f"update crm.customers set city='Bergen', updated_at='{later}' where id={existing}",
    )
    task = api.post(f"/api/ingestion/jobs/{job['id']}/run").json()["task_id"]
    assert wait_for_job(api, task)["status"] == "succeeded"
    second = api.get("/api/ingestion/runs", params={"job_id": job["id"], "limit": 1}).json()[0]
    assert second["rows_written"] == 2
    assert _dataset(api, ds)["row_count"] == source_rows + 2
    compose(
        "exec",
        "-T",
        "src-postgres",
        "psql",
        "-U",
        "dev",
        "-d",
        "sales",
        "-c",
        f"delete from crm.customers where id={new_id}",
    )


def test_sftp_file_template_job(api, sa) -> None:
    cid = _make(api, sa, "sftp", {"host": "src-sftp", "username": "dev", "base_path": "/upload"}, {"password": DEV_PW})
    ds = _uniq("sftp_orders")
    job, run = _ingest(
        api,
        cid,
        {
            "source": {"path_template": "/orders/orders_{yyyyMMdd}_*.csv"},
            "load_mode": "incremental",
            "target": {"layer": "bronze", "dataset": ds},
        },
    )
    # Today's two files only (yesterday's file doesn't match the template).
    assert run["files"] == 2 and run["rows_written"] == 200
    assert all(len(f["sha256"]) == 64 for f in run["details"]["files"])
    task = api.post(f"/api/ingestion/jobs/{job['id']}/run").json()["task_id"]
    wait_for_job(api, task)
    again = api.get("/api/ingestion/runs", params={"job_id": job["id"], "limit": 1}).json()[0]
    assert again["files"] == 0 and again["rows_written"] == 0  # new files only

    graph = api.get("/api/lineage/graph").json()
    node = next(n for n in graph["nodes"] if n["label"] == ds)
    up = api.get("/api/lineage/graph", params={"node": node["id"], "direction": "upstream"}).json()
    files = [n for n in up["nodes"] if n["type"] == "dataset" and n["layer"] == "source"]
    assert {f["label"] for f in files} == {f"/orders/orders_{STAMP}_1.csv", f"/orders/orders_{STAMP}_2.csv"}
    assert all(f["namespace"] == "sftp://src-sftp:22" for f in files)


# ---------------------------------------------------------------- the other connector types


def test_mysql(api, sa) -> None:
    cid = _make(api, sa, "mysql", {"host": "src-mysql", "database": "shop", "username": "dev"}, {"password": DEV_PW})
    _, run = _ingest(
        api,
        cid,
        {
            "source": {"query": "select sku, name, price, stock from products where stock > 0"},
            "target": {"layer": "bronze", "dataset": _uniq("shop_products")},
        },
    )
    in_stock = compose(
        "exec",
        "-T",
        "src-mysql",
        "mysql",
        "-udev",
        "-pdevsource",
        "shop",
        "-N",
        "-e",
        "select count(*) from products where stock > 0",
    )
    assert run["rows_written"] == int(in_stock.strip().splitlines()[-1])


@pytest.mark.skipif(not os.environ.get("DATAPLAT_TEST_MSSQL"), reason="mssql profile not running")
def test_sqlserver(api, sa) -> None:
    cid = _make(
        api,
        sa,
        "sqlserver",
        {"host": "src-mssql", "database": "erp", "username": "sa"},
        {"password": "Dev-Source-2026"},
    )
    _, run = _ingest(
        api,
        cid,
        {"source": {"object": "dbo.suppliers"}, "target": {"layer": "bronze", "dataset": _uniq("erp_suppliers")}},
    )
    assert run["rows_written"] == 30


def test_mongodb(api, sa) -> None:
    cid = _make(
        api, sa, "mongodb", {"host": "src-mongo", "database": "catalog", "username": "dev"}, {"password": DEV_PW}
    )
    ds = _uniq("reviews")
    _, run = _ingest(
        api,
        cid,
        {
            "source": {"object": "reviews"},
            "load_mode": "incremental",
            "watermark_column": "created_at",
            "target": {"layer": "bronze", "dataset": ds},
        },
    )
    docs = compose(
        "exec",
        "-T",
        "src-mongo",
        "mongosh",
        "-u",
        "dev",
        "-p",
        DEV_PW,
        "--authenticationDatabase",
        "admin",
        "--quiet",
        "catalog",
        "--eval",
        "db.reviews.countDocuments()",
    )
    assert run["rows_written"] == int(docs.strip().splitlines()[-1])
    cols = {c["name"]: c["data_type"] for c in _dataset(api, ds)["column_list"]}
    assert cols["author"] == "string"  # nested documents land as JSON text


def test_ftp_jsonl(api, sa) -> None:
    cid = _make(api, sa, "ftp", {"host": "src-ftp", "username": "dev", "base_path": "/"}, {"password": DEV_PW})
    _, run = _ingest(
        api,
        cid,
        {
            "source": {"path_template": "/exports/invoices_{yyyyMMdd}.jsonl"},
            "target": {"layer": "bronze", "dataset": _uniq("invoices")},
        },
    )
    assert run["rows_written"] == 50


def test_smb_share(api, sa) -> None:
    cid = _make(api, sa, "smb", {"host": "src-smb", "share": "landing", "username": "dev"}, {"password": DEV_PW})
    _, run = _ingest(
        api,
        cid,
        {"source": {"path_template": "/finance/budget.csv"}, "target": {"layer": "bronze", "dataset": _uniq("budget")}},
    )
    assert run["rows_written"] == 12


def test_s3_parquet(api, sa) -> None:
    cid = _make(
        api,
        sa,
        "s3",
        {"endpoint_url": "http://src-s3:9000", "bucket": "partner-drop"},
        {"access_key_id": "devsource", "secret_access_key": "devsource-secret"},
    )
    _, run = _ingest(
        api,
        cid,
        {
            "source": {"path_template": "/prices/prices_{yyyyMMdd}.parquet"},
            "target": {"layer": "bronze", "dataset": _uniq("partner_prices")},
        },
    )
    assert run["rows_written"] == 20


def test_local_folder(api) -> None:
    cid = _make(api, "", "local_files", {"base_path": "/data/landing"})
    _, run = _ingest(
        api,
        cid,
        {
            "source": {"path_template": "/customers/customers_{yyyyMMdd}.csv"},
            "target": {"layer": "bronze", "dataset": _uniq("landing_customers")},
        },
    )
    assert run["rows_written"] == 25


@pytest.mark.parametrize(
    ("endpoint", "auth", "secrets", "options", "expected"),
    [
        (
            "/v1/customers",
            {"type": "bearer"},
            {"auth.token": "dev-api-token"},
            {"records_path": "$.data[*]", "pagination": {"type": "page", "page_size": 20}},
            57,
        ),
        (
            "/v1/orders",
            {"type": "api_key", "header_name": "X-API-Key"},
            {"auth.token": "dev-api-key"},
            {"pagination": {"type": "offset", "page_size": 50}},
            123,
        ),
        (
            "/v1/events",
            {
                "type": "oauth2_client_credentials",
                "token_url": "http://mock-api:8090/oauth/token",
                "client_id": "dev-client",
            },
            {"auth.client_secret": "dev-client-secret"},
            {"records_path": "$.data[*]", "pagination": {"type": "cursor", "cursor_path": "$.meta.next"}},
            40,
        ),
        ("/v1/tickets", {"type": "none"}, {}, {"pagination": {"type": "link_header"}}, 25),
    ],
)
def test_rest_api(api, sa, endpoint, auth, secrets, options, expected) -> None:
    cid = _make(api, sa, "rest_api", {"base_url": "http://mock-api:8090", "auth": auth}, secrets)
    ds = _uniq("api" + endpoint.replace("/", "_"))
    _, run = _ingest(
        api, cid, {"source": {"object": endpoint, "options": options}, "target": {"layer": "bronze", "dataset": ds}}
    )
    assert run["rows_written"] == expected


def test_kafka_topic_batches_resume_from_committed_offsets(api) -> None:
    cid = _make(api, "", "kafka", {"bootstrap_servers": "redpanda:9092"})
    ds = _uniq("clicks")
    job, run = _ingest(
        api,
        cid,
        {"source": {"object": "web.clickstream"}, "load_mode": "append", "target": {"layer": "bronze", "dataset": ds}},
    )
    assert run["rows_written"] >= 300
    task = api.post(f"/api/ingestion/jobs/{job['id']}/run").json()["task_id"]
    wait_for_job(api, task)
    again = api.get("/api/ingestion/runs", params={"job_id": job["id"], "limit": 1}).json()[0]
    assert again["rows_written"] == 0  # nothing new since the committed offsets


def test_scheduled_job_runs_via_scheduler(api, sa) -> None:
    cid = _make(api, "", "local_files", {"base_path": "/data/landing"})
    ds = _uniq("sched_customers")
    r = api.post(
        "/api/ingestion/jobs",
        json={
            "name": f"it_{ds}",
            "connection_id": cid,
            "spec": {
                "source": {"path_template": "/customers/*.csv"},
                "target": {"layer": "bronze", "dataset": ds},
                "schedule": {"type": "interval", "interval_seconds": 60},
            },
        },
    )
    job = r.json()
    # Make it due now instead of waiting a minute, then let the scheduler pick it up.
    compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "dataplat",
        "-c",
        f"update schedules set next_run_at = now() - interval '1 second' where name = 'ingest:{job['id']}'",
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        runs = api.get("/api/ingestion/runs", params={"job_id": job["id"]}).json()
        if runs and runs[0]["status"] == "succeeded":
            assert runs[0]["trigger"] == "schedule"
            return
        time.sleep(2)
    pytest.fail("scheduled run did not happen")


def test_ops_summary_and_run_events_on_the_bus(api) -> None:
    ops = api.get("/api/ops/summary").json()
    assert ops["totals"]["succeeded"] >= 10 and ops["datasets"] >= 10

    from confluent_kafka import Consumer

    c = Consumer(
        {
            "bootstrap.servers": "127.0.0.1:19092",
            "group.id": f"it-{uuid.uuid4().hex[:6]}",
            "auto.offset.reset": "earliest",
        }
    )
    c.subscribe(["dataplat.runs"])
    msgs = c.consume(num_messages=50, timeout=10)
    c.close()
    assert any(b'"ingestion.run"' in m.value() for m in msgs if not m.error())


def test_connection_secrets_never_leak(api, sa) -> None:
    haystacks = {
        "metadata dump": compose("exec", "-T", "postgres", "pg_dump", "-U", "postgres", "dataplat"),
        "logs": compose("logs", "--no-color", "api", "worker", "scheduler"),
        "connections": api.get("/api/connections").text,
        "lineage": api.get("/api/lineage/events", params={"limit": 1000}).text,
        "runs": api.get("/api/ingestion/runs", params={"limit": 1000}).text,
    }
    needles = ["dev-client-secret", "dev-api-token", "dev-api-key", "devsource-secret", "Dev-Source-2026"]
    leaks = [(where, n) for where, text in haystacks.items() for n in needles if n in text]
    assert leaks == []
