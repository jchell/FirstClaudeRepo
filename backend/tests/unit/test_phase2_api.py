"""Phase 2 through the API: wizard "Add to Raw Vault", models, pipelines, lineage explorer."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from dataplat.core.context import PlatformContext
from dataplat.db.models import IngestionJob, IngestionRun
from dataplat.ingestion.runner import IngestionRunner
from dataplat.orchestration.worker import Worker
from tests.unit.test_phase1_api import client, h  # noqa: F401  (fixture)


def drain(ctx: PlatformContext) -> list[str]:
    """Runs queued jobs until the queue is empty; returns the kinds run."""
    w, kinds = Worker(ctx, "test"), []
    while True:
        job = ctx.jobs.claim("peek", None)
        if job is None:
            return kinds
        # put it back for the worker (claim() above only peeks at the kind)
        from dataplat.db.models import JobRow

        with ctx.metadata.session() as s:
            row = s.get(JobRow, job.id)
            row.status, row.attempts, row.locked_by = "queued", row.attempts - 1, None
        kinds.append(job.kind)
        w.run_one()


def test_raw_vault_models_pipelines_and_lineage(client: TestClient, ctx: PlatformContext, tmp_path: Path) -> None:  # noqa: F811
    eng = h(client)
    (tmp_path / "in").mkdir()
    csv = tmp_path / "in" / "customers.csv"
    csv.write_text("cust_id,name,city\n1,Ann,Oslo\n2,Bob,Rome\n")
    conn = client.post(
        "/api/connections",
        headers=eng,
        json={"name": "files", "type": "local_files", "config": {"base_path": str(tmp_path)}},
    ).json()["id"]

    # Wizard: bronze job + "Add to Raw Vault" + promote to silver
    spec = {
        "source": {"path_template": "/in/customers.csv"},
        "target": {"layer": "bronze", "dataset": "customers"},
        "raw_vault": {
            "hub": {"name": "hub_customer", "keys": {"customer_id": "cust_id"}},
            "attributes": ["name", "city"],
        },
        "promote_to_silver": True,
    }
    r = client.post("/api/ingestion/jobs", headers=eng, json={"name": "customers", "connection_id": conn, "spec": spec})
    assert r.status_code == 201, r.text
    objs = {o["name"]: o for o in client.get("/api/vault/objects", headers=eng).json()}
    assert set(objs) == {"hub_customer", "sat_customer_details"}
    assert (
        objs["sat_customer_details"]["definition"]["parent"] == "hub_customer" and objs["hub_customer"]["mappings"] == 1
    )

    # A gold SCD2 dimension, served, rebuilt whenever the satellite changes
    r = client.post(
        "/api/transform/models",
        headers=eng,
        json={
            "name": "dim_customer",
            "layer": "gold",
            "kind": "scd2_dimension",
            "config": {"source": "vault.sat_customer_details", "surrogate_key": "customer_sk", "serve": True},
        },
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/api/transform/pipelines",
        headers=eng,
        json={"name": "dims", "models": ["dim_customer"], "trigger_datasets": ["vault.sat_customer_details"]},
    )
    assert r.status_code == 201, r.text

    with ctx.metadata.session() as s:
        job_id = s.scalars(select(IngestionJob.id)).one()
    IngestionRunner(ctx).run(job_id, None)
    kinds = drain(ctx)
    assert kinds[:2] == ["vault.load", "pipeline.run"] and "pipeline.run" in kinds  # promotion + dims

    models = {m["name"]: m for m in client.get("/api/transform/models", headers=eng).json()}
    assert models["customers"]["layer"] == "silver" and models["customers"]["last_run"]["status"] == "succeeded"
    assert models["dim_customer"]["rows"] == 2
    assert {r["city"] for r in ctx.serving.read("gold", "dim_customer")} == {"Oslo", "Rome"}

    # Bob moves: the change flows bronze -> vault -> gold -> serving without anyone clicking
    csv.write_text("cust_id,name,city\n1,Ann,Oslo\n2,Bob,Paris\n")
    IngestionRunner(ctx).run(job_id, None)
    drain(ctx)
    served = ctx.serving.read("gold", "dim_customer")
    assert sorted((r["customer_id"], r["city"], r["is_current"]) for r in served) == [
        ("1", "Oslo", True),
        ("2", "Paris", True),
        ("2", "Rome", False),
    ]

    # Lineage explorer: table graph spans source -> bronze -> vault -> gold -> report
    client.post(
        "/api/portal/apps",
        headers=eng,
        json={"name": "Customer report", "url": "https://bi.example/c", "datasets": ["gold.dim_customer"]},
    )
    g = client.get("/api/lineage/graph", headers=eng).json()
    layers = {n["layer"] for n in g["nodes"]}
    assert {"source", "bronze", "vault", "silver", "gold", "serving", "report"} <= layers
    assert all(n.get("status") == "succeeded" for n in g["nodes"] if n["type"] == "job")

    dim = next(n for n in g["nodes"] if n["label"] == "dim_customer")
    cols = client.get("/api/lineage/columns", headers=eng, params={"dataset": dim["id"]}).json()
    city = next(c["id"] for c in cols["columns"] if c["label"] == "city")
    tr = client.get("/api/lineage/trace", headers=eng, params={"column": city}).json()
    path = {(n["layer"], n["dataset"], n["label"]) for n in tr["nodes"]}
    assert ("vault", "sat_customer_details", "city") in path and ("bronze", "customers", "city") in path
    assert any(layer == "source" and col == "city" and ds.endswith("customers.csv") for layer, ds, col in path)
    assert {s["job"] for s in tr["steps"]} >= {"model.dim_customer", "vault.load.bronze.customers", "ingest.customers"}

    bronze_city = next(n["id"] for n in tr["nodes"] if n["layer"] == "bronze")
    imp = client.get("/api/lineage/impact", headers=eng, params={"node": bronze_city}).json()
    names = {a["name"] for a in imp["affected"]}
    assert {"dim_customer", "sat_customer_details", "Customer report"} <= names

    with ctx.metadata.session() as s:
        batch = s.scalars(select(IngestionRun.batch_id).order_by(IngestionRun.started_at.desc())).first()
    bt = client.get(f"/api/lineage/batch/{batch}", headers=eng).json()
    assert bt["origin"]["job"] == "customers" and bt["origin"]["files"][0]["path"].endswith("customers.csv")
    found = {d["dataset"]: d["rows"] for d in bt["datasets"]}
    assert found["bronze.customers"] == 2 and found["vault.sat_customer_details"] == 1  # only Bob's new version
    assert found["gold.dim_customer"] == 1 and bt["apps"] == ["Customer report"]

    # Model preview runs on a worker and is handed out once
    r = client.post(
        "/api/transform/preview",
        headers=eng,
        json={"sql": "select customer_id, city from {{ ref('dim_customer') }} where is_current"},
    ).json()
    drain(ctx)
    pv = client.get(f"/api/tasks/{r['task_id']}", headers=eng).json()
    assert pv["result"]["ok"] and len(pv["result"]["rows"]) == 2
    assert pv["result"]["lineage"]["city"] == ["gold.dim_customer.city"]
    assert client.get(f"/api/tasks/{r['task_id']}", headers=eng).json()["result"] == {"consumed": True}


def test_definition_errors(client: TestClient, ctx: PlatformContext) -> None:  # noqa: F811
    eng = h(client)
    bad_models = [
        {"name": "x1", "layer": "gold", "sql": "delete from t"},
        {"name": "x2", "layer": "gold", "sql": "select * from {{ ref('missing') }}"},
        {"name": "x3", "layer": "gold", "kind": "date_dimension", "sql": "select 1"},
        {"name": "x4", "layer": "gold", "sql": "select 1", "config": {"unique_key": ["id"]}},
    ]
    for body in bad_models:
        assert client.post("/api/transform/models", headers=eng, json=body).status_code == 422, body
    ok = {"name": "ma", "layer": "silver", "sql": "select 1 as x"}
    assert client.post("/api/transform/models", headers=eng, json=ok).status_code == 201
    assert (
        client.post(
            "/api/transform/models",
            headers=eng,
            json={"name": "mb", "layer": "silver", "sql": "select * from {{ ref('ma') }}"},
        ).status_code
        == 201
    )
    r = client.put("/api/transform/models/ma", headers=eng, json={"sql": "select * from {{ ref('mb') }}"})
    assert r.status_code == 422 and "cycle" in r.text
    assert client.delete("/api/transform/models/ma", headers=eng).status_code == 409
    assert client.post("/api/transform/models", headers=h(client, "ana"), json={**ok, "name": "mc"}).status_code == 403

    assert (
        client.post(
            "/api/vault/objects",
            headers=eng,
            json={"kind": "hub", "name": "hub_x", "definition": {"business_keys": ["k"]}},
        ).status_code
        == 201
    )
    r = client.post(
        "/api/vault/objects",
        headers=eng,
        json={"kind": "sat", "name": "sat_y", "definition": {"parent": "hub_nope", "attributes": ["a"]}},
    )
    assert r.status_code == 422 and "does not exist" in r.text
    assert (
        client.post(
            "/api/vault/objects",
            headers=eng,
            json={"kind": "sat", "name": "sat_x", "definition": {"parent": "hub_x", "attributes": ["a"]}},
        ).status_code
        == 201
    )
    assert client.delete("/api/vault/objects/hub_x", headers=eng).status_code == 409
    r = client.post(
        "/api/vault/mappings", headers=eng, json={"source_dataset": "t", "target": "sat_x", "keys": {"k": "id"}}
    )
    assert r.status_code == 422 and "attributes" in r.text
    r = client.post(
        "/api/vault/mappings",
        headers=eng,
        json={"source_dataset": "t", "target": "sat_x", "keys": {"k": "id"}, "attributes": {"a": "col_a"}},
    )
    assert r.status_code == 201 and r.json()["target_kind"] == "sat"
    d = client.get("/api/vault/diagram", headers=eng).json()
    assert {"source": "sat_x", "target": "hub_x", "kind": "describes"} in d["edges"]
    assert {"source": "bronze.t", "target": "sat_x", "kind": "loads"} in d["edges"]
