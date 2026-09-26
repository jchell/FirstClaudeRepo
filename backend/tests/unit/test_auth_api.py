"""Auth + admin API against a real Postgres (DATAPLAT_TEST_DATABASE_URL)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from dataplat.api.app import create_app
from dataplat.api.ratelimit import SlidingWindowLimiter
from dataplat.api.routers import auth as auth_router
from dataplat.api.routers.auth import REFRESH_COOKIE
from dataplat.core.context import PlatformContext
from dataplat.db.models import AuditLog, User, UserRole
from dataplat.security.passwords import hash_password

ADMIN_PW = "correct horse battery"


@pytest.fixture
def client(ctx: PlatformContext, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(auth_router, "_login_limiter", SlidingWindowLimiter(1000, 60))
    with ctx.metadata.session() as s:
        admin = User(username="admin", password_hash=hash_password(ADMIN_PW))
        s.add(admin)
        s.flush()
        s.add(UserRole(user_id=admin.id, role="admin"))
    with TestClient(create_app(ctx)) as c:
        yield c


def _login(client: TestClient, username: str = "admin", password: str = ADMIN_PW):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _auth(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client).json()['access_token']}"}


def test_login_success_matches_console_contract(client: TestClient) -> None:
    r = _login(client, "  ADMIN ", ADMIN_PW)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "user"}
    assert body["token_type"] == "bearer" and body["expires_in"] == 900
    assert body["user"]["username"] == "admin" and body["user"]["roles"] == ["admin"]
    cookie = r.headers["set-cookie"]
    assert (
        REFRESH_COOKIE in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api/auth" in cookie
    )
    assert r.headers["cache-control"] == "no-store"


def test_wrong_password_and_unknown_user_look_identical(client: TestClient) -> None:
    a, b = _login(client, "admin", "wrong password!"), _login(client, "nobody", "wrong password!")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_lockout_after_repeated_failures_persists(client: TestClient, ctx: PlatformContext) -> None:
    for _ in range(ctx.config.auth.max_failed_logins):
        assert _login(client, "admin", "bad password 123").status_code == 401
    assert _login(client).status_code == 423  # locked even with the right password
    with ctx.metadata.session() as s:
        denied = s.query(AuditLog).filter_by(action="auth.login", outcome="denied").count()
    assert denied == ctx.config.auth.max_failed_logins + 1


def test_rate_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_router, "_login_limiter", SlidingWindowLimiter(2, 60))
    _login(client), _login(client)
    assert _login(client).status_code == 429


def test_refresh_rotates_and_detects_reuse(client: TestClient) -> None:
    _login(client)
    old = client.cookies.get(REFRESH_COOKIE)
    r = client.post("/api/auth/refresh")
    assert r.status_code == 200 and r.json()["access_token"]
    new = client.cookies.get(REFRESH_COOKIE)
    assert new and new != old

    # Replaying the rotated token revokes the whole family, including the new token.
    client.cookies.set(REFRESH_COOKIE, old, path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 401
    client.cookies.set(REFRESH_COOKIE, new, path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 401


def test_logout_revokes_refresh(client: TestClient) -> None:
    _login(client)
    token = client.cookies.get(REFRESH_COOKIE)
    assert client.post("/api/auth/logout").status_code == 204
    client.cookies.set(REFRESH_COOKIE, token, path="/api/auth")
    assert client.post("/api/auth/refresh").status_code == 401


def test_me_requires_valid_token(client: TestClient) -> None:
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert client.get("/api/auth/me", headers=_auth(client)).json()["username"] == "admin"


def test_admin_creates_user_with_role_and_rbac_applies(client: TestClient) -> None:
    h = _auth(client)
    r = client.post(
        "/api/admin/users",
        headers=h,
        json={"username": "Viv", "password": "viewer password 1", "roles": ["viewer"]},
    )
    assert r.status_code == 201, r.text
    assert "password" not in r.text and "viewer password" not in r.text
    assert (
        client.post(
            "/api/admin/users", headers=h, json={"username": "viv", "password": "viewer password 1"}
        ).status_code
        == 409
    )

    vh = {"Authorization": f"Bearer {_login(client, 'viv', 'viewer password 1').json()['access_token']}"}
    assert client.get("/api/admin/users", headers=vh).status_code == 403
    assert client.get("/api/admin/roles", headers=vh).status_code == 200


def test_weak_password_rejected(client: TestClient) -> None:
    r = client.post("/api/admin/users", headers=_auth(client), json={"username": "x1", "password": "short"})
    assert r.status_code == 422
    assert "short" not in r.text  # input values are not echoed back


def test_group_roles_flow_into_token(client: TestClient) -> None:
    h = _auth(client)
    client.post("/api/admin/users", headers=h, json={"username": "eve", "password": "engineer password", "roles": []})
    r = client.post(
        "/api/admin/groups", headers=h, json={"name": "Data Eng", "roles": ["engineer"], "members": ["eve"]}
    )
    assert r.status_code == 201, r.text
    assert _login(client, "eve", "engineer password").json()["user"]["roles"] == ["engineer"]


def test_deactivating_user_revokes_sessions(client: TestClient) -> None:
    h = _auth(client)
    uid = client.post("/api/admin/users", headers=h, json={"username": "tmp", "password": "temporary password"}).json()[
        "id"
    ]
    other = TestClient(client.app)
    other.post("/api/auth/login", json={"username": "tmp", "password": "temporary password"})
    assert client.patch(f"/api/admin/users/{uid}", headers=h, json={"is_active": False}).status_code == 200
    assert other.post("/api/auth/refresh").status_code == 401
    assert _login(client, "tmp", "temporary password").status_code == 401


def test_admin_cannot_demote_self(client: TestClient) -> None:
    h = _auth(client)
    me = client.get("/api/auth/me", headers=h).json()["id"]
    assert client.patch(f"/api/admin/users/{me}", headers=h, json={"roles": ["viewer"]}).status_code == 400


def test_service_account_secrets_are_write_only(client: TestClient, ctx: PlatformContext) -> None:
    h = _auth(client)
    r = client.post("/api/admin/service-accounts", headers=h, json={"name": "etl-sftp", "description": "SFTP loads"})
    assert r.status_code == 201, r.text
    assert r.json()["vault_policy"] == "sa-etl-sftp"
    assert r.json()["vault_path"] == "kv/dataplat/service-accounts/etl-sftp"

    r = client.put(
        "/api/admin/service-accounts/etl-sftp/secrets/sftp",
        headers=h,
        json={"values": {"password": "sftp-pa55word"}},
    )
    assert r.status_code == 200, r.text
    assert "sftp-pa55word" not in r.text
    assert r.json()["refs"] == ["vault://kv/dataplat/service-accounts/etl-sftp/sftp#password"]
    assert ctx.secrets.resolve(r.json()["refs"][0]) == "sftp-pa55word"

    # Rotation creates a new version.
    r = client.put(
        "/api/admin/service-accounts/etl-sftp/secrets/sftp", headers=h, json={"values": {"password": "rotated-pw"}}
    )
    assert r.json()["current_version"] == 2

    listing = client.get("/api/admin/service-accounts/etl-sftp/secrets", headers=h)
    assert listing.status_code == 200 and "rotated-pw" not in listing.text

    assert client.delete("/api/admin/service-accounts/etl-sftp", headers=h).status_code == 204
    assert ctx.secrets.list("service-accounts/etl-sftp") == []


def test_invalid_service_account_names(client: TestClient) -> None:
    h = _auth(client)
    for name in ["UPPER", "a", "../x", "has space", "x" * 70]:
        assert client.post("/api/admin/service-accounts", headers=h, json={"name": name}).status_code == 422


def test_settings_refuse_secrets(client: TestClient) -> None:
    h = _auth(client)
    assert client.put("/api/admin/settings/ui.theme", headers=h, json={"value": "dark"}).status_code == 200
    assert client.put("/api/admin/settings/smtp", headers=h, json={"value": "password=abc123"}).status_code == 422
    assert client.get("/api/admin/settings", headers=h).json() == {"ui.theme": "dark"}


def test_audit_log_records_admin_actions(client: TestClient) -> None:
    h = _auth(client)
    client.post("/api/admin/users", headers=h, json={"username": "aud", "password": "audited password"})
    actions = [e["action"] for e in client.get("/api/admin/audit", headers=h).json()]
    assert "user.create" in actions and "auth.login" in actions
