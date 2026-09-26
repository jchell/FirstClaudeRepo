"""Continuous ingestion: Kafka topics (CDC changes, event streams, webhooks) -> bronze Delta.

One ``StreamRunner`` per continuous ingestion job. It consumes micro-batches (N
records or T seconds, whichever comes first) and writes them to the job's bronze
table in a single Delta commit that also records, per partition, the last offset
written (a Delta "app transaction"). On start it resumes from
max(committed Kafka offset, offset recorded in Delta + 1), so a crash between the
Delta commit and the Kafka offset commit never duplicates or loses changes.

Write modes:
  changelog  every change appended, with _op (c/u/d/r), _source_ts and _offset
  mirror     current state: MERGE by primary key, deletes applied (CDC only)
"""

from __future__ import annotations

import json
import logging
import statistics
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
from confluent_kafka import OFFSET_BEGINNING, Consumer, KafkaError, Producer, TopicPartition
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from dataplat.catalog import service as catalog
from dataplat.catalog.service import AUDIT_COLUMNS
from dataplat.connectors.files import normalize_records
from dataplat.connectors.registry import get_connector_class
from dataplat.core.context import PlatformContext
from dataplat.core.logging import redact
from dataplat.db.models import Connection, DatasetProfile, IngestionJob, StreamMetric, StreamState
from dataplat.ingestion.runner import delta_compatible
from dataplat.ingestion.spec import JobSpec
from dataplat.lineage.openlineage import dataset as ol_dataset
from dataplat.lineage.openlineage import run_event
from dataplat.quality.profiler import profile
from dataplat.streaming.debezium import cdc_topic
from dataplat.transform.triggers import notify_updated

log = logging.getLogger(__name__)

STREAM_AUDIT = ("_op", "_source_ts", "_offset")
DELETE_FLAG = "__dp_deleted"


class StreamError(Exception):
    pass


@dataclass
class StreamPlan:
    """Everything a runner needs, resolved from the job and its connection."""

    job_id: uuid.UUID
    job_name: str
    spec: JobSpec
    kind: str  # cdc | stream
    topic: str
    bootstrap: str
    uri: str
    source_namespace: str
    source_name: str
    group: str
    extra_consumer: dict[str, Any] = field(default_factory=dict)


def plan_for(ctx: PlatformContext, job: IngestionJob, conn: Connection) -> StreamPlan:
    spec = JobSpec.model_validate(job.spec)
    cls = get_connector_class(conn.type)
    namespace = cls(dict(conn.config), None).namespace()
    if spec.load_mode == "cdc":
        topic = cdc_topic(conn.type, conn.name, conn.config, spec.source.object or "")
        bootstrap, source_name = ctx.config.event_bus.bootstrap_servers, spec.source.object or topic
    elif conn.type == "webhook":
        topic = f"webhook.{conn.config['stream_name']}"
        bootstrap, source_name = ctx.config.event_bus.bootstrap_servers, topic
    else:  # kafka
        if conn.config.get("sasl_mechanism") or conn.config.get("password"):
            raise StreamError(
                "continuous streams from Kafka clusters with credentials aren't supported yet; "
                "use a scheduled append load instead"
            )
        topic = spec.source.object or ""
        bootstrap, source_name = conn.config["bootstrap_servers"], topic
    return StreamPlan(
        job_id=job.id,
        job_name=job.name,
        spec=spec,
        kind=spec.load_mode,
        topic=topic,
        bootstrap=bootstrap,
        uri=ctx.config.lake.uri(spec.target.layer, spec.target.dataset),
        source_namespace=namespace,
        source_name=source_name,
        group=f"dataplat-stream-{job.id.hex}",
    )


def _app_id(plan: StreamPlan, partition: int) -> str:
    return f"stream:{plan.job_id.hex}:{partition}"


def _truthy(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.lower() == "true")


