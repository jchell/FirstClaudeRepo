"""Stream worker process: runs every continuous ingestion job (CDC, event streams, webhooks).

A reconcile loop compares what should run (enabled cdc/stream jobs whose desired
state is "running") with the runner threads it has, starts and stops runners, and
restarts failed ones with exponential backoff. It also reports Debezium connector
health for CDC jobs and heartbeats on the ``dataplat.control`` topic.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from dataplat.core.context import PlatformContext
from dataplat.core.logging import configure_logging
from dataplat.db.models import Connection, IngestionJob, StreamState
from dataplat.ingestion.spec import is_continuous
from dataplat.streaming.debezium import connector_name
from dataplat.streaming.processor import StreamError, StreamRunner, plan_for

log = logging.getLogger(__name__)

CONTROL_TOPIC = "dataplat.control"
RECONCILE_SECONDS = 3


class StreamWorker:
    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx
        self.worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self._stop = threading.Event()
        self.runners: dict[uuid.UUID, StreamRunner] = {}
        self._versions: dict[uuid.UUID, int] = {}
        self._backoff: dict[uuid.UUID, tuple[float, float]] = {}  # job -> (retry_at, delay)
        self._last_connector_check = 0.0

    def stop(self, *_: Any) -> None:
        self._stop.set()

    def wanted(self) -> dict[uuid.UUID, tuple[IngestionJob, Connection, StreamState]]:
        with self.ctx.metadata.session() as s:
            out = {}
            for job in s.scalars(select(IngestionJob).where(IngestionJob.enabled)):
                if not is_continuous(job.spec):
                    continue
                st = s.get(StreamState, job.id)
                if st is None:
                    st = StreamState(job_id=job.id, desired="running", status="starting")
                    s.add(st)
                    s.flush()
                if st.desired == "running":
                    out[job.id] = (job, s.get(Connection, job.connection_id), st)
            s.expunge_all()
            return out

    def reconcile(self) -> None:
        wanted = self.wanted()
        now = time.monotonic()
        # Stop runners no longer wanted, or whose job definition changed.
        for job_id, runner in list(self.runners.items()):
            job = wanted.get(job_id)
            if job is None or self._versions.get(job_id) != job[0].version or not runner.is_alive():
                runner.stop()
                runner.join(timeout=30)
                del self.runners[job_id]
                if runner.error:
                    retry_at, delay = self._backoff.get(job_id, (0, 5))
                    self._backoff[job_id] = (now + delay, min(delay * 2, 300))
                    self._alert(job_id, runner.plan.job_name, runner.error)
        # Start what's missing.
        for job_id, (job, conn, _) in wanted.items():
            if job_id in self.runners or self._backoff.get(job_id, (0, 0))[0] > now:
                continue
            try:
                plan = plan_for(self.ctx, job, conn)
            except StreamError as e:
                self._fail(job_id, str(e))
                continue
            runner = StreamRunner(self.ctx, plan, self.worker_id)
            runner.start()
            self.runners[job_id] = runner
            self._versions[job_id] = job.version
            log.info("started stream %s (%s -> %s)", job.name, plan.topic, plan.uri)
        # Streams that have been healthy for a while get their backoff and alerts reset.
        for job_id, runner in self.runners.items():
            if runner.is_alive() and job_id in self._backoff and self._backoff[job_id][0] < now - 60:
                del self._backoff[job_id]
                self._resolve_alert(runner.plan.job_name)
        if now - self._last_connector_check > 15:
            self._check_connectors(wanted)
            self._last_connector_check = now

    def _check_connectors(self, wanted: dict[uuid.UUID, tuple[IngestionJob, Connection, StreamState]]) -> None:
        for job_id, (job, _, _) in wanted.items():
            if job.spec.get("load_mode") != "cdc":
                continue
            try:
                status = self.ctx.change_capture.status(connector_name(job_id))
            except Exception as e:
                status = {"state": "UNREACHABLE", "error": type(e).__name__}
            with self.ctx.metadata.session() as s:
                st = s.get(StreamState, job_id)
                if st is not None:
                    st.connector_name = connector_name(job_id)
                    st.metrics = {**(st.metrics or {}), "connector": status}
            if status.get("state") == "FAILED" or any(t.get("state") == "FAILED" for t in status.get("tasks", [])):
                trace = next((t["trace"][0] for t in status.get("tasks", []) if t.get("trace")), "connector failed")
                self._alert(job_id, job.name, f"CDC connector failed: {trace}")

    def _fail(self, job_id: uuid.UUID, message: str) -> None:
        with self.ctx.metadata.session() as s:
            st = s.get(StreamState, job_id)
            if st is not None:
                st.status, st.last_error = "failed", message
        retry_at, delay = self._backoff.get(job_id, (0, 30))
        self._backoff[job_id] = (time.monotonic() + delay, min(delay * 2, 300))

    def _alert(self, job_id: uuid.UUID, job_name: str, message: str) -> None:
        from dataplat.orchestration.alerts import open_alert

        try:
            open_alert(self.ctx, "stream_failed", f"stream:{job_name}", message, severity="serious")
        except Exception:
            log.exception("could not record alert")

    def _resolve_alert(self, job_name: str) -> None:
        from dataplat.orchestration.alerts import resolve_alert

        try:
            resolve_alert(self.ctx, "stream_failed", f"stream:{job_name}")
        except Exception:
            log.exception("could not resolve alert")

    def run_forever(self) -> None:
        bus = self.ctx.events
        bus.ensure_topic(CONTROL_TOPIC)
        last_beat = 0.0
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception:
                log.exception("reconcile failed")
            if time.monotonic() - last_beat > 30:
                try:
                    bus.publish(
                        CONTROL_TOPIC,
                        {
                            "type": "heartbeat",
                            "worker": self.worker_id,
                            "streams": len(self.runners),
                            "ts": datetime.now(UTC).isoformat(),
                        },
                    )
                    bus.flush(5)
                except Exception:
                    log.warning("heartbeat publish failed")
                last_beat = time.monotonic()
            self._stop.wait(RECONCILE_SECONDS)
        for runner in self.runners.values():
            runner.stop()
        for runner in self.runners.values():
            runner.join(timeout=30)


def main() -> None:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    ctx = PlatformContext()
    worker = StreamWorker(ctx)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    log.info("stream worker %s started", worker.worker_id)
    worker.run_forever()
    ctx.close()
