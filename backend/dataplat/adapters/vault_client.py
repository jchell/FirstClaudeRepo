"""Vault client shared by the Vault-backed adapters.

Each platform service logs in with its own AppRole. The role_id/secret_id files are
mounted by docker compose from outside the repo (see scripts/ and bootstrap/vault_init).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import hvac
from hvac.exceptions import Forbidden, InvalidRequest

from dataplat.core.config import VaultConfig

log = logging.getLogger(__name__)
T = TypeVar("T")


class VaultClient:
    def __init__(
        self,
        addr: str,
        *,
        role_id: str | None = None,
        secret_id: str | None = None,
        token: str | None = None,
    ) -> None:
        self.addr = addr
        self._role_id = role_id
        self._secret_id = secret_id
        self._lock = threading.Lock()
        self._expires_at = float("inf")
        self.client = hvac.Client(url=addr, token=token)
        if token is None:
            self._login()

    @classmethod
    def from_config(cls, cfg: VaultConfig) -> VaultClient:
        approle = Path(cfg.approle_dir)
        return cls(
            cfg.addr,
            role_id=(approle / "role_id").read_text().strip(),
            secret_id=(approle / "secret_id").read_text().strip(),
        )

    def _login(self) -> None:
        if not (self._role_id and self._secret_id):
            raise RuntimeError("Vault token expired and no AppRole credentials to log in again")
        resp = self.client.auth.approle.login(role_id=self._role_id, secret_id=self._secret_id)
        ttl = resp["auth"]["lease_duration"]
        # Log in again at 80% of the token's lifetime.
        self._expires_at = time.monotonic() + ttl * 0.8 if ttl else float("inf")
        log.info("vault approle login ok (ttl=%ss)", ttl)

    def call(self, fn: Callable[[hvac.Client], T]) -> T:
        """Runs ``fn`` with a live token, logging in again if the token expired."""
        with self._lock:
            if time.monotonic() >= self._expires_at:
                self._login()
        try:
            return fn(self.client)
        except Forbidden:
            if not self._role_id:
                raise
            with self._lock:
                self._login()
            return fn(self.client)

    @classmethod
    def unwrap(cls, addr: str, wrap_token: str) -> VaultClient:
        """Unwraps a single-use wrapped token and returns a client that uses it."""
        try:
            resp = hvac.Client(url=addr, token=wrap_token).sys.unwrap()
        except (Forbidden, InvalidRequest) as e:
            raise PermissionError("wrapped token is invalid, expired or was already used") from e
        return cls(addr, token=resp["auth"]["client_token"])

    def health(self) -> dict[str, Any]:
        return self.client.sys.read_health_status(method="GET")
