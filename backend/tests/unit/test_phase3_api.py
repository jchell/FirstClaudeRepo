"""Phase 3 through the API: classification, masking, row filters, grants, DQ, query, notifications."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dataplat.catalog import service as catalog
from dataplat.core.context import PlatformContext
from dataplat.db.models import Dataset, JobRow, NotificationChannel, Schedule, User, UserRole
from dataplat.orchestration.alerts import open_alert
from dataplat.security.passwords import hash_password
from tests.unit.test_phase1_api import PW, client, h  # noqa: F401  (fixture)
from tests.unit.test_phase2_api import drain


@pytest.fixture
def people(client: TestClient, ctx: PlatformContext) -> TestClient:  # noqa: F811
    from dataplat.db.models import Role

    with ctx.metadata.session() as s:
        s.add(Role(name="sales_analyst", description="Sales analysts"))
        s.flush()
        for name, role in (("stu", "steward"), ("sam", "sales_analyst"), ("ana2", "analyst")):
            u = User(username=name, password_hash=hash_password(PW))
            s.add(u)
            s.flush()
            s.add(UserRole(user_id=u.id, role=role))
    return client


def _customers(ctx: PlatformContext) -> str:
    t = pa.table(
        {
            "id": [1, 2, 3],
            "contact": ["ann@x.io", "bob@y.io", "cy@z.io"],
            "country": ["NO", "US", "UK"],
            "_batch_id": ["b" * 32] * 3,
        }
    )
    uri = ctx.config.lake.uri("silver", "customers")
    ctx.tables.write(t, uri, "overwrite")
    from dataplat.db.models import DatasetProfile
    from dataplat.quality.profiler import profile

    with ctx.metadata.session() as s:
        ds, _ = catalog.register(s, layer="silver", name="customers", uri=uri, schema=t.schema)
        s.add(DatasetProfile(dataset_id=ds.id, row_count=3, columns=profile(t)["columns"]))
        return str(ds.id)


def test_pii_suggestion_masking_and_row_filters(people: TestClient, ctx: PlatformContext) -> None:
    c = people
    ds = _customers(ctx)
    stu, ana, eng = h(c, "stu"), h(c, "ana"), h(c, "eng")

    # Scan: "contact" holds emails, though its name doesn't say so
    assert c.post("/api/governance/classify", headers=stu).json()["queued"]
    drain(ctx)
    sugg = c.get("/api/governance/assignments", headers=stu, params={"status": "suggested"}).json()
    email = next(a for a in sugg if a["column"] == "contact")
    assert email["tag"] == "pii.email" and email["source"] == "pattern" and "look like email" in email["reason"]
    assert (
        c.patch(f"/api/governance/assignments/{email['id']}", headers=ana, json={"status": "active"}).status_code == 403
    )
    assert (
        c.patch(f"/api/governance/assignments/{email['id']}", headers=stu, json={"status": "active"}).json()["status"]
        == "active"
    )

    r = c.post(
        "/api/governance/masking",
        headers=stu,
        json={"name": "pii", "tag": "pii", "method": "redact", "exempt_roles": ["steward"]},
    )
    assert r.status_code == 201, r.text
    assert (
        c.post(
            "/api/governance/masking", headers=stu, json={"name": "x", "tag": "pii", "exempt_roles": ["nope"]}
        ).status_code
        == 422
    )

    prev = c.get(f"/api/catalog/datasets/{ds}/preview", headers=ana).json()
    assert prev["masked_columns"] == {"contact": "redact"} and {r["contact"] for r in prev["rows"]} == {"****"}
    detail = c.get(f"/api/catalog/datasets/{ds}", headers=ana).json()
    prof = {p["name"]: p for p in detail["profile"]["columns"]}
    assert all(t["value"] == "****" for t in prof["contact"]["top_values"])
    assert {t["tag"] for t in detail["tags"]} >= {"pii.email"}
    raw = c.get(f"/api/catalog/datasets/{ds}/preview", headers=stu).json()
    assert "ann@x.io" in {r["contact"] for r in raw["rows"]}

    # Row filter: analysts see Norwegian customers only
    bad = c.post(
        "/api/governance/row-filters",
        headers=stu,
        json={"name": "bad_filter", "dataset_id": ds, "predicate": "no_such_col = 1"},
    )
    assert bad.status_code == 422 and "doesn't run" in bad.text
    r = c.post(
        "/api/governance/row-filters",
        headers=stu,
        json={"name": "norway", "dataset_id": ds, "predicate": "country = 'NO'", "exempt_roles": ["steward"]},
    )
    assert r.status_code == 201, r.text
    assert [r["id"] for r in c.get(f"/api/catalog/datasets/{ds}/preview", headers=ana).json()["rows"]] == [1]
    eff = c.get("/api/governance/effective", headers=stu, params={"dataset_id": ds, "role": "analyst"}).json()
    assert eff["masks"] == {"contact": "redact"} and eff["row_filters"] == ["country = 'NO'"]

    # Ad-hoc query: the analyst's SQL only sees the secured rows
    task = c.post(
        "/api/query",
        headers=ana,
        json={"sql": "select id, contact from {{ source('silver', 'customers') }} order by id"},
    ).json()
    drain(ctx)
    assert c.get(f"/api/tasks/{task['task_id']}", headers=eng).status_code == 404  # someone else's results
    res = c.get(f"/api/tasks/{task['task_id']}", headers=ana).json()["result"]
    assert res["ok"] and res["rows"] == [{"id": 1, "contact": "****"}]

    # Model previews read inputs under the builder's own policies too
    pv = c.post(
        "/api/transform/preview", headers=eng, json={"sql": "select contact from {{ source('silver', 'customers') }}"}
    ).json()
    drain(ctx)
    rows = c.get(f"/api/tasks/{pv['task_id']}", headers=eng).json()["result"]["rows"]
    assert {r["contact"] for r in rows} == {"****"} and len(rows) == 1


def test_grants_restrict_catalog_lineage_and_queries(people: TestClient, ctx: PlatformContext) -> None:
    c = people
    ds = _customers(ctx)
    stu, ana, sam = h(c, "stu"), h(c, "ana"), h(c, "sam")
    assert c.patch(f"/api/catalog/datasets/{ds}", headers=h(c, "eng"), json={"domain": "sales"}).status_code == 403
    assert c.patch(f"/api/catalog/datasets/{ds}", headers=stu, json={"domain": "sales"}).json()["domain"] == "sales"
    r = c.post(
        "/api/governance/grants",
        headers=stu,
        json={"target_type": "domain", "target": "sales", "principal_type": "role", "principal": "sales_analyst"},
    )
    assert r.status_code == 201, r.text

    assert [d["name"] for d in c.get("/api/catalog/datasets", headers=ana).json()] == []
    assert c.get(f"/api/catalog/datasets/{ds}", headers=ana).status_code == 404
    assert c.get(f"/api/catalog/datasets/{ds}/preview", headers=ana).status_code == 404
    assert [d["restricted"] for d in c.get("/api/catalog/datasets", headers=sam).json()] == [True]
    task = c.post("/api/query", headers=ana, json={"sql": "select * from {{ source('silver', 'customers') }}"}).json()
    drain(ctx)
    res = c.get(f"/api/tasks/{task['task_id']}", headers=ana).json()["result"]
    assert not res["ok"] and "no access" in res["error"]

    # Lineage: a restricted dataset is a placeholder with no name
    from dataplat.lineage.openlineage import dataset as ol
    from dataplat.lineage.openlineage import run_event

    lake = ctx.config.lake.model_dump()
    ctx.lineage.emit(
        run_event(
            "COMPLETE",
            "dataplat",
            "model.report",
            inputs=[ol(lake["silver"], "customers")],
            outputs=[ol(lake["gold"], "report")],
        )
    )
    g = c.get("/api/lineage/graph", headers=ana).json()
    labels = {n["label"] for n in g["nodes"]}
    assert "customers" not in labels and "Restricted dataset" in labels and "report" in labels
    assert len(g["edges"]) == 2
    assert "customers" in {n["label"] for n in c.get("/api/lineage/graph", headers=sam).json()["nodes"]}


def test_dq_rules_scorecards_and_masked_samples(people: TestClient, ctx: PlatformContext) -> None:
    c = people
    ds = _customers(ctx)
    stu, ana = h(c, "stu"), h(c, "ana")
    r = c.post(
        "/api/quality/rules",
        headers=stu,
        json={"name": "contact_email", "dataset_id": ds, "column": "nope", "rule_type": "not_null"},
    )
    assert r.status_code == 422 and "nope" in r.text
    assert (
        c.post(
            "/api/quality/rules",
            headers=ana,
            json={"name": "x", "dataset_id": ds, "column": "id", "rule_type": "not_null"},
        ).status_code
        == 403
    )
    r = c.post(
        "/api/quality/rules",
        headers=stu,
        json={
            "name": "contact_format",
            "dataset_id": ds,
            "column": "contact",
            "rule_type": "regex",
            "params": {"pattern": r"^[a-z]+@x\.io$"},
            "severity": "critical",
            "run_on_load": True,
        },
    )
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["dimension"] == "validity"
    r2 = c.post(
        "/api/quality/rules", headers=stu, json={"name": "ids", "dataset_id": ds, "column": "id", "rule_type": "unique"}
    ).json()

    card = c.post(
        "/api/quality/scorecards",
        headers=stu,
        json={
            "name": "Customers",
            "rule_ids": [rule["id"], r2["id"]],
            "schedule": {"type": "cron", "cron": "0 6 * * *"},
        },
    )
    assert card.status_code == 201, card.text
    with ctx.metadata.session() as s:
        assert s.scalars(select(Schedule).where(Schedule.kind == "dq.run")).one().cron == "0 6 * * *"

    assert c.post(f"/api/quality/scorecards/{card.json()['id']}/run", headers=stu).json()["queued"]
    drain(ctx)
    results = c.get(f"/api/quality/rules/{rule['id']}/results", headers=stu).json()
    assert results[-1]["rows_failed"] == 2 and results[-1]["passed"] is False
    assert set(results[-1]["sample"]) == {"bob@y.io", "cy@z.io"}
    sc = c.get(f"/api/quality/scorecards/{card.json()['id']}", headers=ana).json()
    # (33.33 x 2 + 100) / 3
    assert sc["score"] == pytest.approx(55.55) and len(sc["history"]) == 1
    summary = c.get("/api/quality/summary", headers=ana).json()
    assert summary["failing"] == 1 and summary["alerts"][0]["target"] == "rule:contact_format"

    # Once contact is classified and masked, analysts see masked failing samples
    with ctx.metadata.session() as s:
        from dataplat.db.models import MaskingPolicy, Tag, TagAssignment

        s.add(Tag(name="secret", category="general"))
        s.flush()
        s.add(TagAssignment(tag="secret", dataset_id=s.get(Dataset, __import__("uuid").UUID(ds)).id, column="contact"))
        s.add(MaskingPolicy(name="secret", tag="secret", method="partial", exempt_roles=["steward"]))
    masked = c.get(f"/api/quality/rules/{rule['id']}/results", headers=ana).json()[-1]["sample"]
    assert set(masked) == {"****y.io", "***z.io"}
    assert c.get(f"/api/catalog/datasets/{ds}", headers=ana).json()["dq"]["failing"] == 1

    # run_on_load: new data queues the dataset's rules
    from dataplat.transform.triggers import notify_updated

    assert notify_updated(ctx, ["silver.customers"])["dq"] == ["silver.customers"]


class _Hook(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Hook.received.append({"auth": self.headers.get("Authorization"), "body": json.loads(body)})
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


def test_webhook_notifications_keep_secrets_in_the_vault(people: TestClient, ctx: PlatformContext) -> None:
    c = people
    adm = h(c, "eng")
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/hook?token=s3cr3t-token"
    try:
        body = {
            "name": "ops-hook",
            "type": "webhook",
            "min_severity": "serious",
            "secrets": {"url": url, "token": "bearer-xyz"},
        }
        assert c.post("/api/admin/notifications", headers=adm, json=body).status_code == 403  # admins only
        with ctx.metadata.session() as s:
            u = User(username="root2", password_hash=hash_password(PW))
            s.add(u)
            s.flush()
            s.add(UserRole(user_id=u.id, role="admin"))
        root = h(c, "root2")
        r = c.post("/api/admin/notifications", headers=root, json=body)
        assert r.status_code == 201, r.text
        assert "s3cr3t" not in r.text and r.json()["secret_fields"] == ["url", "token"]
        listed = c.get("/api/admin/notifications", headers=root).text
        assert "s3cr3t" not in listed and "bearer-xyz" not in listed
        with ctx.metadata.session() as s:
            cfg = s.scalars(select(NotificationChannel)).one().config
        assert cfg["url"].startswith("vault://") and "s3cr3t" not in json.dumps(cfg)

        assert not open_alert(ctx, "freshness", "silver.x", "stale", severity="warning") or True
        open_alert(ctx, "dq_degradation", "scorecard:c", "score dropped", severity="serious")
        drain(ctx)
        got = [m for m in _Hook.received if m["body"]["kind"] == "dq_degradation"]
        assert got and got[0]["auth"] == "Bearer bearer-xyz" and got[0]["body"]["message"] == "score dropped"
        assert not [m for m in _Hook.received if m["body"]["kind"] == "freshness"]  # below min severity

        t = c.post(f"/api/admin/notifications/{r.json()['id']}/test", headers=root).json()
        drain(ctx)
        assert c.get(f"/api/tasks/{t['task_id']}", headers=root).json()["result"]["ok"]
    finally:
        server.shutdown()
    with ctx.metadata.session() as s:
        assert s.scalars(select(JobRow).where(JobRow.kind == "alert.notify")).first().status == "succeeded"


def test_vault_audit_view(people: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataplat.api.routers import notifications

    log = tmp_path / "audit.log"
    entries = [
        {"type": "request", "time": "t0", "request": {"path": "kv/data/x"}},
        {
            "type": "response",
            "time": "t1",
            "auth": {"display_name": "approle", "policies": ["dataplat-worker"]},
            "request": {"operation": "read", "path": "kv/data/dataplat/platform/minio"},
            "response": {"data": {"root_password": "hmac-sha256:abc"}},
        },
    ]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n")
    monkeypatch.setattr(notifications, "VAULT_AUDIT_LOG", log)
    with people as c:
        with c.app.state.ctx.metadata.session() as s:
            u = User(username="root3", password_hash=hash_password(PW))
            s.add(u)
            s.flush()
            s.add(UserRole(user_id=u.id, role="admin"))
        assert c.get("/api/admin/vault-audit", headers=h(c, "stu")).status_code == 403
        out = c.get("/api/admin/vault-audit", headers=h(c, "root3")).json()
    assert out["available"] and out["entries"] == [
        {
            "time": "t1",
            "who": "approle",
            "policies": ["dataplat-worker"],
            "operation": "read",
            "path": "kv/data/dataplat/platform/minio",
            "remote_address": None,
            "error": None,
        }
    ]
