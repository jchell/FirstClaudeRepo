"""Builds models and runs pipelines.

A model build: compile (templating or a declarative builder) -> run in the sandbox
over Arrow views of its inputs -> write the Delta table (overwrite, or merge/append for
incremental models) under the table's lock -> catalog + column-level lineage ->
optional serving-DB replication -> queue whatever is triggered by the new data.

A pipeline builds its models in dependency order (ref() and builder inputs form the
DAG); when a model fails or has no input data yet, the models depending on it are
skipped.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from typing import Any

import pyarrow as pa
from sqlalchemy import select

from dataplat.core.context import PlatformContext
from dataplat.db.models import Pipeline, TransformModel
from dataplat.ingestion.runner import delta_compatible
from dataplat.transform import models as mdl
from dataplat.transform import output
from dataplat.transform.column_lineage import column_lineage
from dataplat.transform.locks import table_lock
from dataplat.transform.sandbox import sandbox
from dataplat.transform.templating import TemplateError, check_select, relation_name, render
from dataplat.transform.triggers import notify_updated

log = logging.getLogger(__name__)

PREVIEW_ROWS = 50


class ModelError(Exception):
    pass


class SkipModel(Exception):
    """The model can't be built yet (an input has no data)."""

    skip = True


@dataclass
class ModelSpec:
    name: str
    layer: str
    kind: str
    sql: str
    config: dict[str, Any]
    id: uuid.UUID | None = None

    @classmethod
    def of(cls, m: TransformModel) -> ModelSpec:
        return cls(m.name, m.layer, m.kind, m.sql or "", dict(m.config or {}), m.id)


@dataclass
class Compiled:
    sql: str
    relations: dict[str, tuple[str, str]] = field(default_factory=dict)
    incremental: bool = False
    extra_lineage: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)


def all_models(ctx: PlatformContext) -> dict[str, ModelSpec]:
    with ctx.metadata.session() as s:
        return {m.name: ModelSpec.of(m) for m in s.scalars(select(TransformModel))}


def inputs(spec: ModelSpec, models: dict[str, ModelSpec]) -> list[str]:
    """Every dataset ("<layer>.<name>") the model reads, models or not."""
    layers = {n: m.layer for n, m in models.items()}
    out: list[str] = []
    try:
        if spec.kind in ("sql", "fact"):
            r = render(spec.sql, layers, incremental=False, this=(spec.layer, spec.name))
            out += [f"{layer}.{name}" for layer, name in r.relations.values()]
        if spec.kind == "fact":
            out += [
                f"{layers[d['dimension']]}.{d['dimension']}"
                for d in spec.config.get("dimensions", [])
                if d.get("dimension") in layers
            ]
        if spec.kind == "scd2_dimension":
            out += [ref for ref in (spec.config.get("source"), spec.config.get("deletes_source")) if ref]
    except TemplateError:
        pass
    return list(dict.fromkeys(out))


def dependencies(spec: ModelSpec, models: dict[str, ModelSpec]) -> set[str]:
    """Names of the models ``spec`` reads."""
    by_table = {(m.layer, m.name): m.name for m in models.values()}
    layers = {n: m.layer for n, m in models.items()}
    deps: set[str] = set()
    try:
        if spec.kind in ("sql", "fact"):
            r = render(spec.sql, layers, incremental=False, this=(spec.layer, spec.name))
            deps |= {by_table[t] for t in r.relations.values() if t in by_table}
        if spec.kind == "fact":
            deps |= {d["dimension"] for d in spec.config.get("dimensions", [])}
        if spec.kind == "scd2_dimension":
            for ref in (spec.config.get("source"), spec.config.get("deletes_source")):
                if ref:
                    layer, name = ref.split(".", 1)
                    if (layer, name) in by_table:
                        deps.add(by_table[(layer, name)])
    except TemplateError:
        pass
    deps.discard(spec.name)
    return deps & set(models)


