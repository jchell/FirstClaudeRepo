"""Phase 3 against the running stack: PII classification and masking, grants, DQ, alerts.

Covers the plan's demo steps "run a DQ rule to produce a score" and "a masked PII
column is hidden for a restricted role", plus email alerts (through the dev Mailpit),
the Vault audit view, and that hash masking uses one Vault-held key in the API
(previews) and the worker (queries).
"""

from __future__ import annotations

import secrets
import uuid

import httpx
import pytest

from tests.integration.conftest import API
from tests.integration.test_phase1b_streams import PG, _conn, wait

pytestmark = pytest.mark.integration

U = uuid.uuid4().hex[:5]
DATASET = f"itg{U}_customers"
MAILPIT = "http://127.0.0.1:8025"


def _user(api: httpx.Client, role: str) -> httpx.Client:
    name, pw = f"it-{role}-{U}", f"Pw-{secrets.token_urlsafe(12)}"
    r = api.post("/api/admin/users", json={"username": name, "password": pw, "roles": [role]})
    assert r.status_code == 201, r.text
    c = httpx.Client(base_url=API, timeout=30)
    tok = c.post("/api/auth/login", json={"username": name, "password": pw}).json()["access_token"]
    c.headers["Authorization"] = f"Bearer {tok}"
    return c


@pytest.fixture(scope="module")
def setup(api) -> dict:
    sa = f"it-gov-{U}"
    assert api.post("/api/admin/service-accounts", json={"name": sa}).status_code == 201
    cid = _conn(api, sa, "postgres", PG, {"password": "devsource"})
    r = api.post(
        "/api/ingestion/jobs",
        json={
            "name": f"it_gov_{U}",
            "connection_id": cid,
            "spec": {
                "source": {"object": "crm.customers"},
                "load_mode": "full",
                "target": {"layer": "bronze", "dataset": DATASET},
            },
        },
    )
    assert r.status_code == 201, r.text
    job = r.json()
    assert api.post(f"/api/ingestion/jobs/{job['id']}/run").status_code == 202
    ds = wait(
        lambda: next(
            (d for d in api.get("/api/catalog/datasets", params={"q": DATASET}).json() if d["name"] == DATASET), None
        ),
        timeout=90,
        every=2,
        what="bronze dataset",
    )
    people = {role: _user(api, role) for role in ("steward", "analyst", "engineer")}
    yield {"job": job, "dataset": ds, **people}
    api.delete(f"/api/ingestion/jobs/{job['id']}")


def _preview(c: httpx.Client, ds_id: str) -> dict:
    return c.get(f"/api/catalog/datasets/{ds_id}/preview", params={"limit": 50}).json()


def _query(c: httpx.Client, sql: str) -> dict:
    task = c.post("/api/query", json={"sql": sql}).json()
    return wait(
        lambda: (lambda t: t if t["status"] in ("succeeded", "failed") else None)(
            c.get(f"/api/tasks/{task['task_id']}").json()
        ),
        timeout=60,
        what="query",
    )["result"]


def test_pii_is_suggested_and_masked_for_restricted_roles(api, setup) -> None:
    stu, ana = setup["steward"], setup["analyst"]
    ds = setup["dataset"]["id"]

    # The load queued a classification scan; email is suggested by name and by values
    sugg = wait(
        lambda: [
            a
            for a in stu.get("/api/governance/assignments", params={"dataset_id": ds, "status": "suggested"}).json()
            if a["column"] == "email"
        ],
        timeout=90,
        every=2,
        what="PII suggestion for email",
    )
    assert sugg[0]["tag"] == "pii.email" and sugg[0]["confidence"] >= 0.8
    names = [
        a
        for a in stu.get("/api/governance/assignments", params={"dataset_id": ds}).json()
        if a["column"] in ("first_name", "phone")
    ]
    assert {a["tag"] for a in names} >= {"pii.name", "pii.phone"}
    assert stu.patch(f"/api/governance/assignments/{sugg[0]['id']}", json={"status": "active"}).status_code == 200

    raw = _preview(ana, ds)
    assert "@" in raw["rows"][0]["email"]  # not masked yet
    r = stu.post(
        "/api/governance/masking",
        json={"name": f"pii-email-{U}", "tag": "pii.email", "method": "hash", "exempt_roles": ["steward"]},
    )
    assert r.status_code == 201, r.text
    try:
        masked = _preview(ana, ds)
        assert masked["masked_columns"] == {"email": "hash"}
        assert all("@" not in (row["email"] or "") for row in masked["rows"])
        assert "@" in _preview(stu, ds)["rows"][0]["email"]  # stewards are exempt
        # One keyed hash in API previews and worker queries (the key lives in Vault)
        by_id = {row["id"]: row["email"] for row in masked["rows"]}
        ids = ", ".join(str(i) for i in list(by_id)[:5])
        q = _query(ana, f"select id, email from {{{{ source('bronze', '{DATASET}') }}}} where id in ({ids})")
        assert q["ok"] and len(q["rows"]) == min(5, len(by_id))
        assert all(by_id[row["id"]] == row["email"] for row in q["rows"])
    finally:
        pol = next(p for p in stu.get("/api/governance/masking").json() if p["name"] == f"pii-email-{U}")
        stu.delete(f"/api/governance/masking/{pol['id']}")


