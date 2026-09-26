"""Job handlers, keyed by job kind. Later phases register ingestion, vault loads, etc."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dataplat.core.context import PlatformContext
from dataplat.core.ports.orchestration import Job
from dataplat.core.ports.secrets import SecretStore
from dataplat.core.secrets import SecretNotFound

# (ctx, job, secrets scoped to the job's service account or None) -> result
Handler = Callable[[PlatformContext, Job, SecretStore | None], dict[str, Any]]

HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return register


@handler("platform.echo")
def echo(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    if job.payload.get("fail"):
        raise RuntimeError("echo asked to fail")
    return {"echo": job.payload}


@handler("platform.healthcheck")
def healthcheck(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    return {
        "metadata_store": ctx.metadata.health(),
        "object_store": ctx.objects.health(),
        "event_bus": ctx.events.health(),
        "knowledge_graph": ctx.knowledge_graph.health(),
    }


@handler("platform.secret_probe")
def secret_probe(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    """Checks that a job can read a secret through its service account — without returning it."""
    if secrets is None:
        raise PermissionError("job has no service account")
    try:
        value = secrets.resolve(job.payload["ref"])
    except SecretNotFound:
        return {"readable": False, "reason": "not_found"}
    except PermissionError:
        return {"readable": False, "reason": "forbidden"}
    return {"readable": True, "length": len(value)}


# Domain handlers register themselves on import.
from dataplat.ingestion import handlers as _ingestion_handlers  # noqa: E402, F401
from dataplat.transform import handlers as _transform_handlers  # noqa: E402, F401
from dataplat.vault import handlers as _vault_handlers  # noqa: E402, F401
