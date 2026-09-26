"""Minimal OpenLineage RunEvent builder/validator (https://openlineage.io/spec)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

PRODUCER = "https://github.com/jchell/FirstClaudeRepo/dataplat"
SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/definitions/RunEvent"
EVENT_TYPES = {"START", "RUNNING", "COMPLETE", "ABORT", "FAIL", "OTHER"}


def dataset(namespace: str, name: str, facets: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"namespace": namespace, "name": name, "facets": facets or {}}


def run_event(
    event_type: str,
    job_namespace: str,
    job_name: str,
    run_id: str | None = None,
    inputs: list[dict[str, Any]] | None = None,
    outputs: list[dict[str, Any]] | None = None,
    run_facets: dict[str, Any] | None = None,
    job_facets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "eventType": event_type,
        "eventTime": datetime.now(UTC).isoformat(),
        "run": {"runId": run_id or str(uuid.uuid4()), "facets": run_facets or {}},
        "job": {"namespace": job_namespace, "name": job_name, "facets": job_facets or {}},
        "inputs": inputs or [],
        "outputs": outputs or [],
        "producer": PRODUCER,
        "schemaURL": SCHEMA_URL,
    }
    validate_run_event(event)
    return event


def validate_run_event(event: dict[str, Any]) -> None:
    try:
        if event["eventType"] not in EVENT_TYPES:
            raise ValueError(f"bad eventType {event['eventType']!r}")
        datetime.fromisoformat(event["eventTime"])
        uuid.UUID(event["run"]["runId"])
        for k in ("namespace", "name"):
            if not event["job"][k]:
                raise ValueError(f"job.{k} is required")
        for ds in [*event.get("inputs", []), *event.get("outputs", [])]:
            if not ds.get("namespace") or not ds.get("name"):
                raise ValueError("datasets need namespace and name")
    except (KeyError, TypeError) as e:
        raise ValueError(f"not an OpenLineage RunEvent: missing {e}") from e
