from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    service_account: str | None = None
    # Single-use, response-wrapped Vault token scoped to the service account's policy.
    vault_wrap_token: str | None = None
    result: dict[str, Any] | None = field(default=None)


@runtime_checkable
class JobQueue(Protocol):
    """Durable work queue. Local: Postgres `FOR UPDATE SKIP LOCKED`. Cloud: Airflow/Step Functions."""

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        run_after: datetime | None = None,
        dedupe_key: str | None = None,
        service_account: str | None = None,
        max_attempts: int = 3,
    ) -> int: ...
    def claim(self, worker_id: str, kinds: list[str] | None = None) -> Job | None: ...
    def complete(self, job_id: int, result: dict[str, Any] | None = None) -> None: ...
    def fail(self, job_id: int, error: str, retry: bool = True) -> None: ...
    def status(self, job_id: int) -> dict[str, Any]: ...


@runtime_checkable
class Orchestrator(Protocol):
    """Schedules work onto the queue. Local: APScheduler. Cloud: Airflow, Dagster."""

    def tick(self) -> int:
        """Enqueues every due schedule; returns how many jobs were enqueued."""
        ...
