"""Worker job handlers for the Data Vault."""

from __future__ import annotations

from typing import Any

from dataplat.core.context import PlatformContext
from dataplat.core.ports.orchestration import Job
from dataplat.core.ports.secrets import SecretStore
from dataplat.orchestration.handlers import handler
from dataplat.transform.triggers import notify_updated
from dataplat.vault.loader import VaultLoader


@handler("vault.load")
def vault_load(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    p = job.payload
    return VaultLoader(ctx).load_source(p.get("layer", "bronze"), p["dataset"], trigger=p.get("trigger", "manual"))


@handler("vault.build")
def vault_build(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    result = VaultLoader(ctx).build(job.payload["name"], trigger=job.payload.get("trigger", "manual"))
    if result.get("rows") is not None:
        result["queued"] = notify_updated(ctx, [f"vault.{job.payload['name']}"], trigger="vault_build")
    return result
