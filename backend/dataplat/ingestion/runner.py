"""Runs one ingestion: source -> Arrow -> Delta bronze -> catalog -> profile -> lineage.

Steps, all under one batch id:
  1. read the source through its connector (as the job's service account)
  2. add audit columns (_load_ts, _source, _batch_id, _file) and write the batch to
     the bronze Delta table in a single commit (overwrite for full loads, append
     otherwise, additive schema changes merged)
  3. register/refresh the dataset in the catalog and record schema drift
  4. profile the table
  5. save the new watermark / ingested files, run metrics, and emit OpenLineage
     START/COMPLETE (or FAIL) events plus a run event on the bus
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.types as pat
from sqlalchemy import select

from dataplat.catalog import service as catalog
from dataplat.catalog.service import AUDIT_COLUMNS
from dataplat.connectors.base import ConnectorError, ReadContext, ReadRequest
from dataplat.connectors.registry import get_connector_class
from dataplat.core.context import PlatformContext
from dataplat.core.logging import redact
from dataplat.core.ports.secrets import SecretStore
from dataplat.db.models import (
    Connection,
    DatasetProfile,
    IngestedFile,
    IngestionJob,
    IngestionRun,
    IngestionState,
)
from dataplat.ingestion.spec import JobSpec, decode_watermark, encode_watermark
from dataplat.lineage.openlineage import dataset as ol_dataset
from dataplat.lineage.openlineage import run_event
from dataplat.quality.profiler import profile

log = logging.getLogger(__name__)

NAMESPACE = "dataplat"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
RUNS_TOPIC = "dataplat.runs"


class IngestionError(Exception):
    pass


def delta_compatible(table: pa.Table) -> pa.Table:
    """Casts Arrow types Delta can't store (null, unsigned, time, duration, dictionary)."""
    fields, arrays = [], []
    for field, col in zip(table.schema, table.columns, strict=True):
        t = field.type
        if pat.is_dictionary(t):
            col, t = col.cast(t.value_type), t.value_type
        if pat.is_null(t):
            col, t = col.cast(pa.string()), pa.string()
        elif pat.is_unsigned_integer(t):
            target = pa.int64() if t.bit_width >= 32 else pa.int32()
            col, t = col.cast(target), target
        elif pat.is_time(t) or pat.is_duration(t) or pat.is_interval(t):
            col, t = pc.cast(col, pa.string()), pa.string()
        elif pat.is_large_string(t):
            col, t = col.cast(pa.string()), pa.string()
        fields.append(pa.field(field.name, t, nullable=True))
        arrays.append(col)
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def with_audit_columns(table: pa.Table, load_ts: datetime, source: str, batch_id: str, file: str | None) -> pa.Table:
    n = table.num_rows
    for name in AUDIT_COLUMNS:
        if name in table.column_names:
            table = table.drop_columns([name])
    return (
        table.append_column("_load_ts", pa.array([load_ts] * n, pa.timestamp("us", tz="UTC")))
        .append_column("_source", pa.array([source] * n, pa.string()))
        .append_column("_batch_id", pa.array([batch_id] * n, pa.string()))
        .append_column("_file", pa.array([file] * n, pa.string()))
    )


