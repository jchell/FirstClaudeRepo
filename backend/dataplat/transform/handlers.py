"""Worker job handlers for models and pipelines."""

from __future__ import annotations

import uuid
from typing import Any

from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
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
        return {"ok": True, **ModelRunner(ctx).preview(spec, principal=principal_of(p))}
    except ModelError as e:
        return {"ok": False, "error": str(e)}


def principal_of(payload: dict[str, Any]) -> Principal | None:
    """The person a task runs for (set by the API when it queued the task)."""
    p = payload.get("principal")
    if not p:
        return None
    return Principal(id=p["id"], name=p["name"], kind=p.get("kind", "user"), roles=frozenset(p.get("roles", [])))


@handler("data.query")
def query(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    """A person's ad-hoc SELECT over lake tables, under their data policies."""
    from dataplat.security.policy import AccessDenied, PolicyEngine, secured_relations
    from dataplat.transform.output import error_message
    from dataplat.transform.runner import all_models
    from dataplat.transform.sandbox import sandbox
    from dataplat.transform.templating import TemplateError, check_select, render

    p = job.payload
    limit = min(max(int(p.get("limit", 500)), 1), 5000)
    try:
        layers = {n: m.layer for n, m in all_models(ctx).items()}
        r = render(p["sql"], layers)
        check_select(r.sql)
        with ctx.metadata.session() as s:
            relations = secured_relations(ctx, PolicyEngine(s, principal_of(p)), r.relations)
        with sandbox(relations) as sb:
            table = sb.query(f"select * from ({r.sql}) __q limit {limit + 1}")
    except (TemplateError, AccessDenied, LookupError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": error_message(e)}
    rows = table.slice(0, limit)
    return {
        "ok": True,
        "columns": [{"name": f.name, "type": str(f.type)} for f in rows.schema],
        "rows": [{k: _json_safe(v) for k, v in row.items()} for row in rows.to_pylist()],
        "truncated": table.num_rows > limit,
        "datasets": sorted(f"{layer}.{name}" for layer, name in r.relations.values()),
    }


def _json_safe(v: Any) -> Any:
    if v is None or isinstance(v, bool | int | float | str):
        return v
    return str(v)
