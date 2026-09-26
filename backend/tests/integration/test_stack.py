"""End-to-end checks of Phase 0 against the running stack."""

from __future__ import annotations

import time

import httpx
import pytest
from hvac.exceptions import Forbidden

from tests.integration.conftest import approle_client, compose, wait_for_job

pytestmark = pytest.mark.integration


def test_every_component_is_healthy(api) -> None:
    r = api.get("/api/health/components").json()
    assert r["ok"], r
    assert {c["name"] for c in r["components"]} == {
        "secret_store",
        "metadata_store",
        "object_store",
        "query_engine",
        "event_bus",
        "knowledge_graph",
    }


def test_job_runs_on_worker_and_emits_lineage(api, unique) -> None:
    job = api.post("/api/jobs", json={"kind": "platform.echo", "payload": {"hello": unique}}).json()
    done = wait_for_job(api, job["id"])
    assert done["status"] == "succeeded" and done["result"] == {"echo": {"hello": unique}}
    events = api.get("/api/lineage/events", params={"job_name": "platform.echo", "limit": 50}).json()
    run_ids = {e["run"]["runId"] for e in events if e["run"]["facets"].get("job_id", {}).get("id") == job["id"]}
    assert len(run_ids) == 1
    types = [e["eventType"] for e in events if e["run"]["runId"] in run_ids]
    assert sorted(types) == ["COMPLETE", "START"]


def test_healthcheck_job_reaches_every_adapter_from_the_worker(api) -> None:
    job = api.post("/api/jobs", json={"kind": "platform.healthcheck"}).json()
    done = wait_for_job(api, job["id"])
    assert done["status"] == "succeeded" and all(done["result"].values()), done


@pytest.fixture
def two_accounts(api, unique):
    a, b = f"it-a-{unique}", f"it-b-{unique}"
    for name, pw in ((a, f"alpha-{unique}-secret"), (b, f"bravo-{unique}-secret-longer")):
        assert api.post("/api/admin/service-accounts", json={"name": name}).status_code == 201
        r = api.put(f"/api/admin/service-accounts/{name}/secrets/db", json={"values": {"password": pw}})
        assert r.status_code == 200 and pw not in r.text
    yield a, b
    for name in (a, b):
        api.delete(f"/api/admin/service-accounts/{name}")


def _probe(api, account: str, ref: str) -> dict:
    job = api.post(
        "/api/jobs", json={"kind": "platform.secret_probe", "service_account": account, "payload": {"ref": ref}}
    )
    assert job.status_code == 201, job.text
    return wait_for_job(api, job.json()["id"])


def test_job_reads_only_its_own_service_account_secrets(api, two_accounts, unique) -> None:
    a, b = two_accounts
    own = _probe(api, a, f"vault://kv/dataplat/service-accounts/{a}/db#password")
    assert own["status"] == "succeeded" and own["result"] == {"readable": True, "length": len(f"alpha-{unique}-secret")}
    other = _probe(api, a, f"vault://kv/dataplat/service-accounts/{b}/db#password")
    assert other["result"] == {"readable": False, "reason": "forbidden"}


def test_rotation_is_picked_up_by_the_next_run(api, two_accounts) -> None:
    a, _ = two_accounts
    r = api.put(f"/api/admin/service-accounts/{a}/secrets/db", json={"values": {"password": "rotated-value-xyz!"}})
    assert r.json()["current_version"] == 2
    res = _probe(api, a, f"vault://kv/dataplat/service-accounts/{a}/db#password")
    assert res["result"] == {"readable": True, "length": len("rotated-value-xyz!")}


def test_worker_and_api_approles_cannot_read_service_account_secrets(two_accounts) -> None:
    a, _ = two_accounts
    for service in ("worker", "api", "scheduler", "stream-worker"):
        client = approle_client(service)
        with pytest.raises(Forbidden):
            client.secrets.kv.v2.read_secret_version(
                path=f"dataplat/service-accounts/{a}/db", mount_point="kv", raise_on_deleted_version=True
            )


