from __future__ import annotations

import json
import time

import pytest

from dataplat.adapters.jwt_identity import (
    InvalidToken,
    JwtIdentityProvider,
    LocalEd25519Signer,
    b64url,
    b64url_decode,
)
from dataplat.core.config import AuthConfig
from dataplat.core.ports.identity import Principal

P = Principal(id="u1", name="ann", kind="user", roles=frozenset({"engineer"}))


@pytest.fixture
def idp() -> JwtIdentityProvider:
    return JwtIdentityProvider(LocalEd25519Signer(), AuthConfig())


def test_roundtrip(idp: JwtIdentityProvider) -> None:
    token, ttl = idp.issue_access_token(P)
    assert ttl == 900
    assert idp.verify_access_token(token) == P


def _tamper(token: str, **changes: object) -> str:
    h, c, s = token.split(".")
    claims = json.loads(b64url_decode(c))
    claims.update(changes)
    return ".".join([h, b64url(json.dumps(claims).encode()), s])


def test_tampered_claims_rejected(idp: JwtIdentityProvider) -> None:
    token, _ = idp.issue_access_token(P)
    with pytest.raises(InvalidToken, match="signature"):
        idp.verify_access_token(_tamper(token, roles=["admin"]))


def test_other_key_rejected(idp: JwtIdentityProvider) -> None:
    other = JwtIdentityProvider(LocalEd25519Signer(), AuthConfig())
    token, _ = other.issue_access_token(P)
    with pytest.raises(InvalidToken):
        idp.verify_access_token(token)


def test_alg_none_rejected(idp: JwtIdentityProvider) -> None:
    token, _ = idp.issue_access_token(P)
    _, c, _ = token.split(".")
    forged = b64url(json.dumps({"alg": "none", "kid": "local-v1"}).encode()) + "." + c + "."
    with pytest.raises(InvalidToken):
        idp.verify_access_token(forged)


def test_expired_rejected(monkeypatch: pytest.MonkeyPatch, idp: JwtIdentityProvider) -> None:
    token, _ = idp.issue_access_token(P)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 901)
    with pytest.raises(InvalidToken, match="expired"):
        idp.verify_access_token(token)


def test_wrong_audience_rejected() -> None:
    signer = LocalEd25519Signer()
    token, _ = JwtIdentityProvider(signer, AuthConfig(audience="other")).issue_access_token(P)
    with pytest.raises(InvalidToken, match="audience"):
        JwtIdentityProvider(signer, AuthConfig()).verify_access_token(token)


@pytest.mark.parametrize("junk", ["", "a.b", "a.b.c", "x" * 50])
def test_malformed_rejected(idp: JwtIdentityProvider, junk: str) -> None:
    with pytest.raises(InvalidToken):
        idp.verify_access_token(junk)