class ModelRunner:
    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx

    def uri(self, layer: str, name: str) -> str:
        return self.ctx.config.lake.uri(layer, name)

    # ---------------------------------------------------------------- compile

    def compile(self, spec: ModelSpec, models: dict[str, ModelSpec]) -> Compiled:
        tf = self.ctx.tables
        target_exists = tf.exists(self.uri(spec.layer, spec.name))
        layers = {n: m.layer for n, m in models.items()}
        if spec.kind in ("sql", "fact"):
            incremental = spec.config.get("materialized") == "incremental" and target_exists
            r = render(spec.sql, layers, incremental=incremental, this=(spec.layer, spec.name))
            check_select(r.sql)
            sql, relations = r.sql, dict(r.relations)
            if spec.kind == "fact":
                lookups = []
                for d in spec.config.get("dimensions", []):
                    lk = mdl.DimensionLookup.model_validate(d)
                    dim = models.get(lk.dimension)
                    if dim is None or dim.kind != "scd2_dimension":
                        raise ModelError(f"{lk.dimension} is not an SCD2 dimension model")
                    lookups.append((lk, dim.layer, dim.config.get("surrogate_key", "sk")))
                    relations[relation_name(dim.layer, dim.name)] = (dim.layer, dim.name)
                sql = mdl.fact_sql(sql, lookups)
            if r.uses_this:
                relations["__this"] = (spec.layer, spec.name)
            return Compiled(sql, relations, incremental)
        if spec.kind == "scd2_dimension":
            cfg = mdl.Scd2Config.model_validate(spec.config)
            layer, name = cfg.source.split(".", 1)
            src_uri = self.uri(layer, name)
            if not tf.exists(src_uri):
                raise SkipModel(f"{cfg.source} has no data yet")
            src_cols = tf.dataset(src_uri).schema.names
            relations = {relation_name(layer, name): (layer, name)}
            hub = None
            extra: dict[str, list[tuple[str, str, str]]] = {}
            if layer == "vault":
                from dataplat.vault.loader import resolve

                with self.ctx.metadata.session() as s:
                    sat = resolve(s, name)
                if sat.kind != "sat":
                    raise ModelError(f"{cfg.source} is a {sat.kind}; SCD2 dimensions read satellites")
                parent = sat.parts["parent"]
                if parent.kind != "hub":
                    raise ModelError("SCD2 dimensions read hub satellites (not link satellites)")
                if sat.definition.get("multi_active_key"):
                    raise ModelError("multi-active satellites can't feed an SCD2 dimension directly")
                hub = (parent.name, parent.definition["business_keys"], parent.hk)
                relations[relation_name("vault", parent.name)] = ("vault", parent.name)
            if cfg.deletes_source:
                dl, dn = cfg.deletes_source.split(".", 1)
                if not tf.exists(self.uri(dl, dn)):
                    cfg = cfg.model_copy(update={"deletes_source": None})
                else:
                    relations[relation_name(dl, dn)] = (dl, dn)
            try:
                sql, bk, attrs = mdl.scd2_sql(cfg, src_cols, hub)
            except ValueError as e:
                raise ModelError(str(e)) from e
            if hub:
                extra = {b: [("vault", hub[0], b)] for b in bk}
            return Compiled(sql, relations, False, extra)
        if spec.kind == "date_dimension":
            return Compiled(mdl.date_sql(mdl.DateConfig.model_validate(spec.config)))
        raise ModelError(f"unknown model kind {spec.kind}")

    def _relations(self, compiled: Compiled) -> dict[str, Any]:
        rels = {}
        for rel, (layer, name) in compiled.relations.items():
            uri = self.uri(layer, name)
            if not self.ctx.tables.exists(uri):
                raise SkipModel(f"{layer}.{name} has no data yet")
            rels[rel] = self.ctx.tables.dataset(uri)
        return rels

    # ---------------------------------------------------------------- preview

    def preview(self, spec: ModelSpec, limit: int = PREVIEW_ROWS, principal: Any = None) -> dict[str, Any]:
        """First rows of a draft model. With a ``principal``, inputs are read under that
        person's data policies (masking, row filters, grants), like any interactive read."""
        from dataplat.security.policy import AccessDenied, PolicyEngine, secured_relations

        models = all_models(self.ctx)
        models[spec.name] = spec
        try:
            compiled = self.compile(spec, models)
            relations = self._relations(compiled)
            if principal is not None:
                with self.ctx.metadata.session() as s:
                    relations = secured_relations(self.ctx, PolicyEngine(s, principal), compiled.relations)
        except (TemplateError, ModelError, SkipModel, AccessDenied, LookupError) as e:
            raise ModelError(str(e)) from e
        with sandbox(relations) as sb:
            try:
                table = sb.query(f"select * from ({compiled.sql}) __preview limit {int(limit)}")
            except Exception as e:
                raise ModelError(output.error_message(e)) from e
        lineage = self._lineage(compiled, relations, table.schema.names)
        return {
            "sql": compiled.sql,
            "columns": [{"name": f.name, "type": str(f.type)} for f in table.schema],
            "rows": [{k: _json_safe(v) for k, v in r.items()} for r in table.to_pylist()],
            "lineage": {c: [f"{layer}.{ds}.{col}" for layer, ds, col in srcs] for c, srcs in lineage.items()},
            "dependencies": sorted(dependencies(spec, models)),
        }

    def _lineage(self, compiled: Compiled, relations: dict[str, Any], columns: list[str]) -> output.ColumnLineage:
        schemas = {rel: ds.schema for rel, ds in relations.items()}
        lineage = column_lineage(compiled.sql, schemas, compiled.relations, columns) if relations else {}
        for col, srcs in compiled.extra_lineage.items():
            lineage[col] = srcs + [s for s in lineage.get(col, []) if s not in srcs]
        return lineage

    # ---------------------------------------------------------------- build

    def run_model(
        self,
        name: str,
        *,
        trigger: str = "manual",
        pipeline_id: uuid.UUID | None = None,
        parent_run_id: uuid.UUID | None = None,
        models: dict[str, ModelSpec] | None = None,
        notify: bool = True,
    ) -> dict[str, Any]:
        models = models or all_models(self.ctx)
        spec = models.get(name)
        if spec is None:
            raise ModelError(f"model {name} does not exist")
        tf = self.ctx.tables
        uri = self.uri(spec.layer, spec.name)
        with (
            output.tracked_run(
                self.ctx,
                "model",
                f"{spec.layer}.{spec.name}",
                job_name=f"model.{spec.name}",
                trigger=trigger,
                pipeline_id=pipeline_id,
                parent_run_id=parent_run_id,
                job_type=spec.kind.upper(),
            ) as rc,
            table_lock(self.ctx, f"{spec.layer}.{spec.name}"),
        ):
            compiled = self.compile(spec, models)
            relations = self._relations(compiled)
            with sandbox(relations) as sb:
                try:
                    table = sb.query(compiled.sql)
                except Exception as e:
                    raise ModelError(output.error_message(e)) from e
            table = delta_compatible(table)
            keys = spec.config.get("unique_key") or []
            if compiled.incremental and keys:
                if missing := [k for k in keys if k not in table.column_names]:
                    raise ModelError(f"unique_key columns {missing} are not in the result")
                version = tf.merge(_last_per_key(table, keys), uri, keys) if table.num_rows else None
                mode = "merge"
            elif compiled.incremental:
                version = tf.write(table, uri, "append") if table.num_rows else None
                mode = "append"
            else:
                version = tf.write(table, uri, "overwrite")
                mode = "overwrite"
            dataset_id = output.register(self.ctx, spec.layer, spec.name, rc.run_id) if tf.exists(uri) else None
            lineage = self._lineage(compiled, relations, table.schema.names)
            rc.inputs += list(compiled.relations.values())
            rc.outputs.append(
                output.Output(
                    spec.layer, spec.name, lineage, rows=table.num_rows, version=version, dataset_id=dataset_id
                )
            )
            rc.rows, rc.table_version = table.num_rows, version
            rc.details.update(mode=mode, sql=compiled.sql[:20000])
            if mdl.serves(spec.kind, spec.config) and tf.exists(uri):
                rc.details["serving"] = self._serve(spec, table, compiled.incremental and bool(keys), keys)
                # The replica is a lineage node of its own: gold table -> serving table.
                rc.outputs.append(
                    output.Output(
                        "serving",
                        f"{spec.layer}.{spec.name}",
                        {c: [(spec.layer, spec.name, c)] for c in table.schema.names},
                        rows=rc.details["serving"]["rows"],
                    )
                )
            run_id = rc.run_id
        queued = (
            notify_updated(
                self.ctx, [f"{spec.layer}.{spec.name}"], trigger=f"model:{spec.name}", skip_pipeline=pipeline_id
            )
            if notify and (table.num_rows or mode == "overwrite")
            else {}
        )
        return {
            "model": spec.name,
            "run_id": str(run_id),
            "rows": table.num_rows,
            "mode": mode,
            "version": version,
            "queued": queued,
        }

    def _serve(self, spec: ModelSpec, changed: pa.Table, upsert: bool, keys: list[str]) -> dict[str, Any]:
        serving = self.ctx.serving
        full = self.ctx.tables.read(self.uri(spec.layer, spec.name))
        existing = serving.table_columns(spec.layer, spec.name)
        if upsert and existing == full.schema.names:
            n = serving.upsert(spec.layer, spec.name, changed, keys) if changed.num_rows else 0
            return {"mode": "upsert", "rows": n, "table": f"{spec.layer}.{spec.name}"}
        n = serving.replace(spec.layer, spec.name, full)
        return {"mode": "replace", "rows": n, "table": f"{spec.layer}.{spec.name}"}


