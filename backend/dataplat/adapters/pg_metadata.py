"""MetadataStore on Postgres, connecting with dynamic credentials from Vault.

Every process gets its own short-lived Postgres login from Vault's database secrets
engine. New connections always use the current credentials; they are re-issued at
two-thirds of the lease so pooled connections recycle long before Vault revokes the
old login. The dynamic role is a member of ``dataplat_owner`` and switches to it on
login, so tables stay owned by that group role, not by any expiring user.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from dataplat.adapters.vault_secrets import shared_vault
from dataplat.core.config import PlatformConfig

log = logging.getLogger(__name__)


class VaultDbCredentials:
    def __init__(self, vault: Any, role: str, mount: str = "database") -> None:
        self.vault = vault
        self.role = role
        self.mount = mount
        self._creds: tuple[str, str] | None = None
        self._renew_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> tuple[str, str]:
        with self._lock:
            if self._creds is None or time.monotonic() >= self._renew_at:
                resp = self.vault.call(
                    lambda c: c.secrets.database.generate_credentials(name=self.role, mount_point=self.mount)
                )
                self._creds = (resp["data"]["username"], resp["data"]["password"])
                self._renew_at = time.monotonic() + resp["lease_duration"] * 2 / 3
                log.info("issued dynamic postgres credentials (lease %ss)", resp["lease_duration"])
            return self._creds


class PostgresMetadataStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> PostgresMetadataStore:
        # Test-only escape hatch: integration tests against a throwaway database.
        if url := os.environ.get("DATAPLAT_TEST_DATABASE_URL"):
            return cls(create_engine(url, pool_pre_ping=True))

        pg = config.postgres
        creds = VaultDbCredentials(shared_vault(registry), pg.vault_role, config.vault.database_mount)
        engine = create_engine(
            f"postgresql+psycopg://{pg.host}:{pg.port}/{pg.database}",
            pool_pre_ping=True,
            pool_recycle=1800,
        )

        @event.listens_for(engine, "do_connect")
        def _inject_credentials(dialect: Any, conn_rec: Any, cargs: Any, cparams: dict[str, Any]) -> None:
            cparams["user"], cparams["password"] = creds.get()

        return cls(engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._sessions() as s:
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise

    def health(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("select 1"))
            return True
        except Exception:
            return False

    def close(self) -> None:
        self.engine.dispose()
