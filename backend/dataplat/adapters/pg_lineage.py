"""LineageSink storing OpenLineage RunEvents in the metadata database."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from dataplat.core.config import PlatformConfig
from dataplat.core.ports.metadata import MetadataStore
from dataplat.db.models import LineageEvent
from dataplat.lineage.openlineage import validate_run_event


class PostgresLineageSink:
    def __init__(self, metadata: MetadataStore) -> None:
        self.metadata = metadata

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> PostgresLineageSink:
        return cls(registry.get("metadata_store"))

    def emit(self, event: dict[str, Any]) -> None:
        validate_run_event(event)
        with self.metadata.session() as s:
            s.add(
                LineageEvent(
                    event_time=datetime.fromisoformat(event["eventTime"]),
                    event_type=event["eventType"],
                    job_namespace=event["job"]["namespace"],
                    job_name=event["job"]["name"],
                    run_id=event["run"]["runId"],
                    event=event,
                )
            )

    def events(self, job_name: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.metadata.session() as s:
            q = select(LineageEvent).order_by(LineageEvent.event_time.desc()).limit(limit)
            if job_name:
                q = q.where(LineageEvent.job_name == job_name)
            return [row.event for row in s.scalars(q)]
