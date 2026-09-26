"""JWT access tokens (EdDSA) signed by Vault transit.

The private signing key never leaves Vault: the API sends the signing input to
``transit/sign`` and verifies tokens locally with the public keys it fetches (and
caches) from ``transit/keys``. Rotating the key in Vault adds a new key id (kid);
older tokens stay verifiable until they expire.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from dataplat.adapters.vault_secrets import shared_vault
from dataplat.core.config import AuthConfig, PlatformConfig
from dataplat.core.ports.identity import Principal
from dataplat.core.ports.secrets import TokenSigner


class InvalidToken(Exception):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class VaultTransitSigner:
    algorithm = "EdDSA"

    def __init__(self, vault: Any, key_name: str, mount: str = "transit") -> None:
        self.vault = vault
        self.key_name = key_name
        self.mount = mount
        self._keys: dict[str, bytes] = {}
        self._keys_fetched = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> VaultTransitSigner:
        return cls(shared_vault(registry), config.auth.jwt_key, config.vault.transit_mount)

    def sign(self, message: bytes) -> tuple[bytes, str]:
        resp = self.vault.call(
            lambda c: c.secrets.transit.sign_data(
                name=self.key_name,
                hash_input=base64.b64encode(message).decode(),
                mount_point=self.mount,
            )
        )
        # "vault:v<version>:<base64 signature>"
        _, version, sig = resp["data"]["signature"].split(":", 2)
        return base64.b64decode(sig), f"{self.key_name}-{version}"

    def public_keys(self) -> dict[str, bytes]:
        with self._lock:
            if not self._keys or time.monotonic() - self._keys_fetched > 300:
                resp = self.vault.call(lambda c: c.secrets.transit.read_key(name=self.key_name, mount_point=self.mount))
                self._keys = {
                    f"{self.key_name}-v{version}": base64.b64decode(info["public_key"])
                    for version, info in resp["data"]["keys"].items()
                }
                self._keys_fetched = time.monotonic()
            return dict(self._keys)

    def refresh_keys(self) -> None:
        with self._lock:
            self._keys_fetched = 0.0


class LocalEd25519Signer:
    """In-process signer for unit tests only; never configured in platform.yaml."""

    algorithm = "EdDSA"

    def __init__(self) -> None:
        self._key = Ed25519PrivateKey.generate()
        self.kid = "local-v1"

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> LocalEd25519Signer:
        return cls()

    def sign(self, message: bytes) -> tuple[bytes, str]:
        return self._key.sign(message), self.kid

    def public_keys(self) -> dict[str, bytes]:
        raw = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {self.kid: raw}


class JwtIdentityProvider:
    """IdentityProvider for built-in users and service accounts."""

    def __init__(self, signer: TokenSigner, cfg: AuthConfig) -> None:
        self.signer = signer
        self.cfg = cfg

    def issue_access_token(self, principal: Principal) -> tuple[str, int]:
        now = int(time.time())
        ttl = self.cfg.access_token_ttl_seconds
        claims = {
            "iss": self.cfg.issuer,
            "aud": self.cfg.audience,
            "sub": principal.id,
            "name": principal.name,
            "kind": principal.kind,
            "roles": sorted(principal.roles),
            "iat": now,
            "nbf": now,
            "exp": now + ttl,
            "jti": uuid.uuid4().hex,
        }
        # kid depends on the signing key version, which we only learn after signing;
        # use the current newest public key id for the header.
        kid = max(self.signer.public_keys(), key=_kid_version)
        header = {"alg": self.signer.algorithm, "typ": "JWT", "kid": kid}
        signing_input = f"{b64url(json.dumps(header, separators=(',', ':')).encode())}." + b64url(
            json.dumps(claims, separators=(",", ":")).encode()
        )
        signature, signed_kid = self.signer.sign(signing_input.encode())
        if signed_kid != kid:
            # Key rotated between the two calls; reissue with the right header.
            refresh = getattr(self.signer, "refresh_keys", None)
            if refresh:
                refresh()
            return self.issue_access_token(principal)
        return f"{signing_input}.{b64url(signature)}", ttl

    def verify_access_token(self, token: str) -> Principal:
        try:
            header_b64, claims_b64, sig_b64 = token.split(".")
            header = json.loads(b64url_decode(header_b64))
            claims = json.loads(b64url_decode(claims_b64))
            signature = b64url_decode(sig_b64)
        except ValueError as e:
            raise InvalidToken("malformed token") from e

        if header.get("alg") != self.signer.algorithm:
            raise InvalidToken("unexpected algorithm")
        keys = self.signer.public_keys()
        kid = header.get("kid")
        if kid not in keys:
            refresh = getattr(self.signer, "refresh_keys", None)
            if refresh:
                refresh()
                keys = self.signer.public_keys()
        if kid not in keys:
            raise InvalidToken("unknown signing key")
        try:
            Ed25519PublicKey.from_public_bytes(keys[kid]).verify(signature, f"{header_b64}.{claims_b64}".encode())
        except InvalidSignature as e:
            raise InvalidToken("bad signature") from e

        now = time.time()
        if claims.get("iss") != self.cfg.issuer or claims.get("aud") != self.cfg.audience:
            raise InvalidToken("wrong issuer or audience")
        if not (claims.get("nbf", 0) - 30 <= now < claims.get("exp", 0)):
            raise InvalidToken("token expired or not yet valid")
        return Principal(
            id=claims["sub"],
            name=claims["name"],
            kind=claims.get("kind", "user"),
            roles=frozenset(claims.get("roles", [])),
        )


def _kid_version(kid: str) -> int:
    try:
        return int(kid.rsplit("-v", 1)[1])
    except (IndexError, ValueError):
        return 0
