"""Contract suites against in-memory and Postgres adapters (no compose stack needed)."""

from __future__ import annotations

from dataplat.adapters.duckdb_engine import DuckDBQueryEngine
from dataplat.adapters.memory import InMemoryEventBus, InMemoryLineageSink, InMemoryObjectStore, InMemorySecretStore
from dataplat.adapters.pg_lineage import PostgresLineageSink
from dataplat.core.ports import QueryEngine
from tests.contracts.suites import (
    event_bus_contract,
    lineage_sink_contract,
    object_store_contract,
    secret_store_contract,
)


def test_memory_secret_store() -> None:
    secret_store_contract(InMemorySecretStore())


def test_memory_object_store() -> None:
    object_store_contract(InMemoryObjectStore())


def test_memory_event_bus() -> None:
    event_bus_contract(InMemoryEventBus())


def test_memory_lineage_sink() -> None:
    lineage_sink_contract(InMemoryLineageSink())


def test_postgres_lineage_sink(metadata_store) -> None:
    lineage_sink_contract(PostgresLineageSink(metadata_store))


def test_duckdb_query_engine() -> None:
    engine = DuckDBQueryEngine()
    assert isinstance(engine, QueryEngine)
    t = engine.query("select ? as a, 'x' as b", [41 + 1])
    assert t.to_pylist() == [{"a": 42, "b": "x"}]
    assert engine.health()
