from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from dataplat.adapters.jwt_identity import LocalEd25519Signer
from dataplat.adapters.memory import (
    InMemoryEventBus,
    InMemoryObjectStore,
    InMemorySecretStore,
)
from dataplat.adapters.pg_lineage import PostgresLineageSink
from dataplat.adapters.pg_metadata import PostgresMetadataStore
from dataplat.adapters.pg_queue import PostgresJobQueue
from dataplat.core.config import load_config
from dataplat.core.context import PlatformContext
from dataplat.db.models import DEFAULT_ROLES, Base, Role

TEST_DB = os.environ.get("DATAPLAT_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """A throwaway Postgres database for the test session.

    Set DATAPLAT_TEST_DATABASE_URL to a server where the user may create databases,
    e.g. postgresql+psycopg://postgres:test@localhost:55432/postgres
    """
    if not TEST_DB:
        pytest.skip("DATAPLAT_TEST_DATABASE_URL not set")
    admin = create_engine(TEST_DB, isolation_level="AUTOCOMMIT")
    name = f"dataplat_test_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(TEST_DB).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def metadata_store(pg_url: str) -> Iterator[PostgresMetadataStore]:
    engine = create_engine(pg_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    store = PostgresMetadataStore(engine)
    with store.session() as s:
        s.add_all(Role(name=k, description=v) for k, v in DEFAULT_ROLES.items())
    yield store
    engine.dispose()


class FakeServiceAccountVault:
    def __init__(self) -> None:
        self.policies: set[str] = set()
        self.minted: list[str] = []

    def create_policy(self, name: str) -> str:
        self.policies.add(f"sa-{name}")
        return f"sa-{name}"

    def delete_policy(self, name: str) -> None:
        self.policies.discard(f"sa-{name}")

    def mint_wrapped_token(self, name: str, **_: object) -> str:
        self.minted.append(name)
        return f"wrap-{name}-{len(self.minted)}"


@pytest.fixture
def lake(tmp_path) -> dict[str, str]:
    roots = {layer: str(tmp_path / "lake" / layer) for layer in ("bronze", "silver", "gold")}
    return roots


@pytest.fixture
def ctx(metadata_store: PostgresMetadataStore, lake: dict[str, str]) -> Iterator[PlatformContext]:
    from dataplat.adapters.delta_format import DeltaTableFormat
    from dataplat.adapters.duckdb_engine import DuckDBQueryEngine
    from dataplat.core.config import LakeConfig

    config = load_config().model_copy(update={"lake": LakeConfig(**lake)})
    overrides: dict[str, object] = {
        "secret_store": InMemorySecretStore(),
        "object_store": InMemoryObjectStore(),
        "event_bus": InMemoryEventBus(),
        "lineage_sink": PostgresLineageSink(metadata_store),
        "token_signer": LocalEd25519Signer(),
        "table_format": DeltaTableFormat(),
        "query_engine": DuckDBQueryEngine(),
        "metadata_store": metadata_store,
        "_sa_vault": FakeServiceAccountVault(),
    }
    c = PlatformContext(config, overrides)
    c.registry._instances["job_queue"] = PostgresJobQueue(metadata_store)
    yield c
