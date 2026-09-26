"""Job queue, worker, scheduler and lineage emission against a real Postgres."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from dataplat.adapters.pg_queue import DuplicateJob
from dataplat.core.context import PlatformContext
from dataplat.db.models import JobRow, Schedule
from dataplat.orchestration.scheduler import SchedulerService
from dataplat.orchestration.worker import Worker


def test_enqueue_claim_complete(ctx: PlatformContext) -> None:
    job_id = ctx.jobs.enqueue("platform.echo", {"x": 1})
    assert Worker(ctx).run_one() is True
    st = ctx.jobs.status(job_id)
    assert st["status"] == "succeeded" and st["result"] == {"echo": {"x": 1}}
    types = [e["eventType"] for e in reversed(ctx.lineage.events("platform.echo"))]
    assert types == ["START", "COMPLETE"]


def test_failure_retries_then_fails(ctx: PlatformContext) -> None:
    job_id = ctx.jobs.enqueue("platform.echo", {"fail": True}, max_attempts=2)
    worker = Worker(ctx)
    worker.run_one()
    st = ctx.jobs.status(job_id)
    assert st["status"] == "queued" and "echo asked to fail" in st["last_error"]
    # Make the retry due now.
    with ctx.metadata.session() as s:
        s.get(JobRow, job_id).run_after = datetime.now(UTC)
    worker.run_one()
    assert ctx.jobs.status(job_id)["status"] == "failed"


def test_dedupe_key_blocks_duplicates_while_active(ctx: PlatformContext) -> None:
    ctx.jobs.enqueue("platform.echo", {}, dedupe_key="k")
    with pytest.raises(DuplicateJob):
        ctx.jobs.enqueue("platform.echo", {}, dedupe_key="k")
    Worker(ctx).run_one()
    ctx.jobs.enqueue("platform.echo", {}, dedupe_key="k")  # finished jobs don't block


def test_concurrent_claims_never_double_claim(ctx: PlatformContext) -> None:
    ids = {ctx.jobs.enqueue("platform.echo", {"i": i}) for i in range(20)}
    claimed: list[int] = []
    lock = threading.Lock()

    def grab() -> None:
        while (job := ctx.jobs.claim("w")) is not None:
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=grab) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(ids)


def test_service_account_jobs_wait_for_dispatcher_token(ctx: PlatformContext) -> None:
    job_id = ctx.jobs.enqueue("platform.echo", {}, service_account="etl")
    assert ctx.jobs.claim("w") is None  # no token yet

    assert SchedulerService(ctx).dispatch_tokens() == 1
    job = ctx.jobs.claim("w")
    assert job is not None and job.id == job_id and job.vault_wrap_token == "wrap-etl-1"
    with ctx.metadata.session() as s:
        assert s.get(JobRow, job_id).vault_wrap_token is None  # handed out once, then erased


def test_scheduler_enqueues_due_interval_schedules(ctx: PlatformContext) -> None:
    with ctx.metadata.session() as s:
        s.add(Schedule(name="hb", kind="platform.echo", payload={"hb": 1}, interval_seconds=60))
    svc = SchedulerService(ctx)
    assert svc.tick() == 0  # first tick only computes next_run_at
    with ctx.metadata.session() as s:
        sch = s.query(Schedule).one()
        sch.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
    assert svc.tick() == 1
    assert svc.tick() == 0  # not due again yet
    with ctx.metadata.session() as s:
        assert s.query(JobRow).filter_by(kind="platform.echo").count() == 1
