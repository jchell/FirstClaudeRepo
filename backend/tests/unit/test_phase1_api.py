"""Phase 1 API: connections, ingestion jobs, catalog, lineage, ops, portal."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dataplat.api.app import create_app
from dataplat.api.ratelimit import SlidingWindowLimiter
from dataplat.api.routers import auth as auth_router
from dataplat.core.context import PlatformContext
from dataplat.db.models import IngestionJob, JobRow, Schedule, ServiceAccount, User, UserRole
from dataplat.ingestion.runner import IngestionRunner
from dataplat.security.passwords import hash_password

PW = "correct horse battery"


@pytest.fixture
def client(ctx: PlatformContext, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(auth_router, "_login_limiter", SlidingWindowLimiter(1000, 60))
    with ctx.metadata.session() as s:
        for name, role in (("eng", "engineer"), ("ana", "analyst"), ("vic", "viewer")):
            u = User(username=name, password_hash=hash_password(PW))
            s.add(u)
            s.flush()
            s.add(UserRole(user_id=u.id, role=role))
        s.add(ServiceAccount(name="etl", vault_path="kv/dataplat/service-accounts/etl", vault_policy="sa-etl"))
    with TestClient(create_app(ctx)) as c:
        yield c


def h(client: TestClient, user: str = "eng") -> dict[str, str]:
    token = client.post("/api/auth/login", json={"username": user, "password": PW}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


PG = {"host": "db", "database": "sales", "username": "reader"}


def test_connection_types_describe_secret_fields(client: TestClient) -> None:
    types = {t["type"]: t for t in client.get("/api/connection-types", headers=h(client)).json()}
    assert {
        "local_files",
        "sftp",
        "ftp",
        "smb",
        "s3",
        "postgres",
        "mysql",
        "sqlserver",
        "mongodb",
        "rest_api",
        "kafka",
    } <= set(types)
    assert types["postgres"]["secret_fields"] == ["password"]
    assert types["postgres"]["config_schema"]["properties"]["password"]["writeOnly"] is True


def test_connection_secrets_go_to_vault_and_are_never_returned(client: TestClient, ctx: PlatformContext) -> None:
    eng = h(client)
    # Plaintext secret in config is refused.
    r = client.post(
        "/api/connections", headers=eng, json={"name": "crm", "type": "postgres", "config": {**PG, "password": "x"}}
    )
    assert r.status_code == 422 and "secret" in r.text
    # Secrets need a service account.
    r = client.post(
        "/api/connections",
        headers=eng,
        json={"name": "crm", "type": "postgres", "config": PG, "secrets": {"password": "S3cret-pw!"}},
    )
    assert r.status_code == 422 and "service account" in r.text

    r = client.post(
        "/api/connections",
        headers=eng,
        json={
            "name": "crm",
            "type": "postgres",
            "service_account": "etl",
            "config": PG,
            "secrets": {"password": "S3cret-pw!"},
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    ref = body["config"]["password"]
    assert ref == f"vault://kv/dataplat/service-accounts/etl/connections/{body['id']}#password"
    assert "S3cret" not in r.text and ctx.secrets.resolve(ref) == "S3cret-pw!"
    assert "S3cret" not in client.get("/api/connections", headers=eng).text

    # Updating non-secret config keeps the reference; rotating writes a new version.
    cid = body["id"]
    r = client.put(f"/api/connections/{cid}", headers=eng, json={"config": {**PG, "port": 5433}})
    assert r.status_code == 200 and r.json()["config"]["password"] == ref and r.json()["config"]["port"] == 5433
    r = client.put(f"/api/connections/{cid}", headers=eng, json={"secrets": {"password": "rotated-pw"}})
    assert r.status_code == 200 and ctx.secrets.resolve(ref) == "rotated-pw"
    assert ctx.secrets.metadata(f"service-accounts/etl/connections/{cid}")["current_version"] == 2

    # Viewers can list but not create.
    assert (
        client.post(
            "/api/connections",
            headers=h(client, "vic"),
            json={"name": "x", "type": "local_files", "config": {"base_path": "/"}},
        ).status_code
        == 403
    )


def test_rest_api_nested_secret_fields(client: TestClient, ctx: PlatformContext) -> None:
    r = client.post(
        "/api/connections",
        headers=h(client),
        json={
            "name": "api",
            "type": "rest_api",
            "service_account": "etl",
            "config": {"base_url": "https://api.example", "auth": {"type": "bearer"}},
            "secrets": {"auth.token": "tok-123"},
        },
    )
    assert r.status_code == 201, r.text
    assert ctx.secrets.resolve(r.json()["config"]["auth"]["token"]) == "tok-123"


def test_source_tasks_run_as_service_account_and_preview_is_scrubbed(client: TestClient, ctx: PlatformContext) -> None:
    eng = h(client)
    cid = client.post(
        "/api/connections",
        headers=eng,
        json={"name": "crm", "type": "postgres", "service_account": "etl", "config": PG, "secrets": {"password": "pw"}},
    ).json()["id"]
    task = client.post(f"/api/connections/{cid}/test", headers=eng).json()
    assert task["status"] == "queued"
    with ctx.metadata.session() as s:
        row = s.get(JobRow, task["task_id"])
        assert row.service_account == "etl" and row.kind == "connection.test"

    pv = client.post(f"/api/connections/{cid}/preview", headers=eng, json={"object": "public.t"}).json()
    with ctx.metadata.session() as s:
        row = s.get(JobRow, pv["task_id"])
        row.status, row.result = "succeeded", {"rows": [{"email": "ann@x.io"}]}
    first = client.get(f"/api/tasks/{pv['task_id']}", headers=eng).json()
    assert first["result"]["rows"][0]["email"] == "ann@x.io"
    assert client.get(f"/api/tasks/{pv['task_id']}", headers=eng).json()["result"] == {"consumed": True}


@pytest.fixture
def files_conn(client: TestClient, tmp_path: Path) -> str:
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "orders_1.csv").write_text("id,amount\n1,5\n2,7\n")
    r = client.post(
        "/api/connections",
        headers=h(client),
        json={"name": "landing", "type": "local_files", "config": {"base_path": str(tmp_path)}},
    )
    return r.json()["id"]


def _job(client: TestClient, cid: str, **spec_over) -> dict:
    spec = {
        "source": {"path_template": "/in/orders_*.csv"},
        "target": {"layer": "bronze", "dataset": "orders"},
        **spec_over,
    }
    r = client.post(
        "/api/ingestion/jobs", headers=h(client), json={"name": "orders_files", "connection_id": cid, "spec": spec}
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_job_validation(client: TestClient, files_conn: str) -> None:
    eng = h(client)
    bad = [
        {"source": {}, "target": {"layer": "bronze", "dataset": "x"}},  # no path template
        {"source": {"path_template": "/a"}, "target": {"layer": "bronze", "dataset": "Bad Name"}},
        {
            "source": {"path_template": "/a"},
            "target": {"layer": "bronze", "dataset": "x"},
            "schedule": {"type": "cron", "cron": "nope"},
        },
        {"source": {"path_template": "/a"}, "target": {"layer": "bronze", "dataset": "x"}, "raw_vault": {"hub": {}}},
    ]
    for spec in bad:
        r = client.post(
            "/api/ingestion/jobs", headers=eng, json={"name": "j1", "connection_id": files_conn, "spec": spec}
        )
        assert r.status_code == 422, spec


def test_job_versioning_schedule_sync_and_target_ownership(
    client: TestClient, ctx: PlatformContext, files_conn: str
) -> None:
    eng = h(client)
    job = _job(client, files_conn, schedule={"type": "interval", "interval_seconds": 3600})
    assert job["version"] == 1 and job["target"] == "bronze.orders"
    with ctx.metadata.session() as s:
        sched = s.scalars(select(Schedule)).one()
        assert (
            sched.kind == "ingestion.run" and sched.interval_seconds == 3600 and sched.payload["trigger"] == "schedule"
        )

    spec = {**job["spec"], "schedule": {"type": "cron", "cron": "15 2 * * *"}}
    upd = client.put(f"/api/ingestion/jobs/{job['id']}", headers=eng, json={"spec": spec}).json()
    assert upd["version"] == 2
    assert [v["version"] for v in client.get(f"/api/ingestion/jobs/{job['id']}/versions", headers=eng).json()] == [2, 1]
    with ctx.metadata.session() as s:
        assert s.scalars(select(Schedule)).one().cron == "15 2 * * *"

    # Same-content update doesn't bump the version.
    assert client.put(f"/api/ingestion/jobs/{job['id']}", headers=eng, json={"spec": spec}).json()["version"] == 2
    # Another job can't take over the same bronze dataset.
    r = client.post(
        "/api/ingestion/jobs", headers=eng, json={"name": "dup", "connection_id": files_conn, "spec": job["spec"]}
    )
    assert r.status_code == 409

    no_sched = {**spec, "schedule": {"type": "none"}}
    client.put(f"/api/ingestion/jobs/{job['id']}", headers=eng, json={"spec": no_sched})
    with ctx.metadata.session() as s:
        assert s.scalars(select(Schedule)).first() is None


def test_run_now_enqueues_once(client: TestClient, ctx: PlatformContext, files_conn: str) -> None:
    job = _job(client, files_conn)
    eng = h(client)
    r = client.post(f"/api/ingestion/jobs/{job['id']}/run", headers=eng)
    assert r.status_code == 202
    assert client.post(f"/api/ingestion/jobs/{job['id']}/run", headers=eng).status_code == 409
    with ctx.metadata.session() as s:
        row = s.get(JobRow, r.json()["task_id"])
        assert row.kind == "ingestion.run" and row.payload["ingestion_job_id"] == job["id"]


def test_catalog_lineage_ops_after_a_run(client: TestClient, ctx: PlatformContext, files_conn: str) -> None:
    job = _job(client, files_conn)
    with ctx.metadata.session() as s:
        job_id = s.scalars(select(IngestionJob.id)).one()
    IngestionRunner(ctx).run(job_id, None)
    eng = h(client)

    runs = client.get("/api/ingestion/runs", headers=eng).json()
    assert runs[0]["status"] == "succeeded" and runs[0]["job_name"] == job["name"] and runs[0]["rows_written"] == 2
    assert client.get(f"/api/ingestion/jobs/{job['id']}", headers=eng).json()["last_run"]["status"] == "succeeded"

    found = client.get("/api/catalog/datasets", headers=eng, params={"q": "amount"}).json()
    assert [d["name"] for d in found] == ["orders"]
    ds = client.get(f"/api/catalog/datasets/{found[0]['id']}", headers=eng).json()
    assert ds["source_job"] == job["name"] and ds["profile"]["row_count"] == 2
    assert {c["name"] for c in ds["column_list"] if not c["is_audit"]} == {"id", "amount"}

    ana = h(client, "ana")
    prev = client.get(f"/api/catalog/datasets/{found[0]['id']}/preview", headers=ana).json()
    assert sorted(r["id"] for r in prev["rows"]) == [1, 2]
    assert client.get(f"/api/catalog/datasets/{found[0]['id']}/preview", headers=h(client, "vic")).status_code == 403

    r = client.patch(
        f"/api/catalog/datasets/{found[0]['id']}",
        headers=eng,
        json={"description": "Raw orders", "column_descriptions": {"amount": "EUR"}},
    )
    assert r.status_code == 200 and r.json()["description"] == "Raw orders"

    ops = client.get("/api/ops/summary", headers=eng).json()
    assert ops["totals"]["succeeded"] == 1 and ops["totals"]["rows_written"] == 2 and ops["datasets"] == 1

    graph = client.get("/api/lineage/graph", headers=eng).json()
    kinds = {n["type"] for n in graph["nodes"]}
    assert kinds == {"dataset", "job"} and len(graph["edges"]) == 2
    node = next(n["id"] for n in graph["nodes"] if n["label"] == "orders")
    up = client.get("/api/lineage/graph", headers=eng, params={"node": node, "direction": "upstream"}).json()
    assert len(up["nodes"]) == 3
    assert client.get("/api/lineage/graph", headers=eng, params={"node": "dataset:nope"}).status_code == 404


def test_portal_apps(client: TestClient) -> None:
    ana = h(client, "ana")
    r = client.post(
        "/api/portal/apps",
        headers=ana,
        json={"name": "Sales", "url": "https://bi.example/sales", "datasets": ["gold.dim_customer"]},
    )
    assert r.status_code == 201, r.text
    assert (
        client.post("/api/portal/apps", headers=ana, json={"name": "Bad", "url": "javascript:alert(1)"}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/portal/apps", headers=ana, json={"name": "Bad2", "url": "https://x", "datasets": ["x"]}
        ).status_code
        == 422
    )
    assert (
        client.post("/api/portal/apps", headers=h(client, "vic"), json={"name": "V", "url": "https://x"}).status_code
        == 403
    )
    assert [a["name"] for a in client.get("/api/portal/apps", headers=h(client, "vic")).json()] == ["Sales"]
    app_id = r.json()["id"]
    assert client.delete(f"/api/portal/apps/{app_id}", headers=ana).status_code == 204
