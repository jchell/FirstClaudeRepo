"""Worker process: claims jobs from the queue and runs their handlers."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import traceback
import uuid
from typing import Any

from hvac.exceptions import Forbidden

from dataplat.adapters.vault_client import VaultClient
from dataplat.adapters.vault_secrets import VaultSecretStore
from dataplat.core.context import PlatformContext
from dataplat.core.logging import configure_logging, redact
from dataplat.core.ports.orchestration import Job
from dataplat.core.ports.secrets import SecretStore
from dataplat.lineage.openlineage import run_event
from dataplat.orchestration.handlers import HANDLERS

log = logging.getLogger(__name__)

NAMESPACE = "dataplat"

# Kinds that emit their own, richer lineage (ingestion) or move no data (source
# checks); a generic START/COMPLETE for them would only clutter the graph.
NO_GENERIC_LINEAGE = {
    "ingestion.run",
    "connection.test",
    "connection.discover",
    "connection.preview",
    "vault.load",
    "vault.build",
    "model.run",
    "model.preview",
    "pipeline.run",
    "dq.run",
    "governance.classify",
    "alert.notify",
    "alert.test",
    "data.query",
}


class _ForbiddenAsPermissionError(VaultSecretStore):
    def resolve(self, ref):  # type: ignore[override]
        try:
            return super().resolve(ref)
        except Forbidden as e:
            raise PermissionError(str(ref)) from e


class Worker:
    def __init__(self, ctx: PlatformContext, worker_id: str | None = None, kinds: list[str] | None = None) -> None:
        self.ctx = ctx
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.kinds = kinds
        self._stop = threading.Event()

    def stop(self, *_: Any) -> None:
        self._stop.set()

    def _scoped_secrets(self, job: Job) -> SecretStore | None:
        if not job.service_account:
            return None
        if not job.vault_wrap_token:
            raise PermissionError("service-account job has no Vault token (already claimed once?)")
        vc = self.ctx.config.vault
        scoped = VaultClient.unwrap(vc.addr, job.vault_wrap_token)
        return _ForbiddenAsPermissionError(scoped, vc.kv_mount, vc.kv_prefix)

    def run_one(self) -> bool:
        job = self.ctx.jobs.claim(self.worker_id, self.kinds)
        if job is None:
            return False
        handler = HANDLERS.get(job.kind)
        ev_start = run_event("START", NAMESPACE, job.kind, run_facets={"job_id": {"id": job.id}})
        run_id = ev_start["run"]["runId"]
        emit = job.kind not in NO_GENERIC_LINEAGE
        if emit:
            self._emit(ev_start)
        try:
            if handler is None:
                raise LookupError(f"no handler for job kind {job.kind!r}")
            secrets = self._scoped_secrets(job)
            result = handler(self.ctx, job, secrets)
        except Exception as e:
            err = redact(f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}")
            log.warning("job %s (%s) failed: %s", job.id, job.kind, err.splitlines()[0])
            self.ctx.jobs.fail(job.id, err)
            if emit:
                self._emit(run_event("FAIL", NAMESPACE, job.kind, run_id=run_id))
            return True
        self.ctx.jobs.complete(job.id, result)
        if emit:
            self._emit(run_event("COMPLETE", NAMESPACE, job.kind, run_id=run_id))
        log.info("job %s (%s) succeeded", job.id, job.kind)
        return True

    def _emit(self, event: dict[str, Any]) -> None:
        try:
            self.ctx.lineage.emit(event)
        except Exception:
            log.exception("lineage emit failed")

    def run_forever(self) -> None:
        interval = self.ctx.config.worker.poll_interval_seconds
        log.info("worker %s started", self.worker_id)
        while not self._stop.is_set():
            try:
                busy = self.run_one()
            except Exception:
                log.exception("worker loop error")
                busy = False
            if not busy:
                self._stop.wait(interval)
        log.info("worker %s stopped", self.worker_id)


def main() -> None:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    worker = Worker(PlatformContext())
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()
