"""Phase 1b: near-real-time ingestion against the running stack + dev sources.

Covers the plan's NRT verification: CDC on the seeded Postgres customers table,
INSERT/UPDATE/DELETE visible in bronze within 10 s, lag/latency from
/api/streams/{id}/metrics, and killing the stream worker mid-stream without
duplicates or lost changes. Plus MySQL/MongoDB (and SQL Server) CDC, webhooks,
Kafka topic streams with dead letters, file-arrival triggers and freshness SLAs.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest

from tests.integration.conftest import API, REPO, compose

pytestmark = pytest.mark.integration


def wait(fn, timeout: float = 60, every: float = 0.5, what: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    pytest.fail(f"timed out after {timeout}s waiting for {what}; last={last!r}")


CREATED: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def cleanup(api):
    """Deletes every stream the tests created; for Postgres CDC that also drops the slot."""
    yield
    for job_id in CREATED:
        api.delete(f"/api/ingestion/jobs/{job_id}")


@pytest.fixture(scope="module")
def sa(api) -> str:
    name = f"it-nrt-{uuid.uuid4().hex[:5]}"
    assert api.post("/api/admin/service-accounts", json={"name": name}).status_code == 201
    return name


def _conn(api, sa: str, type_: str, config: dict, secrets: dict | None = None) -> str:
    r = api.post(
        "/api/connections",
        json={
            "name": f"it-{type_}-{uuid.uuid4().hex[:5]}",
            "type": type_,
            "service_account": sa if secrets else None,
            "config": config,
            "secrets": secrets or {},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _stream(api, cid: str, obj: str | None, dataset: str, mode: str = "cdc", write_mode: str = "changelog") -> dict:
    r = api.post(
        "/api/ingestion/jobs",
        json={
            "name": f"it_{dataset}",
            "connection_id": cid,
            "spec": {
                "source": {"object": obj},
                "load_mode": mode,
                "target": {"layer": "bronze", "dataset": dataset},
                "stream": {"write_mode": write_mode, "max_seconds": 1, "max_records": 500},
            },
        },
    )
    assert r.status_code == 201, r.text
    CREATED.append(r.json()["id"])
    return r.json()


def _metrics(api, job_id: str) -> dict:
    return api.get(f"/api/streams/{job_id}/metrics").json()


def _rows(api, dataset: str) -> list[dict]:
    found = [d for d in api.get("/api/catalog/datasets", params={"q": dataset}).json() if d["name"] == dataset]
    if not found:
        return []
    return api.get(f"/api/catalog/datasets/{found[0]['id']}/preview", params={"limit": 500}).json()["rows"]


def _uniq(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:5]}"


def _psql(sql: str) -> str:
    return compose("exec", "-T", "src-postgres", "psql", "-U", "dev", "-d", "sales", "-Atqc", sql)


PG = {"host": "src-postgres", "database": "sales", "username": "dev"}


def test_postgres_cdc_mirror_within_10s_and_metrics(api, sa) -> None:
    cid = _conn(api, sa, "postgres", PG, {"password": "devsource"})
    ds = _uniq("cdc_customers")
    job = _stream(api, cid, "crm.customers", ds, write_mode="mirror")

    # Connector configs only carry Vault placeholders, never the password.
    name = f"dataplat-cdc-{uuid.UUID(job['id']).hex[:12]}"
    cfg = httpx.get(f"http://127.0.0.1:8083/connectors/{name}/config").json()
    assert cfg["database.password"].startswith(
        "${vault:kv/data/dataplat/service-accounts/"
    ) and "devsource" not in json.dumps(cfg)

    source_count = int(_psql("select count(*) from crm.customers").strip())
    wait(lambda: len(_rows(api, ds)) == source_count, timeout=90, what="complete snapshot")
    new_id = 7000 + int(uuid.uuid4().int % 1000)
    victim = int(_psql("select min(id) from crm.customers where id > 3").strip())
    try:
        t0 = time.monotonic()
        _psql(
            f"insert into crm.customers values ({new_id},'Nrt','Test','nrt@x.io','+1','Oslo','NO',now());"
            "update crm.customers set city='Bodo', updated_at=now() where id=3;"
            f"delete from crm.customers where id={victim};"
        )

        def applied():
            rows = {r["id"]: r for r in _rows(api, ds)}
            return new_id in rows and victim not in rows and rows.get(3, {}).get("city") == "Bodo" and rows

        rows = wait(applied, timeout=10, every=0.25, what="insert/update/delete in bronze")
        assert time.monotonic() - t0 < 10
        assert len(rows) == source_count  # one in, one out
        assert rows[3]["_op"] == "u" and rows[new_id]["_op"] == "c"
    finally:
        _psql(f"delete from crm.customers where id={new_id}")

    m = wait(lambda: (x := _metrics(api, job["id"]))["latency_p50_ms"] is not None and x, what="metrics")
    assert m["lag"] is not None and m["lag"] >= 0 and m["latency_p95_ms"] >= m["latency_p50_ms"] >= 0
    assert m["series"]
    wait(
        lambda: (_metrics(api, job["id"])["stream"]["connector"] or {}).get("state") == "RUNNING",
        timeout=30,
        what="connector health reported",
    )


def test_stream_worker_killed_mid_stream_loses_and_duplicates_nothing(api, sa) -> None:
    """Changelog mode appends every change, so any replay would show up as a duplicate."""
    _psql("drop table if exists crm.ticks; create table crm.ticks (id int primary key, note text)")
    cid = _conn(api, sa, "postgres", PG, {"password": "devsource"})
    ds = _uniq("cdc_ticks")
    job = _stream(api, cid, "crm.ticks", ds)
    wait(lambda: _metrics(api, job["id"])["stream"]["status"] == "running", what="stream running")

    total = 400
    done = threading.Event()

    def writer() -> None:
        # 20 batches of 20 rows over several seconds, so the kill lands mid-stream.
        for i in range(0, total, 20):
            _psql(f"insert into crm.ticks select g, 'n' || g from generate_series({i}, {i + 19}) g")
            time.sleep(0.3)
        done.set()

    t = threading.Thread(target=writer)
    t.start()
    wait(lambda: len(_rows(api, ds)) > 30, timeout=60, what="first rows")
    compose("kill", "-s", "SIGKILL", "stream-worker")  # no graceful shutdown
    time.sleep(3)
    compose("up", "-d", "--no-build", "stream-worker")
    t.join()

    def complete():
        rows = _rows(api, ds)
        return len({r["id"] for r in rows}) >= total and rows

    rows = wait(complete, timeout=120, every=1, what="all changes after the restart")
    ids = [r["id"] for r in rows]
    assert sorted(set(ids)) == list(range(total))  # nothing lost
    assert len(ids) == total, f"{len(ids) - total} duplicate rows"  # nothing replayed
    assert len({r["_offset"] for r in rows}) == total


def test_mysql_cdc_changelog(api, sa) -> None:
    # Debezium's MySQL connector needs replication privileges: use the dev root login.
    cid = _conn(
        api, sa, "mysql", {"host": "src-mysql", "database": "shop", "username": "root"}, {"password": "devsource"}
    )
    ds = _uniq("cdc_products")
    job = _stream(api, cid, "products", ds)
    wait(lambda: len(_rows(api, ds)) >= 80, timeout=120, what="mysql snapshot")
    sku = f"SKU-N{uuid.uuid4().hex[:4]}"
    compose(
        "exec",
        "-T",
        "src-mysql",
        "mysql",
        "-udev",
        "-pdevsource",
        "shop",
        "-e",
        f"insert into products values ('{sku}', 'New', 1.50, 3, now())",
    )
    row = wait(lambda: next((r for r in _rows(api, ds) if r["sku"] == sku), None), timeout=15, what="mysql insert")
    assert row["_op"] == "c" and row["_source_ts"]
    assert _metrics(api, job["id"])["stream"]["kind"] == "cdc"


def test_mongodb_cdc_mirror(api, sa) -> None:
    cid = _conn(
        api,
        sa,
        "mongodb",
        {"host": "src-mongo", "database": "catalog", "username": "dev", "replica_set": "rs0"},
        {"password": "devsource"},
    )
    ds = _uniq("cdc_reviews")
    _stream(api, cid, "reviews", ds, write_mode="mirror")
    wait(lambda: len(_rows(api, ds)) >= 100, timeout=120, what="mongo snapshot")
    rid = 9000 + int(uuid.uuid4().int % 999)
    compose(
        "exec",
        "-T",
        "src-mongo",
        "mongosh",
        "-u",
        "dev",
        "-p",
        "devsource",
        "--authenticationDatabase",
        "admin",
        "--quiet",
        "catalog",
        "--eval",
        f"db.reviews.insertOne({{review_id: {rid}, stars: 5, text: 'fresh'}}); db.reviews.deleteOne({{review_id: 1}})",
    )

    def applied():
        rows = _rows(api, ds)
        ids = {r.get("review_id") for r in rows}
        return rid in ids and 1 not in ids

    wait(applied, timeout=15, what="mongo insert and delete")


@pytest.mark.skipif(not os.environ.get("DATAPLAT_TEST_MSSQL"), reason="mssql profile not running")
def test_sqlserver_cdc(api, sa) -> None:
    cid = _conn(
        api,
        sa,
        "sqlserver",
        {"host": "src-mssql", "database": "erp", "username": "sa"},
        {"password": "Dev-Source-2026"},
    )
    ds = _uniq("cdc_suppliers")
    _stream(api, cid, "dbo.suppliers", ds)
    wait(lambda: len(_rows(api, ds)) >= 30, timeout=180, what="sql server snapshot")
    compose(
        "exec",
        "-T",
        "src-mssql",
        "/opt/mssql-tools18/bin/sqlcmd",
        "-C",
        "-S",
        "localhost",
        "-U",
        "sa",
        "-P",
        "Dev-Source-2026",
        "-d",
        "erp",
        "-Q",
        "insert into dbo.suppliers values (999, 'Late', 'NO', 4.2)",
    )
    wait(lambda: any(r["id"] == 999 for r in _rows(api, ds)), timeout=30, what="sql server insert")


def test_webhook_stream(api) -> None:
    name = f"orders-{uuid.uuid4().hex[:5]}"
    cid = _conn(api, "", "webhook", {"stream_name": name})
    key = api.post(f"/api/connections/{cid}/webhook-key").json()["key"]
    ds = _uniq("hook_orders")
    _stream(api, cid, None, ds, mode="stream")
    url = f"{API}/ingest/events/{name}"
    assert httpx.post(url, json={"a": 1}).status_code == 401
    assert httpx.post(url, json={"a": 1}, headers={"X-API-Key": "whk_wrong"}).status_code == 401
    assert httpx.post(url, json=[1, 2], headers={"X-API-Key": key}).status_code == 422
    assert httpx.post(url, content=b"x" * 1_100_000, headers={"X-API-Key": key}).status_code == 413
    r = httpx.post(url, json=[{"order": i, "total": i * 2.5} for i in range(25)], headers={"X-API-Key": key})
    assert r.status_code == 202 and r.json() == {"accepted": 25}
    # Through the console's nginx too (same origin as the UI).
    assert (
        httpx.post(
            f"http://127.0.0.1:3000/ingest/events/{name}", json={"order": 99, "total": 1.0}, headers={"X-API-Key": key}
        ).status_code
        == 202
    )
    rows = wait(lambda: len(r := _rows(api, ds)) >= 26 and r, timeout=20, what="webhook events in bronze")
    assert {row["order"] for row in rows} == set(range(25)) | {99}
    assert key not in compose("logs", "--no-color", "api", "stream-worker")


def test_kafka_stream_with_dead_letters(api) -> None:
    from confluent_kafka import Producer

    topic = f"it.events.{uuid.uuid4().hex[:5]}"
    p = Producer({"bootstrap.servers": "127.0.0.1:19092"})
    for i in range(10):
        p.produce(topic, json.dumps({"n": i}).encode())
    p.produce(topic, b"not json at all")
    p.produce(topic, b"[1, 2, 3]")
    p.flush(10)
    cid = _conn(api, "", "kafka", {"bootstrap_servers": "redpanda:9092"})
    ds = _uniq("stream_events")
    job = _stream(api, cid, topic, ds, mode="stream")
    wait(lambda: len(_rows(api, ds)) >= 10, timeout=30, what="stream rows")
    m = wait(lambda: (x := _metrics(api, job["id"]))["stream"]["totals"].get("dlq") == 2 and x, what="dead letters")
    dlq = api.get(f"/api/streams/{job['id']}/dlq").json()
    assert len(dlq) == 2 and all(d["error"] for d in dlq)
    assert m["stream"]["totals"]["records"] == 10


def test_pause_resume_and_resnapshot(api, sa) -> None:
    cid = _conn(api, sa, "postgres", PG, {"password": "devsource"})
    ds = _uniq("cdc_pause")
    job = _stream(api, cid, "crm.orders", ds, write_mode="mirror")
    wait(lambda: len(_rows(api, ds)) >= 200, timeout=90, what="snapshot")
    assert api.post(f"/api/streams/{job['id']}/pause").json()["desired"] == "paused"
    wait(lambda: _metrics(api, job["id"])["stream"]["status"] == "paused", what="paused")
    assert api.post(f"/api/streams/{job['id']}/resume").json()["desired"] == "running"
    wait(lambda: _metrics(api, job["id"])["stream"]["status"] == "running", what="resumed")
    assert api.post(f"/api/streams/{job['id']}/resnapshot").status_code == 202
    wait(
        lambda: (_metrics(api, job["id"])["stream"]["connector"] or {}).get("state") == "RUNNING",
        timeout=60,
        what="connector back",
    )
    assert api.post(f"/api/ingestion/jobs/{job['id']}/run").status_code == 409


def test_file_arrival_trigger(api) -> None:
    landing = Path(os.environ.get("DATAPLAT_LANDING", REPO / "samples" / "landing"))
    folder = f"arrivals_{uuid.uuid4().hex[:5]}"
    (landing / folder).mkdir(parents=True)
    cid = _conn(api, "", "local_files", {"base_path": "/data/landing"})
    ds = _uniq("arrivals")
    r = api.post(
        "/api/ingestion/jobs",
        json={
            "name": f"it_{ds}",
            "connection_id": cid,
            "spec": {
                "source": {"path_template": f"/{folder}/*.csv"},
                "load_mode": "incremental",
                "target": {"layer": "bronze", "dataset": ds},
                "schedule": {"type": "file_arrival", "poll_seconds": 10},
            },
        },
    )
    assert r.status_code == 201, r.text
    job = r.json()
    time.sleep(12)  # nothing there yet: no run
    assert api.get("/api/ingestion/runs", params={"job_id": job["id"]}).json() == []
    (landing / folder / "a.csv").write_text("id,v\n1,x\n2,y\n")
    run = wait(
        lambda: (
            (r := api.get("/api/ingestion/runs", params={"job_id": job["id"]}).json())
            and r[0]["status"] == "succeeded"
            and r[0]
        ),
        timeout=60,
        every=2,
        what="file-arrival run",
    )
    assert run["trigger"] == "file_arrival" and run["rows_written"] == 2


def test_freshness_sla_alert_opens_and_resolves(api) -> None:
    cid = _conn(api, "", "local_files", {"base_path": "/data/landing"})
    ds = _uniq("sla_customers")
    job = api.post(
        "/api/ingestion/jobs",
        json={
            "name": f"it_{ds}",
            "connection_id": cid,
            "spec": {"source": {"path_template": "/customers/*.csv"}, "target": {"layer": "bronze", "dataset": ds}},
        },
    ).json()
    task = api.post(f"/api/ingestion/jobs/{job['id']}/run").json()["task_id"]
    from tests.integration.conftest import wait_for_job

    wait_for_job(api, task)
    dataset = next(d for d in api.get("/api/catalog/datasets", params={"q": ds}).json() if d["name"] == ds)
    # Make it stale: pretend the last load was two hours ago, with a one-hour SLA.
    compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "dataplat",
        "-qc",
        f"update datasets set last_loaded_at = now() - interval '2 hours' where id = '{dataset['id']}'",
    )
    assert api.patch(f"/api/catalog/datasets/{dataset['id']}", json={"freshness_sla_minutes": 60}).status_code == 200
    target = f"bronze.{ds}"
    alert = wait(
        lambda: next((a for a in api.get("/api/alerts").json() if a["target"] == target), None),
        timeout=90,
        every=3,
        what="freshness alert",
    )
    assert alert["kind"] == "freshness" and "stale" in alert["message"]
    task = api.post(f"/api/ingestion/jobs/{job['id']}/run").json()["task_id"]
    wait_for_job(api, task)
    wait(
        lambda: not any(a["target"] == target for a in api.get("/api/alerts").json()),
        timeout=90,
        every=3,
        what="alert resolved after a fresh load",
    )


def test_deleting_a_postgres_cdc_job_drops_its_replication_slot(api, sa) -> None:
    cid = _conn(api, sa, "postgres", PG, {"password": "devsource"})
    job = _stream(api, cid, "crm.orders", _uniq("cdc_slot"))
    slot = f"dataplat_{uuid.UUID(job['id']).hex[:12]}"
    wait(lambda: slot in _psql("select slot_name from pg_replication_slots"), timeout=60, what="slot created")
    assert api.delete(f"/api/ingestion/jobs/{job['id']}").status_code == 204
    CREATED.remove(job["id"])
    wait(
        lambda: slot not in _psql("select slot_name from pg_replication_slots"),
        timeout=60,
        every=2,
        what="slot dropped",
    )
    assert slot not in _psql("select pubname from pg_publication")
