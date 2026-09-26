from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class Event:
    topic: str
    key: str | None
    value: dict[str, Any]
    offset: int | None = None
    partition: int | None = None
    timestamp_ms: int | None = None


@runtime_checkable
class EventBus(Protocol):
    """Kafka-API event bus. Local: Redpanda. Cloud: MSK, Event Hubs, Confluent, Pub/Sub."""

    def ensure_topic(self, topic: str, partitions: int = 1) -> None: ...
    def publish(self, topic: str, value: dict[str, Any], key: str | None = None) -> None: ...
    def flush(self, timeout: float = 10) -> None: ...
    def consume(
        self, topics: list[str], group: str, timeout: float = 1.0, max_messages: int = 500
    ) -> Iterator[list[Event]]:
        """Yields micro-batches; offsets are committed when the caller calls ``commit``."""
        ...

    def commit(self) -> None: ...
    def health(self) -> bool: ...


@runtime_checkable
class ChangeCapture(Protocol):
    """Log-based CDC. Local: Debezium on Kafka Connect. Cloud: DMS, ADF CDC, Fivetran."""

    def create_connector(self, name: str, config: dict[str, Any]) -> None: ...
    def delete_connector(self, name: str) -> None: ...
    def status(self, name: str) -> dict[str, Any]: ...
    def pause(self, name: str) -> None: ...
    def resume(self, name: str) -> None: ...


@runtime_checkable
class StreamProcessor(Protocol):
    """Micro-batch stream consumers. Local: in-house Python. Cloud: Spark, Flink, DLT."""

    def run(self, stream_id: str, handler: Callable[[list[Event]], None]) -> None: ...
    def stop(self, stream_id: str) -> None: ...
    def metrics(self, stream_id: str) -> dict[str, Any]: ...
