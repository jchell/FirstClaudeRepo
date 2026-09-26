"""Phase 2 against the running stack: raw vault, gold SCD2 dimension, serving DB, lineage.

The plan's NRT test continued: a CDC flow on the seeded Postgres customers table with
"Add to Raw Vault", an SCD2 dimension pipeline triggered by the satellite, and the
serving replica. INSERT/UPDATE/DELETE in the source must reach the hub/satellite and
gold dim_customer (and the serving DB) within 60 s. A batch job from the same table
feeds the same hub, and lineage traces the gold column back to the source column.
"""

from __future__ import annotations

import time
import uuid

import pytest

from tests.integration.conftest import compose
from tests.integration.test_phase1b_streams import PG, _conn, _psql, _rows, wait

pytestmark = pytest.mark.integration

U = uuid.uuid4().hex[:5]
HUB, SAT, STATUS = f"hub_itc{U}", f"sat_itc{U}_details", f"sat_itc{U}_status"
DIM, PIPE = f"dim_itc{U}", f"it-dims-{U}"
ATTRS = ["first_name", "last_name", "email", "city", "state"]
CREATED: dict[str, list[str]] = {"jobs": [], "pipelines": [], "models": [], "apps": []}


@pytest.fixture(scope="module", autouse=True)
def cleanup(api):
    yield
    for p in CREATED["pipelines"]:
        api.delete(f"/api/transform/pipelines/{p}")
    for m in CREATED["models"]:
        api.delete(f"/api/transform/models/{m}")
    for j in CREATED["jobs"]:
        api.delete(f"/api/ingestion/jobs/{j}")
    for a in CREATED["apps"]:
        api.delete(f"/api/portal/apps/{a}")


@pytest.fixture(scope="module")
def sa(api) -> str:
    name = f"it-dv-{U}"
    assert api.post("/api/admin/service-accounts", json={"name": name}).status_code == 201
    return name


