"""Worker job handlers: DQ rule runs and classification scans."""

from __future__ import annotations

import uuid
from typing import Any

from dataplat.core.context import PlatformContext
from dataplat.core.ports.orchestration import Job
from dataplat.core.ports.secrets import SecretStore
from dataplat.db.models import DqScorecard
from dataplat.orchestration.handlers import handler
from dataplat.quality.rules import RuleRunner


@handler("dq.run")
def run_rules(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    """Runs a scorecard's rules, given rules, or a dataset's (on-load) rules."""
    p = job.payload
    rule_ids = [uuid.UUID(r) for r in p.get("rule_ids", [])] or None
    if p.get("scorecard_id"):
        with ctx.metadata.session() as s:
            card = s.get(DqScorecard, uuid.UUID(p["scorecard_id"]))
            if card is None:
                return {"skipped": "scorecard deleted"}
            rule_ids = [uuid.UUID(r) for r in card.rule_ids]
        if not rule_ids:
            return {"skipped": "scorecard has no rules"}
    return RuleRunner(ctx).run(
        rule_ids,
        p.get("dataset"),
        trigger=p.get("trigger", "manual"),
        on_load_only=bool(p.get("on_load")),
    )


@handler("governance.classify")
def classify(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    """Suggests classifications for catalog columns (names, profiles, lineage)."""
    from dataplat.catalog.classify import suggest
    from dataplat.lineage.columns import column_upstream

    with ctx.metadata.session() as s:
        upstream = column_upstream(s, ctx.config.lake.model_dump())
        added = suggest(s, upstream=upstream)
    return {"suggested": len(added), "suggestions": added[:50]}
