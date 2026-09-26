"""Scheduler process: turns due schedules into queued jobs.

APScheduler drives the tick; schedule state lives in Postgres, and due rows are
claimed with SKIP LOCKED so running two schedulers never double-enqueues.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import or_, select

from dataplat.adapters.pg_queue import DuplicateJob
from dataplat.core.context import PlatformContext
from dataplat.core.logging import configure_logging
from dataplat.db.models import Schedule

log = logging.getLogger(__name__)


def next_run(schedule: Schedule, after: datetime) -> datetime | None:
    if schedule.cron:
        return CronTrigger.from_crontab(schedule.cron, timezone=UTC).get_next_fire_time(None, after)
    if schedule.interval_seconds:
        return after + timedelta(seconds=schedule.interval_seconds)
    return None


class SchedulerService:
    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx

    def tick(self) -> int:
        now = datetime.now(UTC)
        enqueued = 0
        with self.ctx.metadata.session() as s:
            due = s.scalars(
                select(Schedule)
                .where(Schedule.enabled, or_(Schedule.next_run_at.is_(None), Schedule.next_run_at <= now))
                .with_for_update(skip_locked=True)
            ).all()
            for sch in due:
                if sch.next_run_at is None:
                    # Newly created: compute the first fire time instead of firing immediately.
                    sch.next_run_at = next_run(sch, now)
                    continue
                try:
                    self.ctx.jobs.enqueue(
                        sch.kind,
                        dict(sch.payload or {}),
                        dedupe_key=f"schedule:{sch.id}",
                        service_account=sch.service_account,
                    )
                    enqueued += 1
                except DuplicateJob:
                    log.info("schedule %s skipped: previous run still active", sch.name)
                sch.last_run_at = now
                sch.next_run_at = next_run(sch, now)
        return enqueued

    def dispatch_tokens(self) -> int:
        """Attaches a single-use, service-account-scoped Vault token to waiting jobs.

        Only the scheduler's AppRole may mint these tokens (and read the sa-* policies
        to check they still match the managed template), so neither the API nor the
        workers can ever read a service account's secrets on their own.
        """
        attached = 0
        for job_id, account in self.ctx.jobs.pending_token_jobs():
            try:
                wrap = self.ctx.service_account_vault.mint_wrapped_token(account)
            except Exception as e:
                log.warning("cannot mint token for job %s (%s): %s", job_id, account, e)
                self.ctx.jobs.fail(job_id, f"service account token: {type(e).__name__}: {e}", retry=False)
                continue
            if self.ctx.jobs.attach_token(job_id, wrap):
                attached += 1
        return attached


def main() -> None:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    ctx = PlatformContext()
    service = SchedulerService(ctx)
    scheduler = BlockingScheduler(timezone=UTC)
    scheduler.add_job(
        service.tick, "interval", seconds=ctx.config.scheduler.tick_seconds, max_instances=1, coalesce=True
    )
    scheduler.add_job(service.dispatch_tokens, "interval", seconds=2, max_instances=1, coalesce=True)
    log.info("scheduler started (tick %ss)", ctx.config.scheduler.tick_seconds)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        ctx.close()
