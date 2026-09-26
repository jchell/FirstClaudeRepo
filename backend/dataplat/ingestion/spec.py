"""The ingestion JobSpec: what the ETL wizard saves (versioned on every change)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field, model_validator

from dataplat.connectors.base import JobMode
from dataplat.vault.model import RawVaultSpec

DATASET_NAME = r"^[a-z][a-z0-9_]{1,62}$"


class SourceSpec(BaseModel):
    object: str | None = Field(default=None, description="Table, collection, topic or endpoint")
    query: str | None = Field(default=None, description="SQL instead of a table")
    path_template: str | None = Field(default=None, description="Files, e.g. /in/orders_{yyyyMMdd}*.csv")
    format: Literal["csv", "json", "jsonl", "parquet"] | None = None
    format_options: dict[str, Any] = {}
    options: dict[str, Any] = {}


class TargetSpec(BaseModel):
    layer: Literal["bronze"] = "bronze"
    dataset: str = Field(pattern=DATASET_NAME)


class ScheduleSpec(BaseModel):
    # file_arrival: poll the source every poll_seconds and run when new files appear.
    type: Literal["none", "cron", "interval", "file_arrival"] = "none"
    cron: str | None = None
    interval_seconds: int | None = Field(default=None, ge=10)
    poll_seconds: int | None = Field(default=None, ge=10)

    @model_validator(mode="after")
    def _check(self) -> ScheduleSpec:
        if self.type == "cron":
            if not self.cron:
                raise ValueError("cron schedules need a cron expression")
            CronTrigger.from_crontab(self.cron)  # raises ValueError on bad syntax
        if self.type == "interval" and not self.interval_seconds:
            raise ValueError("interval schedules need interval_seconds")
        if self.type == "file_arrival" and not self.poll_seconds:
            self.poll_seconds = 30
        return self


class StreamSpec(BaseModel):
    """Continuous modes: CDC replication and event streams."""

    # changelog: append every change with _op/_source_ts/_offset (full history).
    # mirror: keep a current-state copy, applying updates and deletes by primary key.
    write_mode: Literal["changelog", "mirror"] = "changelog"
    snapshot: Literal["initial", "never"] = "initial"  # CDC: copy existing rows first
    # Micro-batches close at whichever comes first.
    max_records: int = Field(default=1000, ge=1, le=100_000)
    max_seconds: float = Field(default=5, ge=0.5, le=300)


class JobSpec(BaseModel):
    source: SourceSpec
    load_mode: JobMode = "full"
    watermark_column: str | None = None
    target: TargetSpec
    schedule: ScheduleSpec = ScheduleSpec()
    stream: StreamSpec = StreamSpec()
    # "Add to Raw Vault": hub/satellite/link mappings created for the target dataset.
    raw_vault: RawVaultSpec | None = None
    # Keep a cleaned silver copy (audit columns dropped), rebuilt whenever bronze changes.
    promote_to_silver: bool = False


CDC_TYPES = {"postgres", "mysql", "sqlserver", "mongodb"}
STREAM_TYPES = {"kafka", "webhook"}


def is_continuous(spec: JobSpec | dict[str, Any]) -> bool:
    mode = spec.load_mode if isinstance(spec, JobSpec) else spec.get("load_mode")
    return mode in ("cdc", "stream")


def validate_for_category(spec: JobSpec, category: str, conn_type: str | None = None) -> None:
    src = spec.source
    if spec.load_mode == "cdc":
        if conn_type not in CDC_TYPES:
            raise ValueError(f"real-time replication (CDC) works with {', '.join(sorted(CDC_TYPES))} connections")
        if not src.object:
            raise ValueError("CDC replicates one table or collection: choose it")
        if spec.schedule.type != "none":
            raise ValueError("CDC runs continuously; it has no schedule")
        return
    if spec.load_mode == "stream":
        if conn_type not in STREAM_TYPES:
            raise ValueError("continuous streams read Kafka topics or webhook streams")
        if conn_type == "kafka" and not src.object:
            raise ValueError("choose the topic to stream")
        if spec.stream.write_mode == "mirror":
            raise ValueError("event streams are append-only (changelog)")
        if spec.schedule.type != "none":
            raise ValueError("streams run continuously; they have no schedule")
        return
    if conn_type == "webhook":
        raise ValueError("webhook streams are continuous: use the stream load mode")
    if spec.schedule.type == "file_arrival" and category != "file":
        raise ValueError("file-arrival triggers work with file sources")
    if category == "file":
        if not src.path_template:
            raise ValueError("file sources need a path template")
    elif category == "database":
        if not (src.object or src.query):
            raise ValueError("choose a table or write a query")
    elif not src.object:
        raise ValueError("choose what to read")
    if spec.load_mode == "incremental" and category != "file" and category != "event" and not spec.watermark_column:
        raise ValueError("incremental loads need a watermark column")


# Watermarks are stored as JSON; keep their types across the round trip.
def encode_watermark(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    return {"type": "json", "value": value}


def decode_watermark(stored: Any) -> Any:
    if not stored:
        return None
    kind, value = stored.get("type"), stored.get("value")
    if kind == "datetime":
        return datetime.fromisoformat(value)
    if kind == "date":
        return date.fromisoformat(value)
    if kind == "decimal":
        return Decimal(value)
    return value