class StreamRunner(threading.Thread):
    def __init__(self, ctx: PlatformContext, plan: StreamPlan, worker_id: str) -> None:
        super().__init__(name=f"stream-{plan.job_name}", daemon=True)
        self.ctx = ctx
        self.plan = plan
        self.worker_id = worker_id
        self.stop_event = threading.Event()
        self.error: str | None = None
        self._consumer: Consumer | None = None
        self._dlq: Producer | None = None
        self._last_lineage = float("-inf")  # first batch emits lineage right away
        self._last_profile = time.monotonic()
        self._last_stats = float("-inf")
        self._schema_sig: tuple | None = None
        self._lineage_run = str(uuid.uuid4())
        self._key_columns: list[str] | None = None
        self._totals = {"records": 0, "batches": 0, "dlq": 0}
        self._dataset_id: uuid.UUID | None = None

    # ---------------------------------------------------------------- lifecycle

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            self._set_state(status="running", started_at=datetime.now(UTC), last_error=None, worker=self.worker_id)
            self.ctx.lineage.emit(
                run_event(
                    "START",
                    "dataplat",
                    f"stream.{self.plan.job_name}",
                    run_id=self._lineage_run,
                    run_facets={
                        "dataplat_stream": {"mode": self.plan.spec.stream.write_mode, "topic": self.plan.topic}
                    },
                )
            )
            self._loop()
            self._set_state(status="paused")
        except Exception as e:
            self.error = redact(f"{type(e).__name__}: {e}")
            log.error("stream %s failed: %s\n%s", self.plan.job_name, self.error, redact(traceback.format_exc(limit=5)))
            self._set_state(status="failed", last_error=self.error[:2000])
        finally:
            if self._consumer is not None:
                self._consumer.close()
            if self._dlq is not None:
                self._dlq.flush(5)

    def _on_assign(self, consumer: Consumer, partitions: list[TopicPartition]) -> None:
        """Resume after the last offset recorded in Delta, if that is ahead of Kafka's."""
        committed = {tp.partition: tp.offset for tp in consumer.committed(partitions, timeout=10)}
        for tp in partitions:
            written = self.ctx.tables.app_transaction_version(self.plan.uri, _app_id(self.plan, tp.partition))
            start = committed.get(tp.partition, -1001)
            if written is not None and (start < 0 or written + 1 > start):
                tp.offset = written + 1
            elif start < 0:
                tp.offset = OFFSET_BEGINNING
            else:
                tp.offset = start
        consumer.assign(partitions)
        log.info("stream %s assigned %s", self.plan.job_name, [(tp.partition, tp.offset) for tp in partitions])

    def _loop(self) -> None:
        p = self.plan
        self._consumer = Consumer(
            {
                "bootstrap.servers": p.bootstrap,
                "group.id": p.group,
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
                "enable.partition.eof": False,
                "allow.auto.create.topics": True,
                "topic.metadata.refresh.interval.ms": 5000,
                **p.extra_consumer,
            }
        )
        self._consumer.subscribe([p.topic], on_assign=self._on_assign)
        max_records, max_seconds = p.spec.stream.max_records, p.spec.stream.max_seconds
        while not self.stop_event.is_set():
            batch: list[Any] = []
            deadline = time.monotonic() + max_seconds
            while len(batch) < max_records and time.monotonic() < deadline and not self.stop_event.is_set():
                msgs = self._consumer.consume(num_messages=max_records - len(batch), timeout=min(1.0, max_seconds))
                for m in msgs:
                    if m.error():
                        if m.error().code() not in (KafkaError._PARTITION_EOF, KafkaError.UNKNOWN_TOPIC_OR_PART):
                            raise StreamError(f"consumer error: {m.error()}")
                        continue
                    batch.append(m)
            if batch:
                self._process(batch)
            self._heartbeat()

    # ---------------------------------------------------------------- one micro-batch

    def _process(self, msgs: list[Any]) -> None:
        p = self.plan
        now = datetime.now(UTC)
        batch_id = uuid.uuid4().hex
        rows: list[dict[str, Any]] = []
        latencies: list[float] = []
        dlq = 0
        last_offsets: dict[int, int] = {}
        for m in msgs:
            last_offsets[m.partition()] = max(last_offsets.get(m.partition(), -1), m.offset())
            try:
                value = json.loads(m.value()) if m.value() is not None else None
                if not isinstance(value, dict):
                    raise ValueError("record is not a JSON object")
            except (ValueError, UnicodeDecodeError) as e:
                self._to_dlq(m, str(e))
                dlq += 1
                continue
            if p.kind == "cdc":
                op = value.pop("__op", None) or "r"
                src_ms = value.pop("__source_ts_ms", None)
                deleted = _truthy(value.pop("__deleted", False))
                row = {**self._key_values(m.key(), value), **value, "_op": "d" if deleted else op}
                if p.spec.stream.write_mode == "mirror":
                    row[DELETE_FLAG] = deleted
                if self._key_columns is None and m.key():
                    self._key_columns = self._key_from(m.key(), value)
            else:
                src_ms = m.timestamp()[1] if m.timestamp()[1] > 0 else None
                row = {**value, "_op": "c"}
            row["_source_ts"] = datetime.fromtimestamp(src_ms / 1000, UTC) if src_ms else None
            row["_offset"] = f"{m.partition()}:{m.offset()}"
            if src_ms:
                latencies.append(max(now.timestamp() * 1000 - src_ms, 0))
            rows.append(row)

        written = 0
        version = None
        if rows:
            table = self._to_table(rows, now, batch_id)
            txns = {_app_id(p, part): off for part, off in last_offsets.items()}
            if p.spec.stream.write_mode == "mirror":
                if not self._key_columns:
                    raise StreamError("mirror mode needs the source's primary key; the change events carry none")
                table = self._latest_per_key(table)
                version = self.ctx.tables.merge(table, p.uri, self._key_columns, DELETE_FLAG, txns)
            else:
                version = self.ctx.tables.write(table, p.uri, "append", app_transactions=txns)
            written = table.num_rows
            self._after_write(table, version)
            # Raw vault and downstream models follow each micro-batch.
            notify_updated(self.ctx, [f"{p.spec.target.layer}.{p.spec.target.dataset}"], trigger=f"stream:{p.job_name}")
        # Kafka offsets are committed only after Delta has the data (and the offsets). A batch
        # of nothing but dead letters commits Kafka offsets only; they are never re-read.
        self._consumer.commit(
            offsets=[TopicPartition(p.topic, part, off + 1) for part, off in last_offsets.items()], asynchronous=False
        )
        lag = self._lag()
        self._record_metrics(len(msgs) - dlq, dlq, latencies, lag, version)
        log.info("stream %s batch: %s records, %s dead letters, table v%s", p.job_name, written, dlq, version)

    @staticmethod
    def _key_values(raw_key: bytes | None, value: dict[str, Any]) -> dict[str, Any]:
        """Primary-key fields from the event key. Some deletes (MongoDB) carry nothing else."""
        if not raw_key:
            return {}
        try:
            key = json.loads(raw_key)
        except ValueError:
            return {}
        if not isinstance(key, dict):
            return {}
        if list(key) == ["id"] and "id" not in value:
            return {"_id": key["id"]}  # MongoDB documents are keyed by _id
        return key

    def _key_from(self, raw_key: bytes, value: dict[str, Any]) -> list[str] | None:
        try:
            key = json.loads(raw_key)
        except ValueError:
            return None
        if not isinstance(key, dict) or not key:
            return None
        cols = list(key)
        if cols == ["id"] and "id" not in value:
            return ["_id"]  # MongoDB documents
        return cols

    def _to_table(self, rows: list[dict[str, Any]], now: datetime, batch_id: str) -> pa.Table:
        table = pa.Table.from_pylist(normalize_records(rows))
        n = table.num_rows
        table = (
            table.append_column("_load_ts", pa.array([now] * n, pa.timestamp("us", tz="UTC")))
            .append_column(
                "_source", pa.array([f"{self.plan.source_namespace}/{self.plan.source_name}"] * n, pa.string())
            )
            .append_column("_batch_id", pa.array([batch_id] * n, pa.string()))
            .append_column("_file", pa.nulls(n, pa.string()))
        )
        if DELETE_FLAG in table.column_names:
            table = table.set_column(
                table.column_names.index(DELETE_FLAG), DELETE_FLAG, pc.fill_null(table.column(DELETE_FLAG), False)
            )
        return self._conform(delta_compatible(table))

    def _conform(self, table: pa.Table) -> pa.Table:
        """Casts batch columns to the table's existing types (JSON can flip int/float/null)."""
        if not self.ctx.tables.exists(self.plan.uri):
            return table
        existing = self.ctx.tables.dataset(self.plan.uri).schema
        for i, name in enumerate(table.column_names):
            if name in existing.names and table.schema.field(i).type != existing.field(name).type:
                try:
                    table = table.set_column(i, name, table.column(i).cast(existing.field(name).type))
                except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as e:
                    raise StreamError(f"column {name!r} changed type to {table.schema.field(i).type}: {e}") from e
        return table

    def _latest_per_key(self, table: pa.Table) -> pa.Table:
        """Within a batch only the last change per key matters for the mirror."""
        keys = self._key_columns or []
        seen: dict[tuple, int] = {}
        cols = [table.column(k).to_pylist() for k in keys]
        for i, key in enumerate(zip(*cols, strict=True)):
            seen[key] = i  # rows are in offset order, so later wins
        return table.take(sorted(seen.values()))

    def _to_dlq(self, msg: Any, error: str) -> None:
        if self._dlq is None:
            self._dlq = Producer({"bootstrap.servers": self.ctx.config.event_bus.bootstrap_servers})
        self._dlq.produce(
            f"dlq.{self.plan.job_id.hex[:12]}",
            msg.value() or b"",
            key=msg.key(),
            headers={
                "error": error[:500],
                "source_topic": msg.topic(),
                "source_offset": f"{msg.partition()}:{msg.offset()}",
            },
        )
        self._dlq.poll(0)

    def _lag(self) -> int | None:
        try:
            total = 0
            for tp in self._consumer.assignment():
                _, high = self._consumer.get_watermark_offsets(tp, cached=False, timeout=2)
                pos = self._consumer.position([tp])[0].offset
                total += max(high - pos, 0) if pos >= 0 else high
            return total
        except Exception:
            return None

    # ---------------------------------------------------------------- catalog, lineage, metrics

    def _after_write(self, table: pa.Table, version: int | None) -> None:
        p = self.plan
        sig = tuple((f.name, str(f.type)) for f in table.schema if f.name != DELETE_FLAG)
        due_stats = time.monotonic() - self._last_stats > 30
        if sig != self._schema_sig or due_stats:
            schema = self.ctx.tables.dataset(p.uri).schema
            with self.ctx.metadata.session() as s:
                ds, _ = catalog.register(
                    s,
                    layer=p.spec.target.layer,
                    name=p.spec.target.dataset,
                    uri=p.uri,
                    schema=schema,
                    source_job_id=p.job_id,
                    stats=self.ctx.tables.stats(p.uri),
                    table_version=version,
                )
                self._dataset_id = ds.id
            self._schema_sig = sig
            self._last_stats = time.monotonic()
        else:
            with self.ctx.metadata.session() as s:
                from dataplat.db.models import Dataset

                ds = s.scalars(
                    select(Dataset).where(Dataset.layer == p.spec.target.layer, Dataset.name == p.spec.target.dataset)
                ).first()
                if ds is not None:
                    ds.last_loaded_at, ds.table_version = datetime.now(UTC), version
        cfg = self.ctx.config.streaming
        if time.monotonic() - self._last_lineage > cfg.lineage_every_seconds:
            self._emit_lineage()
            self._last_lineage = time.monotonic()
        if time.monotonic() - self._last_profile > cfg.profile_every_seconds:
            prof = profile(self.ctx.tables.dataset(p.uri), skip=set(AUDIT_COLUMNS) | set(STREAM_AUDIT))
            with self.ctx.metadata.session() as s:
                s.add(DatasetProfile(dataset_id=self._dataset_id, row_count=prof["row_count"], columns=prof["columns"]))
            self._last_profile = time.monotonic()

    def _emit_lineage(self) -> None:
        p = self.plan
        schema = self.ctx.tables.dataset(p.uri).schema
        inputs = [
            ol_dataset(
                p.source_namespace,
                p.source_name,
                {"dataSource": {"name": p.source_namespace, "uri": p.source_namespace}},
            )
        ]
        cols = [f.name for f in schema if f.name not in AUDIT_COLUMNS and f.name not in STREAM_AUDIT]
        output = ol_dataset(
            self.ctx.config.lake.model_dump()[p.spec.target.layer],
            p.spec.target.dataset,
            {
                "schema": {"fields": [{"name": f.name, "type": str(f.type)} for f in schema]},
                "columnLineage": {
                    "fields": {
                        c: {"inputFields": [{"namespace": p.source_namespace, "name": p.source_name, "field": c}]}
                        for c in cols
                    }
                },
                "dataplat_dataset": {"layer": p.spec.target.layer},
            },
        )
        self.ctx.lineage.emit(
            run_event(
                "COMPLETE",
                "dataplat",
                f"stream.{p.job_name}",
                run_id=self._lineage_run,
                inputs=inputs,
                outputs=[output],
                job_facets={
                    "jobType": {"processingType": "STREAMING", "integration": "DATAPLAT", "jobType": p.kind.upper()}
                },
            )
        )
        self._lineage_run = str(uuid.uuid4())

    def _record_metrics(
        self, records: int, dlq: int, latencies: list[float], lag: int | None, version: int | None
    ) -> None:
        self._totals["records"] += records
        self._totals["batches"] += 1
        self._totals["dlq"] += dlq
        p50 = int(statistics.median(latencies)) if latencies else None
        p95 = int(sorted(latencies)[max(int(len(latencies) * 0.95) - 1, 0)]) if latencies else None
        now = datetime.now(UTC)
        minute = now.replace(second=0, microsecond=0)
        metrics = {
            "lag": lag,
            "latency_p50_ms": p50,
            "latency_p95_ms": p95,
            "last_batch_records": records,
            "table_version": version,
            "at": now.isoformat(),
        }
        with self.ctx.metadata.session() as s:
            st = s.get(StreamState, self.plan.job_id)
            if st is not None:
                st.metrics = {**(st.metrics or {}), **metrics}
                st.totals = {
                    k: (st.totals or {}).get(k, 0) + v for k, v in (("records", records), ("batches", 1), ("dlq", dlq))
                }
                st.last_batch_at = now
                st.heartbeat_at = now
                if self._key_columns and st.key_columns != self._key_columns:
                    st.key_columns = self._key_columns
            stmt = pg_insert(StreamMetric).values(
                job_id=self.plan.job_id,
                minute=minute,
                records=records,
                batches=1,
                dlq=dlq,
                latency_p50_ms=p50,
                latency_p95_ms=p95,
                lag=lag,
            )
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["job_id", "minute"],
                    set_={
                        "records": StreamMetric.records + stmt.excluded.records,
                        "batches": StreamMetric.batches + 1,
                        "dlq": StreamMetric.dlq + stmt.excluded.dlq,
                        "latency_p50_ms": stmt.excluded.latency_p50_ms,
                        "latency_p95_ms": stmt.excluded.latency_p95_ms,
                        "lag": stmt.excluded.lag,
                    },
                )
            )

    def _heartbeat(self) -> None:
        with self.ctx.metadata.session() as s:
            st = s.get(StreamState, self.plan.job_id)
            if st is not None:
                st.heartbeat_at = datetime.now(UTC)
                lag = self._lag()
                if lag is not None:
                    st.metrics = {**(st.metrics or {}), "lag": lag}

    def _set_state(self, **values: Any) -> None:
        with self.ctx.metadata.session() as s:
            st = s.get(StreamState, self.plan.job_id)
            if st is None:
                st = StreamState(job_id=self.plan.job_id, topics=[self.plan.topic])
                s.add(st)
            for k, v in values.items():
                setattr(st, k, v)
            st.topics = [self.plan.topic]
            st.heartbeat_at = datetime.now(UTC)