def _serving(sql: str) -> str:
    return compose("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "serving", "-Atqc", sql).strip()


def _dim(api) -> list[dict]:
    return _rows(api, DIM)


def test_cdc_to_vault_to_gold_to_serving_within_60s(api, sa) -> None:
    cid = _conn(api, sa, "postgres", PG, {"password": "devsource"})
    spec = {
        "source": {"object": "crm.customers"},
        "load_mode": "cdc",
        "target": {"layer": "bronze", "dataset": f"itc{U}_changes"},
        "stream": {"write_mode": "changelog", "max_seconds": 1, "max_records": 500},
        "raw_vault": {
            "hub": {"name": HUB, "keys": {"customer_id": "id"}},
            "attributes": ATTRS,
            "satellite": SAT,
            "track_deletes": True,
        },
    }
    r = api.post("/api/ingestion/jobs", json={"name": f"it_dv_{U}", "connection_id": cid, "spec": spec})
    assert r.status_code == 201, r.text
    CREATED["jobs"].append(r.json()["id"])
    objs = {o["name"]: o for o in api.get("/api/vault/objects").json()}
    assert {HUB, SAT, STATUS} <= set(objs) and objs[STATUS]["definition"]["status"] is True

    r = api.post(
        "/api/transform/models",
        json={
            "name": DIM,
            "layer": "gold",
            "kind": "scd2_dimension",
            "config": {
                "source": f"vault.{SAT}",
                "deletes_source": f"vault.{STATUS}",
                "surrogate_key": "customer_sk",
                "serve": True,
            },
        },
    )
    assert r.status_code == 201, r.text
    CREATED["models"].append(DIM)
    r = api.post(
        "/api/transform/pipelines",
        json={"name": PIPE, "models": [DIM], "trigger_datasets": [f"vault.{SAT}", f"vault.{STATUS}"]},
    )
    assert r.status_code == 201, r.text
    CREATED["pipelines"].append(r.json()["id"])

    # Snapshot: every source customer ends up as a current dimension row, and in serving.
    n = int(_psql("select count(*) from crm.customers").strip())
    wait(lambda: sum(1 for r in _dim(api) if r["is_current"]) == n, timeout=120, every=2, what="initial dimension")
    wait(
        lambda: _serving(f"select count(*) from gold.{DIM} where is_current") == str(n), timeout=30, what="serving copy"
    )

    new_id = 8000 + int(uuid.uuid4().int % 1000)
    old_city = _psql("select city from crm.customers where id = 3").strip()
    t0 = time.monotonic()
    _psql(
        f"insert into crm.customers values ({new_id},'Vault','Test','vt@x.io','+1','Tromso','NO',now());"
        f"update crm.customers set city='Alta{U}', updated_at=now() where id=3;"
    )

    def updated():
        rows = [r for r in _dim(api) if r["customer_id"] in ("3", str(new_id))]
        cur = {r["customer_id"]: r for r in rows if r["is_current"]}
        return cur.get("3", {}).get("city") == f"Alta{U}" and str(new_id) in cur and rows

    rows = wait(updated, timeout=60, every=1, what="update in gold dimension")
    gold_s = time.monotonic() - t0
    three = sorted((r for r in rows if r["customer_id"] == "3"), key=lambda r: r["valid_from"])
    assert [r["city"] for r in three][-2:] == [old_city, f"Alta{U}"] and three[-2]["valid_to"] == three[-1][
        "valid_from"
    ]
    wait(
        lambda: _serving(f"select city from gold.{DIM} where customer_id = '3' and is_current") == f"Alta{U}",
        timeout=15,
        what="serving update",
    )

    _psql(f"delete from crm.customers where id={new_id}")
    wait(
        lambda: not any(r["customer_id"] == str(new_id) and r["is_current"] for r in _dim(api)),
        timeout=60,
        every=1,
        what="delete closes the dimension row",
    )
    print(f"\nsource update -> gold SCD2 in {gold_s:.1f}s")

    # The vault kept every version: insert-only hub, two satellite versions for customer 3,
    # and the delete in the status satellite.
    hub_rows = _rows(api, HUB)
    assert len(hub_rows) == n + 1 and len({r[f"hk_itc{U}"] for r in hub_rows}) == n + 1
    status = [r for r in _rows(api, STATUS) if r["is_deleted"]]
    assert len(status) >= 1


def test_batch_source_feeds_the_same_hub_and_lineage_traces_to_source(api, sa) -> None:
    cid = _conn(api, sa, "postgres", PG, {"password": "devsource"})
    spec = {
        "source": {"object": "crm.customers"},
        "load_mode": "full",
        "target": {"layer": "bronze", "dataset": f"itc{U}_batch"},
        "raw_vault": {
            "hub": {"name": HUB, "keys": {"customer_id": "id"}},
            "attributes": ["email"],
            "satellite": f"sat_itc{U}_crm",
        },
    }
    r = api.post("/api/ingestion/jobs", json={"name": f"it_dvb_{U}", "connection_id": cid, "spec": spec})
    assert r.status_code == 201, r.text
    job = r.json()
    CREATED["jobs"].append(job["id"])
    hub_before = len(_rows(api, HUB))
    assert api.post(f"/api/ingestion/jobs/{job['id']}/run").status_code == 202
    wait(lambda: len(_rows(api, f"sat_itc{U}_crm")) > 0, timeout=90, every=2, what="batch vault load")
    assert len(_rows(api, HUB)) == hub_before  # same customers: no new hub rows
    mappings = api.get("/api/vault/mappings", params={"source": f"bronze.itc{U}_batch"}).json()
    assert {m["target"] for m in mappings} == {HUB, f"sat_itc{U}_crm"}

    r = api.post(
        "/api/portal/apps", json={"name": f"it-report-{U}", "url": "https://bi.example/r", "datasets": [f"gold.{DIM}"]}
    )
    assert r.status_code == 201
    CREATED["apps"].append(r.json()["id"])

    g = api.get("/api/lineage/graph").json()
    dim = next(n for n in g["nodes"] if n["label"] == DIM and n["layer"] == "gold")
    cols = api.get("/api/lineage/columns", params={"dataset": dim["id"]}).json()
    city = next(c["id"] for c in cols["columns"] if c["label"] == "city")
    tr = api.get("/api/lineage/trace", params={"column": city}).json()
    path = {(n["layer"], n["dataset"], n["label"]) for n in tr["nodes"]}
    assert ("vault", SAT, "city") in path and ("bronze", f"itc{U}_changes", "city") in path
    assert ("source", "crm.customers", "city") in path

    src_city = next(n["id"] for n in tr["nodes"] if n["layer"] == "source")
    impact = api.get("/api/lineage/impact", params={"node": src_city}).json()
    assert f"it-report-{U}" in {a["name"] for a in impact["affected"]}
    down = api.get("/api/lineage/trace", params={"column": city, "direction": "downstream"}).json()
    assert ("serving", f"gold.{DIM}", "city") in {(n["layer"], n["dataset"], n["label"]) for n in down["nodes"]}

    runs = api.get("/api/ingestion/runs", params={"job_id": job["id"]}).json()
    batch = next(r["batch_id"] for r in runs if r["status"] == "succeeded")
    bt = api.get(f"/api/lineage/batch/{batch}").json()
    found = {d["dataset"] for d in bt["datasets"]}
    assert bt["origin"]["job"] == f"it_dvb_{U}" and {f"bronze.itc{U}_batch", f"vault.sat_itc{U}_crm"} <= found
