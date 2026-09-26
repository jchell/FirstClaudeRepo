"""Event-driven propagation: new data in a dataset queues whatever consumes it.

  bronze/silver dataset with vault mappings  ->  vault.load for that source
  any dataset a pipeline is triggered by     ->  pipeline.run

Jobs are queued with coalescing keys: at most one waits per target, and a trigger
that arrives while one is running queues a follow-up, so nothing is missed and a
busy stream doesn't flood the queue.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable

from sqlalchemy import select

from dataplat.adapters.pg_queue import COALESCE_PREFIX, DuplicateJob
from dataplat.core.context import PlatformContext
from dataplat.db.models import Pipeline, VaultMapping

log = logging.getLogger(__name__)


def _enqueue(ctx: PlatformContext, kind: str, payload: dict, key: str) -> bool:
    try:
        # Model errors don't heal on retry; the next data change triggers the pipeline again.
        attempts = 1 if kind == "pipeline.run" else 3
        ctx.jobs.enqueue(kind, payload, dedupe_key=f"{COALESCE_PREFIX}{key}", max_attempts=attempts)
        return True
    except DuplicateJob:
        return False  # one is already waiting; it will see this data too


def notify_updated(
    ctx: PlatformContext,
    datasets: Iterable[str],
    *,
    trigger: str = "data",
    skip_pipeline: uuid.UUID | None = None,
) -> dict[str, list[str]]:
    """``datasets`` are "<layer>.<name>" references that just received new data."""
    datasets = list(dict.fromkeys(datasets))
    queued: dict[str, list[str]] = {"vault_loads": [], "pipelines": []}
    if not datasets:
        return queued
    try:
        with ctx.metadata.session() as s:
            sources = {
                (m.source_layer, m.source_dataset) for m in s.scalars(select(VaultMapping).where(VaultMapping.enabled))
            }
            pipelines = [
                (p.id, p.name, p.trigger_datasets or []) for p in s.scalars(select(Pipeline).where(Pipeline.enabled))
            ]
        for ref in datasets:
            layer, _, name = ref.partition(".")
            if (layer, name) in sources and _enqueue(
                ctx, "vault.load", {"layer": layer, "dataset": name, "trigger": trigger}, f"vault:{ref}"
            ):
                queued["vault_loads"].append(ref)
        for pid, pname, triggers in pipelines:
            if pid != skip_pipeline and set(triggers) & set(datasets):
                if _enqueue(ctx, "pipeline.run", {"pipeline_id": str(pid), "trigger": trigger}, f"pipeline:{pid}"):
                    queued["pipelines"].append(pname)
    except Exception:
        # Propagation must never fail the load that triggered it.
        log.exception("could not queue downstream work for %s", datasets)
    return queued
