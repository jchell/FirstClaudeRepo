"""Data quality: steward rules, results (time series), scorecards and their schedules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dataplat.adapters.pg_queue import COALESCE_PREFIX, DuplicateJob
from dataplat.api.deps import client_ip, get_ctx, get_policy, get_session, require_roles
from dataplat.core import audit
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.db.models import (
    Alert,
    Dataset,
    DatasetColumn,
    DqResult,
    DqRule,
    DqScorecard,
    DqScorecardResult,
    Schedule,
)
from dataplat.quality.rules import DEFAULT_DIMENSION, DIMENSIONS, RuleParams, RuleSpec, RuleType
from dataplat.quality.summary import latest_results
from dataplat.security.policy import PolicyEngine, mask_value

router = APIRouter(prefix="/api/quality", tags=["quality"])
steward = require_roles("steward", "engineer")
reader = require_roles("steward", "engineer", "analyst", "viewer")


class RuleIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    dataset_id: uuid.UUID
    column: str | None = None
    rule_type: RuleType
    params: dict[str, Any] = {}
    dimension: Literal[DIMENSIONS] | None = None  # type: ignore[valid-type]
    severity: Literal["warning", "critical"] = "warning"
    threshold: float = Field(default=1.0, ge=0, le=1)
    run_on_load: bool = False
    enabled: bool = True
    description: str = ""


class RuleOut(BaseModel):
    id: uuid.UUID
    name: str
    dataset_id: uuid.UUID
    dataset: str
    column: str | None
    rule_type: str
    params: dict[str, Any]
    dimension: str
    severity: str
    threshold: float
    run_on_load: bool
    enabled: bool
    description: str
    owner: str | None
    last: dict[str, Any] | None


def _masked_sample(r: DqResult, rule: DqRule, ds: Dataset, policy: PolicyEngine) -> list[Any]:
    masks = policy.for_dataset(ds).masks
    if not masks or not r.sample:
        return r.sample or []
    if rule.column and rule.column in masks and rule.rule_type != "custom_sql":
        return [mask_value(v, masks[rule.column]) for v in r.sample]
    if rule.rule_type in ("custom_sql", "unique"):
        return ["****" for _ in r.sample]  # rows may include masked columns
    return r.sample


def _result(r: DqResult, rule: DqRule, ds: Dataset, policy: PolicyEngine) -> dict[str, Any]:
    return {
        "ts": r.ts,
        "run_id": r.run_id,
        "trigger": r.trigger,
        "rows_checked": r.rows_checked,
        "rows_failed": r.rows_failed,
        "score": r.score,
        "passed": r.passed,
        "error": r.error,
        "sample": _masked_sample(r, rule, ds, policy),
    }


def _rule_out(rule: DqRule, ds: Dataset, last: DqResult | None, policy: PolicyEngine) -> RuleOut:
    return RuleOut(
        id=rule.id,
        name=rule.name,
        dataset_id=ds.id,
        dataset=f"{ds.layer}.{ds.name}",
        column=rule.column,
        rule_type=rule.rule_type,
        params=rule.params,
        dimension=rule.dimension,
        severity=rule.severity,
        threshold=rule.threshold,
        run_on_load=rule.run_on_load,
        enabled=rule.enabled,
        description=rule.description,
        owner=rule.owner,
        last=_result(last, rule, ds, policy) if last else None,
    )


def _validated(s: Session, body: RuleIn) -> tuple[Dataset, RuleSpec]:
    ds = s.get(Dataset, body.dataset_id)
    if ds is None:
        raise HTTPException(422, "dataset not found")
    try:
        spec = RuleSpec(rule_type=body.rule_type, column=body.column, params=RuleParams.model_validate(body.params))
    except (ValidationError, ValueError) as e:
        msg = "; ".join(err["msg"] for err in e.errors()) if isinstance(e, ValidationError) else str(e)
        raise HTTPException(422, msg) from e
    cols = {
        c.name
        for c in s.scalars(
            select(DatasetColumn).where(DatasetColumn.dataset_id == ds.id, DatasetColumn.removed_at.is_(None))
        )
    }
    needed = [c for c in (body.column, *spec.params.columns, spec.params.timestamp_column) if c]
    if spec.rule_type == "freshness" and not spec.params.timestamp_column:
        needed.append("_load_ts")
    if missing := [c for c in needed if c not in cols]:
        raise HTTPException(422, f"{ds.layer}.{ds.name} has no column(s) {missing}")
    if spec.params.ref_dataset:
        layer, name = spec.params.ref_dataset.split(".", 1)
        if not s.scalars(select(Dataset).where(Dataset.layer == layer, Dataset.name == name)).first():
            raise HTTPException(422, f"{spec.params.ref_dataset} is not in the catalog")
    return ds, spec


@router.get("/rules", response_model=list[RuleOut])
def list_rules(
    dataset_id: uuid.UUID | None = None,
    s: Session = Depends(get_session, scope="function"),
    policy: PolicyEngine = Depends(get_policy),
    _: Principal = Depends(reader),
):
    q = select(DqRule, Dataset).join(Dataset, Dataset.id == DqRule.dataset_id).order_by(Dataset.name, DqRule.name)
    if dataset_id:
        q = q.where(DqRule.dataset_id == dataset_id)
    rows = [(r, d) for r, d in s.execute(q) if policy.can_read(d)]
    latest = latest_results(s, [r.id for r, _ in rows])
    return [_rule_out(r, d, latest.get(r.id), policy) for r, d in rows]


@router.post("/rules", response_model=RuleOut, status_code=201)
def create_rule(
    body: RuleIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    policy: PolicyEngine = Depends(get_policy),
    actor: Principal = Depends(steward),
):
    ds, spec = _validated(s, body)
    rule = DqRule(
        name=body.name,
        dataset_id=ds.id,
        column=body.column,
        rule_type=body.rule_type,
        params=spec.params.model_dump(exclude_defaults=True),
        dimension=body.dimension or DEFAULT_DIMENSION[body.rule_type],
        severity=body.severity,
        threshold=body.threshold,
        run_on_load=body.run_on_load,
        enabled=body.enabled,
        description=body.description,
        owner=actor.name,
        created_by=actor.name,
    )
    s.add(rule)
    try:
        s.flush()
    except IntegrityError as e:
        raise HTTPException(409, "a rule with that name exists") from e
    audit.record(s, actor=actor.name, action="dq.rule.create", target=rule.name, ip=client_ip(request))
    return _rule_out(rule, ds, None, policy)


@router.put("/rules/{rule_id}", response_model=RuleOut)
def update_rule(
    rule_id: uuid.UUID,
    body: RuleIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    policy: PolicyEngine = Depends(get_policy),
    actor: Principal = Depends(steward),
):
    rule = s.get(DqRule, rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    ds, spec = _validated(s, body)
    rule.name, rule.dataset_id, rule.column, rule.rule_type = body.name, ds.id, body.column, body.rule_type
    rule.params = spec.params.model_dump(exclude_defaults=True)
    rule.dimension = body.dimension or DEFAULT_DIMENSION[body.rule_type]
    rule.severity, rule.threshold, rule.run_on_load = body.severity, body.threshold, body.run_on_load
    rule.enabled, rule.description = body.enabled, body.description
    audit.record(s, actor=actor.name, action="dq.rule.update", target=rule.name, ip=client_ip(request))
    s.flush()
    return _rule_out(rule, ds, latest_results(s, [rule.id]).get(rule.id), policy)


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(
    rule_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    rule = s.get(DqRule, rule_id)
    if rule is None:
        raise HTTPException(404, "rule not found")
    for card in s.scalars(select(DqScorecard)):
        if str(rule_id) in card.rule_ids:
            card.rule_ids = [r for r in card.rule_ids if r != str(rule_id)]
    s.delete(rule)
    audit.record(s, actor=actor.name, action="dq.rule.delete", target=rule.name, ip=client_ip(request))


@router.get("/rules/{rule_id}/results")
def rule_results(
    rule_id: uuid.UUID,
    days: int = 30,
    s: Session = Depends(get_session, scope="function"),
    policy: PolicyEngine = Depends(get_policy),
    _: Principal = Depends(reader),
) -> list[dict[str, Any]]:
    rule = s.get(DqRule, rule_id)
    ds = s.get(Dataset, rule.dataset_id) if rule else None
    if rule is None or not policy.can_read(ds):
        raise HTTPException(404, "rule not found")
    since = datetime.now(UTC) - timedelta(days=min(max(days, 1), 365))
    rows = s.scalars(select(DqResult).where(DqResult.rule_id == rule_id, DqResult.ts >= since).order_by(DqResult.ts))
    return [_result(r, rule, ds, policy) for r in rows]


def _queue(ctx: PlatformContext, payload: dict[str, Any], key: str) -> dict[str, Any]:
    try:
        return {"queued": True, "task_id": ctx.jobs.enqueue("dq.run", payload, dedupe_key=f"{COALESCE_PREFIX}{key}")}
    except DuplicateJob:
        return {"queued": False, "detail": "a run is already waiting"}


class RunIn(BaseModel):
    rule_ids: list[uuid.UUID] = []
    dataset_id: uuid.UUID | None = None


@router.post("/run", status_code=202)
def run_rules(
    body: RunIn,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    _: Principal = Depends(steward),
):
    if body.dataset_id:
        ds = s.get(Dataset, body.dataset_id)
        if ds is None:
            raise HTTPException(404, "dataset not found")
        return _queue(ctx, {"dataset": f"{ds.layer}.{ds.name}", "trigger": "manual"}, f"dq:{ds.layer}.{ds.name}")
    if not body.rule_ids:
        raise HTTPException(422, "choose rules or a dataset")
    ids = sorted(str(r) for r in body.rule_ids)
    return _queue(ctx, {"rule_ids": ids, "trigger": "manual"}, f"dq-rules:{','.join(ids)[:200]}")


# ---------------------------------------------------------------- scorecards


class ScheduleIn(BaseModel):
    type: Literal["none", "cron", "interval"] = "none"
    cron: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60)

    @model_validator(mode="after")
    def _check(self) -> ScheduleIn:
        if self.type == "cron":
            if not self.cron:
                raise ValueError("cron schedules need a cron expression")
            CronTrigger.from_crontab(self.cron)
        if self.type == "interval" and not self.interval_seconds:
            raise ValueError("interval schedules need interval_seconds")
        return self


class ScorecardIn(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]{1,127}$")
    description: str = ""
    rule_ids: list[uuid.UUID] = Field(min_length=1)
    schedule: ScheduleIn = ScheduleIn()
    degradation_pct: float = Field(default=10, gt=0, le=100)
    baseline_runs: int = Field(default=7, ge=1, le=100)


def _sync_schedule(s: Session, card: DqScorecard) -> None:
    sch = s.scalars(select(Schedule).where(Schedule.name == f"dq:{card.id}")).first()
    kind = (card.schedule or {}).get("type", "none")
    if kind == "none":
        if sch is not None:
            s.delete(sch)
        return
    if sch is None:
        sch = Schedule(name=f"dq:{card.id}", kind="dq.run")
        s.add(sch)
    sch.payload = {"scorecard_id": str(card.id), "trigger": "schedule"}
    sch.cron = card.schedule.get("cron") if kind == "cron" else None
    sch.interval_seconds = card.schedule.get("interval_seconds") if kind == "interval" else None
    sch.enabled = True
    sch.next_run_at = None


def _card_out(s: Session, card: DqScorecard, policy: PolicyEngine, history_days: int = 0) -> dict[str, Any]:
    rows = list(
        s.execute(
            select(DqRule, Dataset)
            .join(Dataset, Dataset.id == DqRule.dataset_id)
            .where(DqRule.id.in_([uuid.UUID(r) for r in card.rule_ids]))
        )
    )
    latest = latest_results(s, [r.id for r, _ in rows])
    last = s.scalars(
        select(DqScorecardResult).where(DqScorecardResult.scorecard_id == card.id).order_by(DqScorecardResult.ts.desc())
    ).first()
    sch = s.scalars(select(Schedule).where(Schedule.name == f"dq:{card.id}")).first()
    out: dict[str, Any] = {
        "id": card.id,
        "name": card.name,
        "description": card.description,
        "rule_ids": card.rule_ids,
        "rules": [_rule_out(r, d, latest.get(r.id), policy).model_dump() for r, d in rows if policy.can_read(d)],
        "schedule": card.schedule or {"type": "none"},
        "next_run_at": sch.next_run_at if sch else None,
        "degradation_pct": card.degradation_pct,
        "baseline_runs": card.baseline_runs,
        "owner": card.owner,
        "score": last.score if last else None,
        "baseline": last.baseline if last else None,
        "degraded": last.degraded if last else False,
        "dimensions": last.dimensions if last else {},
        "last_run_at": last.ts if last else None,
    }
    if history_days:
        since = datetime.now(UTC) - timedelta(days=history_days)
        out["history"] = [
            {"ts": h.ts, "score": h.score, "baseline": h.baseline, "degraded": h.degraded, "dimensions": h.dimensions}
            for h in s.scalars(
                select(DqScorecardResult)
                .where(DqScorecardResult.scorecard_id == card.id, DqScorecardResult.ts >= since)
                .order_by(DqScorecardResult.ts)
            )
        ]
    return out


@router.get("/scorecards")
def list_scorecards(
    s: Session = Depends(get_session, scope="function"),
    policy: PolicyEngine = Depends(get_policy),
    _: Principal = Depends(reader),
):
    return [_card_out(s, c, policy) for c in s.scalars(select(DqScorecard).order_by(DqScorecard.name))]


@router.get("/scorecards/{card_id}")
def get_scorecard(
    card_id: uuid.UUID,
    days: int = 30,
    s: Session = Depends(get_session, scope="function"),
    policy: PolicyEngine = Depends(get_policy),
    _: Principal = Depends(reader),
):
    card = s.get(DqScorecard, card_id)
    if card is None:
        raise HTTPException(404, "scorecard not found")
    return _card_out(s, card, policy, history_days=min(max(days, 1), 365))


def _check_rules(s: Session, ids: list[uuid.UUID]) -> list[str]:
    found = {r.id for r in s.scalars(select(DqRule).where(DqRule.id.in_(ids)))}
    if missing := [str(i) for i in ids if i not in found]:
        raise HTTPException(422, f"unknown rules {missing}")
    return [str(i) for i in dict.fromkeys(ids)]


@router.post("/scorecards", status_code=201)
def create_scorecard(
    body: ScorecardIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    policy: PolicyEngine = Depends(get_policy),
    actor: Principal = Depends(steward),
):
    card = DqScorecard(
        name=body.name,
        description=body.description,
        rule_ids=_check_rules(s, body.rule_ids),
        schedule=body.schedule.model_dump(exclude_none=True),
        degradation_pct=body.degradation_pct,
        baseline_runs=body.baseline_runs,
        owner=actor.name,
        created_by=actor.name,
    )
    s.add(card)
    try:
        s.flush()
    except IntegrityError as e:
        raise HTTPException(409, "a scorecard with that name exists") from e
    _sync_schedule(s, card)
    audit.record(s, actor=actor.name, action="dq.scorecard.create", target=card.name, ip=client_ip(request))
    s.flush()
    return _card_out(s, card, policy)


@router.put("/scorecards/{card_id}")
def update_scorecard(
    card_id: uuid.UUID,
    body: ScorecardIn,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    policy: PolicyEngine = Depends(get_policy),
    actor: Principal = Depends(steward),
):
    card = s.get(DqScorecard, card_id)
    if card is None:
        raise HTTPException(404, "scorecard not found")
    card.name, card.description = body.name, body.description
    card.rule_ids = _check_rules(s, body.rule_ids)
    card.schedule = body.schedule.model_dump(exclude_none=True)
    card.degradation_pct, card.baseline_runs = body.degradation_pct, body.baseline_runs
    _sync_schedule(s, card)
    audit.record(s, actor=actor.name, action="dq.scorecard.update", target=card.name, ip=client_ip(request))
    s.flush()
    return _card_out(s, card, policy)


@router.delete("/scorecards/{card_id}", status_code=204)
def delete_scorecard(
    card_id: uuid.UUID,
    request: Request,
    s: Session = Depends(get_session, scope="function"),
    actor: Principal = Depends(steward),
):
    card = s.get(DqScorecard, card_id)
    if card is None:
        raise HTTPException(404, "scorecard not found")
    card.schedule = {"type": "none"}
    _sync_schedule(s, card)
    s.delete(card)
    audit.record(s, actor=actor.name, action="dq.scorecard.delete", target=card.name, ip=client_ip(request))


@router.post("/scorecards/{card_id}/run", status_code=202)
def run_scorecard(
    card_id: uuid.UUID,
    s: Session = Depends(get_session, scope="function"),
    ctx: PlatformContext = Depends(get_ctx),
    _: Principal = Depends(steward),
):
    if s.get(DqScorecard, card_id) is None:
        raise HTTPException(404, "scorecard not found")
    return _queue(ctx, {"scorecard_id": str(card_id), "trigger": "manual"}, f"dq-card:{card_id}")


@router.get("/summary")
def summary(
    s: Session = Depends(get_session, scope="function"),
    policy: PolicyEngine = Depends(get_policy),
    _: Principal = Depends(reader),
) -> dict[str, Any]:
    rules = [
        (r, d)
        for r, d in s.execute(select(DqRule, Dataset).join(Dataset, Dataset.id == DqRule.dataset_id))
        if policy.can_read(d)
    ]
    latest = latest_results(s, [r.id for r, _ in rules])
    alerts = s.scalars(
        select(Alert)
        .where(Alert.kind.in_(["dq_failed", "dq_degradation"]), Alert.resolved_at.is_(None))
        .order_by(Alert.opened_at.desc())
    )
    return {
        "rules": len(rules),
        "passing": sum(1 for r, _ in rules if r.id in latest and latest[r.id].passed),
        "failing": sum(1 for r, _ in rules if r.id in latest and not latest[r.id].passed),
        "unevaluated": sum(1 for r, _ in rules if r.id not in latest),
        "alerts": [
            {
                "id": a.id,
                "kind": a.kind,
                "severity": a.severity,
                "target": a.target,
                "message": a.message,
                "opened_at": a.opened_at,
            }
            for a in alerts
        ],
    }