def _last_per_key(table: pa.Table, keys: list[str]) -> pa.Table:
    """Delta MERGE needs one source row per key; the last one wins."""
    seen: dict[tuple, int] = {}
    cols = [table.column(k).to_pylist() for k in keys]
    for i, key in enumerate(zip(*cols, strict=True)):
        seen[key] = i
    return table.take(sorted(seen.values())) if len(seen) != table.num_rows else table


def _json_safe(v: Any) -> Any:
    if v is None or isinstance(v, bool | int | float | str):
        return v
    return str(v)


# ------------------------------------------------------------------ pipelines


def model_order(names: list[str], models: dict[str, ModelSpec]) -> list[str]:
    selected = [n for n in names if n in models]
    graph = {n: dependencies(models[n], models) & set(selected) for n in selected}
    try:
        return list(TopologicalSorter(graph).static_order())
    except CycleError as e:
        raise ModelError(f"models depend on each other in a cycle: {' -> '.join(e.args[1])}") from e


class PipelineRunner:
    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx

    def run(self, pipeline_id: uuid.UUID, *, trigger: str = "manual") -> dict[str, Any]:
        with self.ctx.metadata.session() as s:
            p = s.get(Pipeline, pipeline_id)
            if p is None:
                raise ModelError("pipeline not found")
            name, names = p.name, list(p.models or [])
        models = all_models(self.ctx)
        missing = [n for n in names if n not in models]
        order = model_order(names, models)
        results: dict[str, dict[str, Any]] = {}
        blocked: set[str] = set()
        runner = ModelRunner(self.ctx)
        written: list[str] = []
        with output.tracked_run(
            self.ctx,
            "pipeline",
            name,
            job_name=f"pipeline.{name}",
            trigger=trigger,
            pipeline_id=pipeline_id,
            job_type="PIPELINE",
        ) as rc:
            for model in order:
                deps = dependencies(models[model], models)
                if deps & blocked:
                    results[model] = {"status": "skipped", "reason": f"upstream {sorted(deps & blocked)} not built"}
                    blocked.add(model)
                    continue
                try:
                    res = runner.run_model(
                        model,
                        trigger=f"pipeline:{name}",
                        pipeline_id=pipeline_id,
                        parent_run_id=rc.run_id,
                        models=models,
                        notify=False,
                    )
                    results[model] = {"status": "succeeded", **res}
                    written.append(f"{models[model].layer}.{model}")
                except SkipModel as e:
                    results[model] = {"status": "skipped", "reason": str(e)}
                    blocked.add(model)
                except Exception as e:
                    log.warning("pipeline %s: model %s failed: %s", name, model, e)
                    results[model] = {"status": "failed", "error": output.error_message(e)}
                    blocked.add(model)
            rc.rows = sum(r.get("rows", 0) for r in results.values())
            rc.details = {"models": results, "missing": missing}
            failed = [m for m, r in results.items() if r["status"] == "failed"]
            if failed:
                raise ModelError(f"models failed: {', '.join(failed)}")
        queued = notify_updated(self.ctx, written, trigger=f"pipeline:{name}", skip_pipeline=pipeline_id)
        return {"pipeline": name, "run_id": str(rc.run_id), "models": results, "queued": queued}
