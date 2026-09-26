"""Worker job handlers for models and pipelines."""

from __future__ import annotations

import uuid
from typing import Any

from dataplat.core.context import PlatformContext
from dataplat.core.ports.orchestration import Job
from dataplat.core.ports.secrets import SecretStore
from dataplat.orchestration.handlers import handler
from dataplat.transform.runner import ModelError, ModelRunner, ModelSpec, PipelineRunner, SkipModel


@handler("model.run")
def run_model(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    try:
        return ModelRunner(ctx).run_model(job.payload["model"], trigger=job.payload.get("trigger", "manual"))
    except SkipModel as e:
        return {"model": job.payload["model"], "skipped": str(e)}


@handler("pipeline.run")
def run_pipeline(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    return PipelineRunner(ctx).run(uuid.UUID(job.payload["pipeline_id"]), trigger=job.payload.get("trigger", "manual"))


@handler("model.preview")
def preview_model(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    """First rows of a draft model. The API hands the result out once, then scrubs it."""
    p = job.payload
    spec = ModelSpec(p["name"], p["layer"], p["kind"], p.get("sql", ""), p.get("config", {}))
    try:
        return {"ok": True, **ModelRunner(ctx).preview(spec)}
    except ModelError as e:
        return {"ok": False, "error": str(e)}