class IngestionRunner:
    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx

    def run(
        self,
        job_id: uuid.UUID,
        secrets: SecretStore | None,
        *,
        queue_job_id: int | None = None,
        trigger: str = "manual",
    ) -> dict[str, Any]:
        started = time.monotonic()
        load_ts = datetime.now(UTC)
        batch_id = uuid.uuid4().hex

        with self.ctx.metadata.session() as s:
            job = s.get(IngestionJob, job_id)
            if job is None:
                raise IngestionError(f"ingestion job {job_id} not found")
            conn = s.get(Connection, job.connection_id)
            spec = JobSpec.model_validate(job.spec)
            if spec.load_mode in ("cdc", "stream"):
                raise IngestionError(f"{job.name} runs continuously on the stream worker")
            state = s.get(IngestionState, job.id)
            seen_files = {
                f.path: f.fingerprint for f in s.scalars(select(IngestedFile).where(IngestedFile.job_id == job.id))
            }
            run = IngestionRun(
                job_id=job.id,
                job_version=job.version,
                queue_job_id=queue_job_id,
                batch_id=batch_id,
                trigger=trigger,
                status="running",
                started_at=load_ts,
            )
            s.add(run)
            s.flush()
            run_id, job_name, job_version = run.id, job.name, job.version
            conn_type, conn_config = conn.type, dict(conn.config)

        cls = get_connector_class(conn_type)
        target_uri = self.ctx.config.lake.uri(spec.target.layer, spec.target.dataset)
        out_namespace = target_uri.rsplit("/", 1)[0]
        request = ReadRequest(
            object=spec.source.object,
            query=spec.source.query,
            path_template=spec.source.path_template,
            format=spec.source.format,
            format_options=spec.source.format_options,
            load_mode=spec.load_mode,
            watermark_column=spec.watermark_column,
            last_watermark=decode_watermark(state.watermark) if state else None,
            seen_files=seen_files,
            run_date=load_ts,
            options={**spec.source.options, "job_id": str(job_id)},
        )

        start_event = run_event(
            "START",
            NAMESPACE,
            f"ingest.{job_name}",
            run_facets={
                "dataplat_run": {
                    "run_id": str(run_id),
                    "batch_id": batch_id,
                    "job_version": job_version,
                    "trigger": trigger,
                }
            },
        )
        lineage_run = start_event["run"]["runId"]
        self._emit(start_event)

        with cls(conn_config, secrets) as connector:
            rctx = ReadContext(namespace=connector.namespace())
            try:
                tables = [
                    with_audit_columns(
                        pa.table(b.data) if isinstance(b.data, pa.RecordBatch) else b.data,
                        load_ts,
                        b.source,
                        batch_id,
                        b.file,
                    )
                    for b in connector.read(request, rctx)
                    if b.data.num_rows
                ]
                result = self._write_and_register(
                    spec, target_uri, tables, run_id, job_id, rctx, lineage_run, out_namespace, job_name
                )
            except Exception as e:
                self._fail(run_id, e, started, rctx, lineage_run, job_name, out_namespace)
                raise

        # Source offsets (Kafka) are committed only once the data is safely in Delta.
        if commit := rctx.extra.get("commit"):
            commit()

        with self.ctx.metadata.session() as s:
            if request.load_mode == "incremental" and rctx.new_watermark is not None:
                st = s.get(IngestionState, job_id) or IngestionState(job_id=job_id)
                st.watermark = encode_watermark(rctx.new_watermark)
                s.merge(st)
            for f in rctx.files:
                existing = s.scalars(
                    select(IngestedFile).where(IngestedFile.job_id == job_id, IngestedFile.path == f.path)
                ).first()
                row = existing or IngestedFile(job_id=job_id, path=f.path)
                row.fingerprint, row.checksum, row.size, row.rows, row.batch_id = (
                    f.fingerprint,
                    f.checksum,
                    f.size,
                    f.rows,
                    batch_id,
                )
                row.ingested_at = datetime.now(UTC)
                s.add(row)
            run = s.get(IngestionRun, run_id)
            run.status = "succeeded"
            run.finished_at = datetime.now(UTC)
            run.duration_ms = int((time.monotonic() - started) * 1000)
            run.rows_read = rctx.rows
            run.rows_written = result["rows_written"]
            run.bytes_read = rctx.bytes
            run.files = len(rctx.files)
            run.table_version = result.get("table_version")
            run.dataset_id = result.get("dataset_id")
            run.lineage_run_id = lineage_run
            run.details = {
                "schema_changes": result.get("schema_changes", []),
                "files": [{"path": f.path, "rows": f.rows, "size": f.size, "sha256": f.checksum} for f in rctx.files],
                "watermark": encode_watermark(rctx.new_watermark),
                "kafka": {k: v for k, v in rctx.extra.items() if k.endswith("_offsets")},
            }
            summary = {
                "run_id": str(run_id),
                "batch_id": batch_id,
                "status": "succeeded",
                "rows_read": rctx.rows,
                "rows_written": result["rows_written"],
                "files": len(rctx.files),
                "duration_ms": run.duration_ms,
                "dataset": f"{spec.target.layer}.{spec.target.dataset}",
                "table_version": result.get("table_version"),
            }
        self._publish({"type": "ingestion.run", "job": job_name, **summary})
        if result["rows_written"]:
            from dataplat.transform.triggers import notify_updated

            summary["queued"] = notify_updated(
                self.ctx, [f"{spec.target.layer}.{spec.target.dataset}"], trigger=f"ingest:{job_name}"
            )
        return summary

    # ---------------------------------------------------------------- steps

    def _write_and_register(
        self,
        spec: JobSpec,
        uri: str,
        tables: list[pa.Table],
        run_id: uuid.UUID,
        job_id: uuid.UUID,
        rctx: ReadContext,
        lineage_run: str,
        out_namespace: str,
        job_name: str,
    ) -> dict[str, Any]:
        tf = self.ctx.tables
        rows_written, version = 0, None
        if tables:
            data = delta_compatible(pa.concat_tables(tables, promote_options="permissive"))
            mode = "overwrite" if spec.load_mode == "full" else "append"
            try:
                version = tf.write(data, uri, mode=mode)
            except Exception as e:  # delta-rs raises many error types (schema, storage...)
                hint = " (incompatible schema change?)" if "schema" in str(e).lower() else ""
                raise IngestionError(f"writing {spec.target.layer}.{spec.target.dataset} failed{hint}: {e}") from e
            rows_written = data.num_rows
        elif not tf.exists(uri):
            return {"rows_written": 0, "schema_changes": []}  # nothing read yet, nothing to register

        delta = tf.table(uri)
        schema = delta.to_pyarrow_dataset().schema
        stats = tf.stats(uri)
        with self.ctx.metadata.session() as s:
            ds, changes = catalog.register(
                s,
                layer=spec.target.layer,
                name=spec.target.dataset,
                uri=uri,
                schema=schema,
                run_id=run_id,
                source_job_id=job_id,
                stats=stats,
                table_version=delta.version(),
            )
            dataset_id = ds.id
            if tables:
                prof = profile(tf.dataset(uri), skip=set(AUDIT_COLUMNS))
                s.add(
                    DatasetProfile(
                        dataset_id=ds.id, run_id=run_id, row_count=prof["row_count"], columns=prof["columns"]
                    )
                )

        source_cols = [f.name for f in schema if f.name not in AUDIT_COLUMNS]
        inputs = [
            ol_dataset(rctx.namespace, name, self._input_facets(rctx, name)) for name in dict.fromkeys(rctx.inputs)
        ]
        output = ol_dataset(
            out_namespace,
            spec.target.dataset,
            {
                "schema": {"fields": [{"name": f.name, "type": str(f.type)} for f in schema]},
                "columnLineage": {
                    "fields": {
                        c: {
                            "inputFields": [
                                {"namespace": i["namespace"], "name": i["name"], "field": c} for i in inputs
                            ]
                        }
                        for c in source_cols
                    }
                },
                "outputStatistics": {"rowCount": rows_written, "size": stats.get("bytes")},
                "dataplat_dataset": {
                    "layer": spec.target.layer,
                    "dataset_id": str(dataset_id),
                    "table_version": version,
                },
            },
        )
        job_facets: dict[str, Any] = {
            "jobType": {"processingType": "BATCH", "integration": "DATAPLAT", "jobType": "INGESTION"}
        }
        if rctx.query:
            job_facets["sql"] = {"query": rctx.query}
        self._emit(
            run_event(
                "COMPLETE",
                NAMESPACE,
                f"ingest.{job_name}",
                run_id=lineage_run,
                inputs=inputs,
                outputs=[output],
                job_facets=job_facets,
            )
        )
        return {
            "rows_written": rows_written,
            "table_version": version,
            "dataset_id": dataset_id,
            "schema_changes": changes,
        }

    @staticmethod
    def _input_facets(rctx: ReadContext, name: str) -> dict[str, Any]:
        facets: dict[str, Any] = {"dataSource": {"name": rctx.namespace, "uri": rctx.namespace}}
        for f in rctx.files:
            if f.path == name:
                facets["dataplat_file"] = {"size": f.size, "sha256": f.checksum, "rows": f.rows}
        return facets

    def _fail(
        self,
        run_id: uuid.UUID,
        error: Exception,
        started: float,
        rctx: ReadContext,
        lineage_run: str,
        job_name: str,
        out_namespace: str,
    ) -> None:
        message = f"{type(error).__name__}: {error}"
        if isinstance(error, ConnectorError | IngestionError):
            message = str(error)
        message = redact(_ANSI.sub("", message))
        with self.ctx.metadata.session() as s:
            run = s.get(IngestionRun, run_id)
            run.status = "failed"
            run.error = message[:4000]
            run.finished_at = datetime.now(UTC)
            run.duration_ms = int((time.monotonic() - started) * 1000)
            run.rows_read = rctx.rows
            run.lineage_run_id = lineage_run
        self._emit(
            run_event(
                "FAIL",
                NAMESPACE,
                f"ingest.{job_name}",
                run_id=lineage_run,
                run_facets={"errorMessage": {"message": message[:1000], "programmingLanguage": "python"}},
            )
        )
        self._publish({"type": "ingestion.run", "job": job_name, "run_id": str(run_id), "status": "failed"})

    def _emit(self, event: dict[str, Any]) -> None:
        try:
            self.ctx.lineage.emit(event)
        except Exception:
            log.exception("lineage emit failed")

    def _publish(self, event: dict[str, Any]) -> None:
        try:
            self.ctx.events.publish(RUNS_TOPIC, event, key=event.get("job"))
            self.ctx.events.flush(5)
        except Exception:
            log.warning("could not publish run event", exc_info=True)
