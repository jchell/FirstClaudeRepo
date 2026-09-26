"""JobQueue on Postgres using SELECT ... FOR UPDATE SKIP LOCKED.

Workers claim jobs concurrently without blocking each other. Failed jobs retry with
exponential backoff until max_attempts; jobs whose worker died are reclaimed after
a lease timeout.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from dataplat.core.config import PlatformConfig
from dataplat.core.ports.metadata import MetadataStore
from dataplat.core.ports.orchestration import Job
from dataplat.db.models import JobRow

LEASE = timedelta(minutes=30)


# Dedupe keys with this prefix only coalesce *queued* jobs (see claim()); plain keys also
# block while a job is running.
COALESCE_PREFIX = "coalesce:"


class DuplicateJob(Exception):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


class PostgresJobQueue:
    def __init__(self, metadata: MetadataStore) -> None:
        self.metadata = metadata

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> PostgresJobQueue:
        return cls(registry.get("metadata_store"))

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        run_after: datetime | None = None,
        dedupe_key: str | None = None,
        service_account: str | None = None,
        max_attempts: int = 3,
        vault_wrap_token: str | None = None,
    ) -> int:
        row = JobRow(
            kind=kind,
            payload=payload,
            status="queued",
            attempts=0,
            max_attempts=max_attempts,
            run_after=run_after or _now(),
            dedupe_key=dedupe_key,
            service_account=service_account,
            vault_wrap_token=vault_wrap_token,
        )
        try:
            with self.metadata.session() as s:
                s.add(row)
                s.flush()
                return row.id
        except IntegrityError as e:
            raise DuplicateJob(dedupe_key) from e

    def claim(self, worker_id: str, kinds: list[str] | None = None) -> Job | None:
        now = _now()
        with self.metadata.session() as s:
            q = (
                select(JobRow)
                .where(
                    or_(
                        (JobRow.status == "queued")
                        & (JobRow.run_after <= now)
                        # Service-account jobs wait until the dispatcher has attached a token.
                        & (JobRow.service_account.is_(None) | JobRow.vault_wrap_token.is_not(None)),
                        # Reclaim jobs whose worker died mid-run.
                        (JobRow.status == "running") & (JobRow.locked_at < now - LEASE),
                    )
                )
                .order_by(JobRow.run_after, JobRow.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if kinds:
                q = q.where(JobRow.kind.in_(kinds))
            row = s.scalars(q).first()
            if row is None:
                return None
            row.status = "running"
            if row.dedupe_key and row.dedupe_key.startswith(COALESCE_PREFIX):
                # "At most one waiting" keys: once this run starts, the next trigger may queue
                # a follow-up, so changes that arrive mid-run are never dropped.
                row.dedupe_key = None
            row.attempts += 1
            row.locked_by = worker_id
            row.locked_at = now
            wrap, row.vault_wrap_token = row.vault_wrap_token, None  # single use: never handed out twice
            return Job(
                id=row.id,
                kind=row.kind,
                payload=dict(row.payload or {}),
                attempts=row.attempts,
                max_attempts=row.max_attempts,
                service_account=row.service_account,
                vault_wrap_token=wrap,
            )

    def complete(self, job_id: int, result: dict[str, Any] | None = None) -> None:
        with self.metadata.session() as s:
            s.execute(
                update(JobRow)
                .where(JobRow.id == job_id)
                .values(status="succeeded", result=result, finished_at=_now(), locked_by=None, last_error=None)
            )

    def fail(self, job_id: int, error: str, retry: bool = True) -> None:
        with self.metadata.session() as s:
            row = s.get(JobRow, job_id, with_for_update=True)
            if row is None:
                return
            row.last_error = error[:4000]
            row.locked_by = None
            if not retry or row.attempts >= row.max_attempts:
                row.status = "failed"
                row.finished_at = _now()
            else:
                row.status = "queued"
                row.run_after = _now() + timedelta(seconds=min(30 * 2 ** (row.attempts - 1), 3600))

    def pending_token_jobs(self, limit: int = 50) -> list[tuple[int, str]]:
        """Queued service-account jobs still waiting for a Vault token."""
        with self.metadata.session() as s:
            rows = s.execute(
                select(JobRow.id, JobRow.service_account)
                .where(
                    JobRow.status == "queued",
                    JobRow.service_account.is_not(None),
                    JobRow.vault_wrap_token.is_(None),
                )
                .order_by(JobRow.id)
                .limit(limit)
            ).all()
            return [(r[0], r[1]) for r in rows]

    def attach_token(self, job_id: int, wrap_token: str) -> bool:
        with self.metadata.session() as s:
            res = s.execute(
                update(JobRow)
                .where(JobRow.id == job_id, JobRow.status == "queued", JobRow.vault_wrap_token.is_(None))
                .values(vault_wrap_token=wrap_token)
            )
            return res.rowcount == 1

    def status(self, job_id: int) -> dict[str, Any]:
        with self.metadata.session() as s:
            row = s.get(JobRow, job_id)
            if row is None:
                raise KeyError(job_id)
            return {
                "id": row.id,
                "kind": row.kind,
                "status": row.status,
                "attempts": row.attempts,
                "last_error": row.last_error,
                "result": row.result,
                "created_at": row.created_at,
                "finished_at": row.finished_at,
            }
