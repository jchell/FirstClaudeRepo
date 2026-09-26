"""Data quality rules: definitions, SQL, evaluation, scores and scorecards.

Each rule counts the rows it checks and the rows that fail, over the dataset's
current Delta version, in the locked-down sandbox (relation ``data``; referential
rules also get ``ref``). Literal parameters are bound, never spliced into SQL.

  score   = 100 x passing rows / checked rows (100 when nothing is checked)
  passed  = passing share >= the rule's threshold
  scorecard score = mean of its rules' scores, critical rules counting double;
  dimension scores = the same mean per DQ dimension.

A scorecard is degraded when its score falls more than ``degradation_pct`` percent
below the mean of its previous ``baseline_runs`` scores; that opens an alert.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from dataplat.core.context import PlatformContext
from dataplat.db.models import Dataset, DqResult, DqRule, DqScorecard, DqScorecardResult
from dataplat.transform.output import error_message
from dataplat.transform.sandbox import sandbox
from dataplat.transform.templating import TemplateError, check_select
from dataplat.vault.sql import q

log = logging.getLogger(__name__)

RuleType = Literal[
    "not_null", "unique", "range", "regex", "allowed_values", "referential", "freshness", "row_count", "custom_sql"
]
DIMENSIONS = ("completeness", "uniqueness", "validity", "consistency", "timeliness", "accuracy")
DEFAULT_DIMENSION = {
    "not_null": "completeness",
    "unique": "uniqueness",
    "range": "validity",
    "regex": "validity",
    "allowed_values": "validity",
    "referential": "consistency",
    "freshness": "timeliness",
    "row_count": "completeness",
    "custom_sql": "accuracy",
}
NEEDS_COLUMN = {"not_null", "range", "regex", "allowed_values", "referential"}
SAMPLE = 5


class RuleParams(BaseModel):
    """Parameters by rule type (unused ones are ignored)."""

    columns: list[str] = []  # unique: the key columns (default: the rule's column)
    min: float | str | None = None  # range, row_count
    max: float | str | None = None
    pattern: str | None = None  # regex (full match)
    values: list[Any] = []  # allowed_values
    ref_dataset: str | None = Field(default=None, pattern=r"^(bronze|silver|gold|vault)\.[a-z][a-z0-9_]*$")
    ref_column: str | None = None
    max_age_minutes: int | None = Field(default=None, ge=1)  # freshness
    timestamp_column: str | None = None  # freshness (default _load_ts)
    sql: str | None = Field(default=None, max_length=20_000)  # custom_sql: SELECT of failing rows from data

    @field_validator("pattern")
    @classmethod
    def _regex(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"invalid pattern: {e}") from e
        return v


class RuleSpec(BaseModel):
    rule_type: RuleType
    column: str | None = None
    params: RuleParams = RuleParams()

    @model_validator(mode="after")
    def _check(self) -> RuleSpec:
        t, p = self.rule_type, self.params
        if t in NEEDS_COLUMN and not self.column:
            raise ValueError(f"{t} rules check a column")
        if t == "range" and p.min is None and p.max is None:
            raise ValueError("range rules need a min and/or max")
        if t == "regex" and not p.pattern:
            raise ValueError("regex rules need a pattern")
        if t == "allowed_values" and not p.values:
            raise ValueError("allowed_values rules need the allowed values")
        if t == "referential" and not (p.ref_dataset and p.ref_column):
            raise ValueError("referential rules need the referenced dataset and column")
        if t == "freshness" and not p.max_age_minutes:
            raise ValueError("freshness rules need max_age_minutes")
        if t == "row_count" and p.min is None and p.max is None:
            raise ValueError("row_count rules need a min and/or max")
        if t == "unique" and not (p.columns or self.column):
            raise ValueError("unique rules need the key column(s)")
        if t == "custom_sql":
            if not p.sql:
                raise ValueError("custom SQL rules need a query returning the failing rows")
            try:
                check_select(p.sql)
            except TemplateError as e:
                raise ValueError(str(e)) from e
        return self


def compile_rule(spec: RuleSpec, columns: set[str]) -> tuple[str, list[Any], str | None]:
    """(SQL returning checked, failed; parameters; SQL for a sample of failing values)."""
    t, p = spec.rule_type, spec.params

    def col(name: str | None) -> str:
        if not name or name not in columns:
            raise ValueError(f"the dataset has no column {name!r}")
        return q(name)

    c = col(spec.column) if spec.column and t != "freshness" else None
    if t == "not_null":
        return f"select count(*), count(*) filter (where {c} is null) from data", [], None
    if t == "unique":
        keys = ", ".join(col(k) for k in (p.columns or [spec.column]))
        return (
            f"select (select count(*) from data), coalesce(sum(n), 0) from "
            f"(select count(*) as n from data group by {keys} having count(*) > 1)",
            [],
            f"select {keys} from data group by {keys} having count(*) > 1 limit {SAMPLE}",
        )
    if t == "range":
        conds, params = [], []
        if p.min is not None:
            conds.append(f"{c} < ?")
            params.append(p.min)
        if p.max is not None:
            conds.append(f"{c} > ?")
            params.append(p.max)
        bad = " or ".join(conds)
        return (
            f"select count({c}), count(*) filter (where {bad}) from data",
            params,
            f"select {c} from data where {bad} limit {SAMPLE}",
        )
    if t == "regex":
        bad = f"not regexp_full_match(cast({c} as varchar), ?)"
        return (
            f"select count({c}), count(*) filter (where {c} is not null and {bad}) from data",
            [p.pattern],
            f"select {c} from data where {c} is not null and {bad} limit {SAMPLE}",
        )
    if t == "allowed_values":
        values = [str(v) for v in p.values]
        bad = f"cast({c} as varchar) not in (select unnest(?::varchar[]))"
        return (
            f"select count({c}), count(*) filter (where {c} is not null and {bad}) from data",
            [values],
            f"select {c} from data where {c} is not null and {bad} limit {SAMPLE}",
        )
    if t == "referential":
        rc = q(p.ref_column or "")
        bad = f"cast({c} as varchar) not in (select cast({rc} as varchar) from ref where {rc} is not null)"
        return (
            f"select count({c}), count(*) filter (where {c} is not null and {bad}) from data",
            [],
            f"select {c} from data where {c} is not null and {bad} limit {SAMPLE}",
        )
    if t == "freshness":
        ts = col(p.timestamp_column or "_load_ts")
        return (
            f"select 1, case when max({ts}) is null or "
            f"max({ts})::timestamptz < now() - (? * interval '1 minute') then 1 else 0 end from data",
            [p.max_age_minutes],
            f"select max({ts}) from data",
        )
    if t == "row_count":
        conds, params = [], []
        if p.min is not None:
            conds.append("n < ?")
            params.append(p.min)
        if p.max is not None:
            conds.append("n > ?")
            params.append(p.max)
        return (
            f"select 1, case when {' or '.join(conds)} then 1 else 0 end from (select count(*) as n from data)",
            params,
            "select count(*) from data",
        )
    # custom_sql: the steward's query returns the failing rows of ``data``
    return (
        f"select (select count(*) from data), (select count(*) from ({p.sql}) failing)",
        [],
        f"select * from ({p.sql}) failing limit {SAMPLE}",
    )


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, bool | int | float | str):
        return v
    return str(v)


class RuleRunner:
    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx

    def evaluate(self, rule: DqRule, dataset: Dataset, run_id: uuid.UUID, trigger: str) -> DqResult:
        spec = RuleSpec(rule_type=rule.rule_type, column=rule.column, params=RuleParams.model_validate(rule.params))
        res = DqResult(rule_id=rule.id, run_id=run_id, trigger=trigger[:256], ts=datetime.now(UTC))
        tf = self.ctx.tables
        try:
            if not tf.exists(dataset.uri):
                raise ValueError(f"{dataset.layer}.{dataset.name} has no data")
            data = tf.dataset(dataset.uri)
            relations: dict[str, Any] = {"data": data}
            if spec.rule_type == "referential":
                layer, name = spec.params.ref_dataset.split(".", 1)  # type: ignore[union-attr]
                ref_uri = self.ctx.config.lake.uri(layer, name)
                if not tf.exists(ref_uri):
                    raise ValueError(f"{spec.params.ref_dataset} has no data")
                relations["ref"] = tf.dataset(ref_uri)
            sql, params, sample_sql = compile_rule(spec, set(data.schema.names))
            with sandbox(relations) as sb:
                checked, failed = sb.conn.execute(sql, params).fetchone()
                sample: list[Any] = []
                if failed and sample_sql:
                    rows = sb.conn.execute(sample_sql, params).fetchall()
                    sample = [_jsonable(r[0]) if len(r) == 1 else [_jsonable(x) for x in r] for r in rows[:SAMPLE]]
            res.rows_checked, res.rows_failed = int(checked or 0), int(failed or 0)
            ratio = 1.0 if not res.rows_checked else (res.rows_checked - res.rows_failed) / res.rows_checked
            res.score = round(100 * ratio, 2)
            res.passed = ratio >= rule.threshold
            res.sample = sample if spec.rule_type != "custom_sql" else sample[:SAMPLE]
            res.table_version = dataset.table_version
        except Exception as e:
            res.error = error_message(e)[:2000]
            res.passed, res.score = False, None
        return res

    def run(
        self,
        rule_ids: list[uuid.UUID] | None = None,
        dataset_ref: str | None = None,
        *,
        trigger: str = "manual",
        on_load_only: bool = False,
    ) -> dict[str, Any]:
        """Evaluates rules (by id, or all enabled rules of a dataset) and stores results."""
        from dataplat.orchestration.alerts import open_alert, resolve_alert

        run_id = uuid.uuid4()
        with self.ctx.metadata.session() as s:
            q_ = select(DqRule).where(DqRule.enabled)
            if rule_ids is not None:
                q_ = q_.where(DqRule.id.in_(rule_ids))
            if dataset_ref:
                layer, name = dataset_ref.split(".", 1)
                q_ = q_.join(Dataset, Dataset.id == DqRule.dataset_id).where(
                    Dataset.layer == layer, Dataset.name == name
                )
            if on_load_only:
                q_ = q_.where(DqRule.run_on_load)
            rules = list(s.scalars(q_))
            results = []
            for rule in rules:
                ds = s.get(Dataset, rule.dataset_id)
                r = self.evaluate(rule, ds, run_id, trigger)
                s.add(r)
                results.append((rule, r))
            s.flush()
            summary = [
                {
                    "rule": rule.name,
                    "passed": r.passed,
                    "score": r.score,
                    "rows_checked": r.rows_checked,
                    "rows_failed": r.rows_failed,
                    "error": r.error,
                }
                for rule, r in results
            ]
            alerts = [(rule.name, rule.severity, r) for rule, r in results]
        for name, severity, r in alerts:
            target = f"rule:{name}"
            if r.passed:
                resolve_alert(self.ctx, "dq_failed", target)
            elif severity == "critical" or r.error:
                detail = r.error or f"{r.rows_failed} of {r.rows_checked} rows failed (score {r.score})"
                open_alert(
                    self.ctx,
                    "dq_failed",
                    target,
                    f"DQ rule {name} failed: {detail}",
                    severity="serious" if severity == "critical" else "warning",
                    details={"rule": name, "score": r.score},
                )
        cards = self.score_scorecards({rule.id for rule, _ in results}, run_id) if results else []
        return {"run_id": str(run_id), "rules": summary, "scorecards": cards}

    # ------------------------------------------------------------------ scorecards

    def score_scorecards(self, rule_ids: set[uuid.UUID], run_id: uuid.UUID) -> list[dict[str, Any]]:
        from dataplat.orchestration.alerts import open_alert, resolve_alert

        out = []
        with self.ctx.metadata.session() as s:
            cards = [c for c in s.scalars(select(DqScorecard)) if {uuid.UUID(i) for i in c.rule_ids} & rule_ids]
            for card in cards:
                score, dims = scorecard_score(s, [uuid.UUID(i) for i in card.rule_ids])
                previous = [
                    r.score
                    for r in s.scalars(
                        select(DqScorecardResult)
                        .where(DqScorecardResult.scorecard_id == card.id, DqScorecardResult.score.is_not(None))
                        .order_by(DqScorecardResult.ts.desc())
                        .limit(card.baseline_runs)
                    )
                ]
                baseline = round(sum(previous) / len(previous), 2) if previous else None
                degraded = bool(score is not None and baseline and score < baseline * (1 - card.degradation_pct / 100))
                s.add(
                    DqScorecardResult(
                        scorecard_id=card.id,
                        run_id=run_id,
                        score=score,
                        dimensions=dims,
                        baseline=baseline,
                        degraded=degraded,
                        ts=datetime.now(UTC),
                    )
                )
                out.append(
                    {
                        "scorecard": card.name,
                        "score": score,
                        "baseline": baseline,
                        "degraded": degraded,
                        "dimensions": dims,
                    }
                )
        for c in out:
            target = f"scorecard:{c['scorecard']}"
            if c["degraded"]:
                drop = round(100 * (1 - c["score"] / c["baseline"]), 1)
                open_alert(
                    self.ctx,
                    "dq_degradation",
                    target,
                    f"DQ scorecard {c['scorecard']} dropped {drop}% to {c['score']} (baseline {c['baseline']})",
                    severity="serious",
                    details=c,
                )
            elif c["score"] is not None:
                resolve_alert(self.ctx, "dq_degradation", target)
        return out


def scorecard_score(s: Any, rule_ids: list[uuid.UUID]) -> tuple[float | None, dict[str, float]]:
    """Weighted mean of each rule's latest score, overall and per dimension."""
    total = weight = 0.0
    by_dim: dict[str, list[float]] = {}
    for rule in s.scalars(select(DqRule).where(DqRule.id.in_(rule_ids), DqRule.enabled)):
        latest = s.scalars(
            select(DqResult).where(DqResult.rule_id == rule.id).order_by(DqResult.ts.desc()).limit(1)
        ).first()
        if latest is None:
            continue
        score = latest.score if latest.score is not None else 0.0  # an erroring rule scores 0
        w = 2.0 if rule.severity == "critical" else 1.0
        total += w * score
        weight += w
        by_dim.setdefault(rule.dimension, []).extend([score] * int(w))
    if not weight:
        return None, {}
    return round(total / weight, 2), {d: round(sum(v) / len(v), 2) for d, v in by_dim.items()}