def test_domain_grants_hide_datasets(api, setup) -> None:
    stu, ana, eng = setup["steward"], setup["analyst"], setup["engineer"]
    ds = setup["dataset"]["id"]
    domain = f"it-hr-{U}"
    assert stu.patch(f"/api/catalog/datasets/{ds}", json={"domain": domain}).status_code == 200
    g = stu.post(
        "/api/governance/grants",
        json={"target_type": "domain", "target": domain, "principal_type": "role", "principal": "engineer"},
    )
    assert g.status_code == 201, g.text
    try:
        assert ana.get(f"/api/catalog/datasets/{ds}").status_code == 404
        assert eng.get(f"/api/catalog/datasets/{ds}").status_code == 200
        graph = ana.get("/api/lineage/graph").json()
        assert DATASET not in {n["label"] for n in graph["nodes"]}
        assert any(n.get("restricted") for n in graph["nodes"])
    finally:
        stu.delete(f"/api/governance/grants/{g.json()['id']}")
    assert ana.get(f"/api/catalog/datasets/{ds}").status_code == 200


def test_dq_rule_produces_a_score_and_emails_an_alert(api, setup) -> None:
    stu = setup["steward"]
    ds = setup["dataset"]["id"]
    ch = api.post(
        "/api/admin/notifications",
        json={"name": f"it-mail-{U}", "type": "email", "recipients": [f"ops-{U}@example.com"], "kinds": ["dq_failed"]},
    )
    assert ch.status_code == 201, ch.text
    try:
        ok = stu.post(
            "/api/quality/rules",
            json={
                "name": f"it.{U}.id_unique",
                "dataset_id": ds,
                "column": "id",
                "rule_type": "unique",
                "run_on_load": True,
            },
        ).json()
        bad = stu.post(
            "/api/quality/rules",
            json={
                "name": f"it.{U}.email_domain",
                "dataset_id": ds,
                "column": "email",
                "rule_type": "regex",
                "params": {"pattern": r"^[^@]+@nowhere\.invalid$"},
                "severity": "critical",
            },
        ).json()
        card = stu.post("/api/quality/scorecards", json={"name": f"it-{U}", "rule_ids": [ok["id"], bad["id"]]}).json()
        assert stu.post(f"/api/quality/scorecards/{card['id']}/run").json()["queued"]
        sc = wait(
            lambda: (lambda c: c if c["score"] is not None else None)(
                stu.get(f"/api/quality/scorecards/{card['id']}").json()
            ),
            timeout=60,
            every=1,
            what="scorecard score",
        )
        assert sc["score"] == pytest.approx(
            100 / 3, abs=0.01
        )  # unique passes (100), the critical rule fails (0, weight 2)
        assert sc["dimensions"] == {"uniqueness": 100.0, "validity": 0.0}

        # The failing critical rule opened an alert, which the worker emailed
        def mailed():
            msgs = httpx.get(f"{MAILPIT}/api/v1/messages").json().get("messages", [])
            return [m for m in msgs if any(t["Address"] == f"ops-{U}@example.com" for t in m["To"])]

        msgs = wait(mailed, timeout=60, every=2, what="alert email in Mailpit")
        assert "dq_failed" in msgs[0]["Subject"] and f"rule:it.{U}.email_domain" in msgs[0]["Subject"]

        # run_on_load: reloading the dataset re-evaluates its on-load rule
        before = len(stu.get(f"/api/quality/rules/{ok['id']}/results").json())
        assert api.post(f"/api/ingestion/jobs/{setup['job']['id']}/run").status_code == 202
        wait(
            lambda: len(stu.get(f"/api/quality/rules/{ok['id']}/results").json()) > before,
            timeout=90,
            every=2,
            what="on-load rule run",
        )
    finally:
        api.delete(f"/api/admin/notifications/{ch.json()['id']}")


def test_vault_audit_shows_secret_access_without_values(api) -> None:
    out = api.get("/api/admin/vault-audit", params={"limit": 500}).json()
    assert out["available"] and out["entries"]
    paths = {e["path"] for e in out["entries"]}
    assert any(p and p.startswith("database/creds/") for p in paths)  # dynamic DB logins
    text = str(out)
    assert "devsource" not in text and "hmac-sha256" not in text
