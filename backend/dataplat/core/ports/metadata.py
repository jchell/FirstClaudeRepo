from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


@runtime_checkable
class MetadataStore(Protocol):
    """Platform metadata (users, jobs, catalog...). Local: Postgres. Cloud: RDS/Azure PG."""

    engine: Engine

    def session(self) -> AbstractContextManager[Session]: ...
    def health(self) -> bool: ...


@runtime_checkable
class LineageSink(Protocol):
    """Receives OpenLineage RunEvents. Local: Postgres store. Cloud: Marquez, DataHub, Purview."""

    def emit(self, event: dict[str, Any]) -> None: ...
    def events(self, job_name: str | None = None, limit: int = 100) -> list[dict[str, Any]]: ...