def test_api_cannot_mint_service_account_tokens() -> None:
    with pytest.raises(Forbidden):
        approle_client("api").write("auth/token/create/sa-job", policies=["sa-anything"])


def test_tampered_service_account_policy_is_refused(api, two_accounts) -> None:
    a, _ = two_accounts
    # The API may manage sa-* policies; widen one as a compromised API might.
    approle_client("api").sys.create_or_update_acl_policy(
        name=f"sa-{a}", policy='path "kv/data/dataplat/*" { capabilities = ["read"] }'
    )
    job = api.post("/api/jobs", json={"kind": "platform.echo", "service_account": a}).json()
    done = wait_for_job(api, job["id"])
    assert done["status"] == "failed" and "modified outside dataplat" in done["last_error"]


def test_postgres_logins_are_dynamic_and_expire() -> None:
    client = approle_client("worker")
    creds = client.secrets.database.generate_credentials(name="dataplat-app")
    assert creds["lease_duration"] == 86400
    user = creds["data"]["username"]
    until = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-Atc",
        f"select rolvaliduntil from pg_roles where rolname = '{user}'",
    )
    assert until.strip(), "dynamic role has no expiry"
    # Services cannot revoke arbitrary leases...
    with pytest.raises(Forbidden):
        client.sys.revoke_lease(creds["lease_id"])
    # ...but when a service's token is revoked (or expires), Vault drops its logins.
    client.auth.token.revoke_self()
    time.sleep(2)
    gone = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-Atc",
        f"select count(*) from pg_roles where rolname = '{user}'",
    )
    assert gone.strip() == "0"


def test_connection_style_responses_only_carry_vault_references(api, two_accounts) -> None:
    a, _ = two_accounts
    listing = api.get(f"/api/admin/service-accounts/{a}/secrets").json()
    assert listing and all(
        set(item) == {"path", "current_version", "created_time", "updated_time", "versions", "refs"} for item in listing
    )


def test_seeded_secrets_never_leak(api, unique, admin_password) -> None:
    """Grep the metadata DB, container logs, lineage events and API responses for secrets."""
    name = f"it-leak-{unique}"
    secret = f"LeakCanary-{unique}-{unique[::-1]}"
    api.post("/api/admin/service-accounts", json={"name": name})
    api.put(f"/api/admin/service-accounts/{name}/secrets/sftp", json={"values": {"password": secret}})
    done = wait_for_job(
        api,
        api.post(
            "/api/jobs",
            json={
                "kind": "platform.secret_probe",
                "service_account": name,
                "payload": {"ref": f"vault://kv/dataplat/service-accounts/{name}/sftp#password"},
            },
        ).json()["id"],
    )
    assert done["result"]["readable"] is True
    # A failed login with the secret as the password must not echo it anywhere either.
    api.post("/api/auth/login", json={"username": "admin", "password": secret})

    from tests.integration.conftest import HOME

    minio_pw = (HOME / "minio" / "root_password").read_text().strip()
    needles = [secret, minio_pw, admin_password]

    haystacks = {
        "metadata dump": compose("exec", "-T", "postgres", "pg_dump", "-U", "postgres", "dataplat"),
        "container logs": compose("logs", "--no-color", "api", "worker", "scheduler", "stream-worker", "console-ui"),
        "lineage events": api.get("/api/lineage/events", params={"limit": 1000}).text,
        "audit log": api.get("/api/admin/audit", params={"limit": 1000}).text,
        "jobs": api.get("/api/jobs", params={"limit": 500}).text,
        "openapi": api.get("/api/openapi.json").text,
    }
    leaks = [(where, i) for where, text in haystacks.items() for i, n in enumerate(needles) if n in text]
    assert leaks == []
    api.delete(f"/api/admin/service-accounts/{name}")


def test_console_proxy_serves_api_same_origin() -> None:
    r = httpx.get("http://127.0.0.1:3000/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    page = httpx.get("http://127.0.0.1:3000/")
    assert "Content-Security-Policy" in page.headers and page.headers["X-Frame-Options"] == "DENY"
