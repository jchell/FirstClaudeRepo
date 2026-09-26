"""Shared plumbing for everything that writes vault/silver/gold tables.

Run bookkeeping (transform_runs), catalog registration, and OpenLineage events with
column-level lineage (``columnLineage`` facets), so vault loads and model builds show
up in the Lineage Explorer the same way ingestion does.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from dataplat.catalog import service as catalog
from dataplat.core.context import PlatformContext
from dataplat.core.logging import redact
from dataplat.db.models import TransformRun
from dataplat.lineage.openlineage import dataset as ol_dataset
from dataplat.lineage.openlineage import run_event

log = logging.getLogger(__name__)

NAMESPACE = "dataplat"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Namespace of serving-DB replicas in lineage (dataset name: "<schema>.<table>").
SERVING_SCHEME = "serving://"

# column -> [(layer, dataset, column)]
ColumnLineage = dict[str, list[tuple[str, str, str]]]


@dataclass
class Output:
    layer: str
    name: str
    columns: ColumnLineage = field(default_factory=dict)
    rows: int = 0
    version: int | None = None
    dataset_id: uuid.UUID | None = None


@dataclass
class RunContext:
    run_id: uuid.UUID
    lineage_run: str
    started: float
    outputs: list[Output] = field(default_factory=list)
    inputs: list[tuple[str, str]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    rows: int = 0
    table_version: int | None = None


def error_message(e: Exception) -> str:
    return redact(_ANSI.sub("", f"{type(e).__name__}: {e}"))[:4000]


@contextmanager
def tracked_run(
    ctx: PlatformContext,
    kind: str,
    target: str,
    *,
    job_name: str,
    trigger: str = "manual",
    pipeline_id: uuid.UUID | None = None,
    parent_run_id: uuid.UUID | None = None,
    job_type: str = "TRANSFORM",
    sql: str | None = None,
):
    """Records a transform run and its START/COMPLETE/FAIL lineage events."""
    lineage_run = str(uuid.uuid4())
    with ctx.metadata.session() as s:
        run = TransformRun(
            kind=kind,
            target=target,
            trigger=trigger[:256],
            pipeline_id=pipeline_id,
            parent_run_id=parent_run_id,
            status="running",
            started_at=datetime.now(UTC),
            lineage_run_id=lineage_run,
        )
        s.add(run)
        s.flush()
        run_id = run.id
    rc = RunContext(run_id=run_id, lineage_run=lineage_run, started=time.monotonic())
    job_facets: dict[str, Any] = {
        "jobType": {"processingType": "BATCH", "integration": "DATAPLAT", "jobType": job_type}
    }
    _emit(ctx, run_event("START", NAMESPACE, job_name, run_id=lineage_run, run_facets=_run_facets(rc)))
    try:
        yield rc
    except Exception as e:
        msg = error_message(e)
        if getattr(e, "skip", False):  # e.g. an input has no data yet: not a failure
            _finish(ctx, rc, "skipped", str(e))
            raise
        _finish(ctx, rc, "failed", msg)
        _emit(
            ctx,
            run_event(
                "FAIL",
                NAMESPACE,
                job_name,
                run_id=lineage_run,
                run_facets={"errorMessage": {"message": msg[:1000], "programmingLanguage": "SQL"}},
            ),
        )
        raise
    if sql:
        job_facets["sql"] = {"query": sql}
    _finish(ctx, rc, "succeeded", None)
    if rc.outputs:
        lake = ctx.config.lake.model_dump()
        inputs = [ol_dataset(lake.get(layer, layer), name) for layer, name in dict.fromkeys(rc.inputs)]
        outputs = [
            ol_dataset(
                lake.get(o.layer) or f"{SERVING_SCHEME}{ctx.config.serving.database}",
                o.name,
                {
                    "columnLineage": {
                        "fields": {
                            col: {
                                "inputFields": [
                                    {"namespace": lake.get(layer, layer), "name": ds, "field": c}
                                    for layer, ds, c in srcs
                                ]
                            }
                            for col, srcs in o.columns.items()
                        }
                    },
                    "outputStatistics": {"rowCount": o.rows},
                    "dataplat_dataset": {
                        "layer": o.layer,
                        "dataset_id": str(o.dataset_id) if o.dataset_id else None,
                        "table_version": o.version,
                    },
                },
            )
            for o in rc.outputs
        ]
        _emit(
            ctx,
            run_event(
                "COMPLETE",
                NAMESPACE,
                job_name,
                run_id=lineage_run,
                inputs=inputs,
                outputs=outputs,
                run_facets=_run_facets(rc),
                job_facets=job_facets,
            ),
        )


def _run_facets(rc: RunContext) -> dict[str, Any]:
    return {"dataplat_run": {"transform_run_id": str(rc.run_id)}}


def _finish(ctx: PlatformContext, rc: RunContext, status: str, error: str | None) -> None:
    with ctx.metadata.session() as s:
        run = s.get(TransformRun, rc.run_id)
        run.status = status
        run.error = error
        run.finished_at = datetime.now(UTC)
        run.duration_ms = int((time.monotonic() - rc.started) * 1000)
        run.rows_written = rc.rows
        run.table_version = rc.table_version
        run.details = {
            **rc.details,
            "outputs": [{"dataset": f"{o.layer}.{o.name}", "rows": o.rows, "version": o.version} for o in rc.outputs],
        }


def register(ctx: PlatformContext, layer: str, name: str, run_id: uuid.UUID | None = None) -> uuid.UUID:
    """Registers/refreshes a lake table in the catalog; returns its dataset id."""
    uri = ctx.config.lake.uri(layer, name)
    tf = ctx.tables
    delta = tf.table(uri)
    with ctx.metadata.session() as s:
        ds, _ = catalog.register(
            s,
            layer=layer,
            name=name,
            uri=uri,
            schema=delta.to_pyarrow_dataset().schema,
            run_id=run_id,
            stats=tf.stats(uri),
            table_version=delta.version(),
        )
        return ds.id


def _emit(ctx: PlatformContext, event: dict[str, Any]) -> None:
    try:
        ctx.lineage.emit(event)
    except Exception:
        log.exception("lineage emit failed")
