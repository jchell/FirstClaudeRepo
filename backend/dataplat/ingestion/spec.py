"""The ingestion JobSpec: what the ETL wizard saves (versioned on every change)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field, field_validator, model_validator

from dataplat.connectors.base import LoadMode

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
    type: Literal["none", "cron", "interval"] = "none"
    cron: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60)

    @model_validator(mode="after")
    def _check(self) -> ScheduleSpec:
        if self.type == "cron":
            if not self.cron:
                raise ValueError("cron schedules need a cron expression")
            CronTrigger.from_crontab(self.cron)  # raises ValueError on bad syntax
        if self.type == "interval" and not self.interval_seconds:
            raise ValueError("interval schedules need interval_seconds")
        return self


class JobSpec(BaseModel):
    source: SourceSpec
    load_mode: LoadMode = "full"
    watermark_column: str | None = None
    target: TargetSpec
    schedule: ScheduleSpec = ScheduleSpec()
    # Phase 2 options, accepted now so specs stay forward compatible.
    raw_vault: dict[str, Any] | None = None
    promote_to_silver: bool = False

    @field_validator("promote_to_silver")
    @classmethod
    def _phase2(cls, v: bool) -> bool:
        if v:
            raise ValueError("promotion to silver arrives in Phase 2")
        return v


def validate_for_category(spec: JobSpec, category: str) -> None:
    src = spec.source
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
