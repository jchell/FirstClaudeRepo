"""Integration tests against the running compose stack (make up / tasks.ps1 up).

    DATAPLAT_ADMIN_PASSWORD=... pytest -m integration

They reach the stack on its 127.0.0.1 ports and read bootstrap material (AppRole
credentials, MinIO root) from DATAPLAT_HOME, exactly as an operator on the laptop could.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import hvac
import pytest

API = os.environ.get("DATAPLAT_API_URL", "http://127.0.0.1:8000")
VAULT = os.environ.get("DATAPLAT_VAULT_URL", "http://127.0.0.1:8200")
HOME = Path(os.environ.get("DATAPLAT_HOME", Path.home() / ".dataplat"))
REPO = Path(__file__).resolve().parents[3]


def approle_client(service: str) -> hvac.Client:
    d = HOME / "approle" / service
    client = hvac.Client(url=VAULT)
    client.auth.approle.login(
        role_id=(d / "role_id").read_text().strip(), secret_id=(d / "secret_id").read_text().strip()
    )
    return client


@pytest.fixture(scope="session")
def admin_password() -> str:
    pw = os.environ.get("DATAPLAT_ADMIN_PASSWORD")
    if not pw:
        pytest.skip("DATAPLAT_ADMIN_PASSWORD not set")
    return pw


@pytest.fixture(scope="session")
def api(admin_password: str) -> Iterator[httpx.Client]:
    try:
        httpx.get(f"{API}/api/health", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip(f"platform API not reachable at {API}")
    with httpx.Client(base_url=API, timeout=30) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": admin_password})
        r.raise_for_status()
        c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        yield c


@pytest.fixture
def unique() -> str:
    return uuid.uuid4().hex[:6]


def wait_for_job(api: httpx.Client, job_id: int, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = api.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(1)
    raise TimeoutError(f"job {job_id} still {job['status']} after {timeout}s")


def compose(*args: str) -> str:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "DATAPLAT_HOME": str(HOME)},
    ).stdout
