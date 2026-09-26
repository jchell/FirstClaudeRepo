"""Model and pipeline definitions: validation and versioning, shared by the API and wizard."""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dataplat.catalog.service import AUDIT_COLUMNS
from dataplat.db.models import Pipeline, Schedule, TransformModel, TransformModelVersion
from dataplat.transform import models as mdl
from dataplat.transform.runner import ModelSpec, dependencies
from dataplat.transform.templating import TemplateError, check_select, render

MODEL_NAME = re.compile(mdl.MODEL_NAME)


class DefinitionError(ValueError):
    pass


def validate_model(s: Session, name: str, layer: str, kind: str, sql: str, config: dict[str, Any]) -> dict[str, Any]:
    if not MODEL_NAME.match(name):
        raise DefinitionError("model names are lower case letters, digits and underscores")
    if layer not in ("silver", "gold"):
        raise DefinitionError("models write to silver or gold")
    try:
        config = mdl.validate_config(kind, config)
    except ValueError as e:
        raise DefinitionError(str(e)) from e
    if kind in ("sql", "fact"):
        layers = {m.name: m.layer for m in s.scalars(select(TransformModel))}
        layers[name] = layer
        try:
            r = render(sql, layers, incremental=True, this=(layer, name))
            check_select(r.sql)
            check_select(render(sql, layers, incremental=False, this=(layer, name)).sql)
        except TemplateError as e:
            raise DefinitionError(str(e)) from e
        if (layer, name) in r.relations.values():
            raise DefinitionError("a model can't read itself; use {{ this }} in an incremental model")
        if config.get("unique_key") and config.get("materialized") != "incremental":
            raise DefinitionError("unique_key applies to incremental models")
    elif sql.strip():
        raise DefinitionError(f"{kind} models are configured, not written in SQL")
    return config


def save_model(
    s: Session,
    *,
    name: str,
    layer: str,
    kind: str,
    sql: str,
    config: dict[str, Any],
    description: str = "",
    user: str | None = None,
    existing: TransformModel | None = None,
) -> TransformModel:
    config = validate_model(s, name, layer, kind, sql, config)
    m = existing
    if m is None:
        if s.scalars(select(TransformModel).where(TransformModel.name == name)).first():
            raise DefinitionError(f"model {name} already exists")
        m = TransformModel(
            name=name, layer=layer, kind=kind, sql=sql, config=config, version=1, created_by=user, owner=user
        )
        m.description = description
        s.add(m)
        s.flush()
    else:
        if (m.layer, m.kind) != (layer, kind):
            raise DefinitionError("a model's layer and kind can't change; create a new model")
        m.description = description
        if (m.sql, m.config) == (sql, config):
            return m
        m.version += 1
        m.sql, m.config = sql, config
    s.add(TransformModelVersion(model_id=m.id, version=m.version, kind=kind, sql=sql, config=config, created_by=user))
    s.flush()
    models = {x.name: ModelSpec.of(x) for x in s.scalars(select(TransformModel))}
    # Reject a definition that closes a dependency cycle.
    from graphlib import CycleError, TopologicalSorter

    try:
        TopologicalSorter({n: dependencies(spec, models) for n, spec in models.items()}).prepare()
    except CycleError as e:
        raise DefinitionError(f"this would create a dependency cycle: {' -> '.join(e.args[1])}") from e
    return m


def sync_pipeline_schedule(s: Session, p: Pipeline) -> None:
    sch = s.scalars(select(Schedule).where(Schedule.name == f"pipeline:{p.id}")).first()
    kind = (p.schedule or {}).get("type", "none")
    if kind == "none" or not p.enabled:
        if sch is not None:
            s.delete(sch)
        return
    if sch is None:
        sch = Schedule(name=f"pipeline:{p.id}", kind="pipeline.run")
        s.add(sch)
    sch.payload = {"pipeline_id": str(p.id), "trigger": "schedule"}
    sch.cron = p.schedule.get("cron") if kind == "cron" else None
    sch.interval_seconds = p.schedule.get("interval_seconds") if kind == "interval" else None
    sch.enabled = True
    sch.next_run_at = None


def ensure_promotion(s: Session, dataset: str, job_id: uuid.UUID, user: str | None) -> TransformModel:
    """The wizard's "promote to silver": a silver copy without bronze audit columns."""
    sql = f"select * exclude ({', '.join(AUDIT_COLUMNS)}) from {{{{ source('bronze', '{dataset}') }}}}"
    m = s.scalars(select(TransformModel).where(TransformModel.name == dataset)).first()
    if m is not None and (m.config or {}).get("promoted_from") != f"bronze.{dataset}":
        raise DefinitionError(f"a model named {dataset} already exists")
    config = {"materialized": "table", "promoted_from": f"bronze.{dataset}"}
    m = save_model(
        s,
        name=dataset,
        layer="silver",
        kind="sql",
        sql=sql,
        config=config,
        user=user,
        existing=m,
        description=f"Silver copy of bronze.{dataset}, maintained by its ingestion job",
    )
    name = f"promote_{dataset}"[:128]
    p = s.scalars(select(Pipeline).where(Pipeline.name == name)).first()
    if p is None:
        p = Pipeline(name=name, created_by=user, description=f"Promotes bronze.{dataset} to silver")
        s.add(p)
    p.models, p.trigger_datasets, p.enabled = [dataset], [f"bronze.{dataset}"], True
    s.flush()
    return m
