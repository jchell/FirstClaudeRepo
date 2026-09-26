"""Loads the raw vault from a source dataset, then rebuilds the business vault on top.

For one source (e.g. ``bronze.customers``), every enabled mapping is loaded in DV2
order (hubs, then links, then satellites). Each mapping reads only the source rows
newer than its high-water mark (``_load_ts``) and appends what the generated
insert-only SQL returns, under the target table's lock. Rows keep the source
``_batch_id``, so any vault row can be traced to the ingestion run and files it came from.

Afterwards the PIT and bridge tables that depend on a changed table are rebuilt, and
pipelines triggered by the changed vault tables are queued.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

import pyarrow as pa
from sqlalchemy import select
from sqlalchemy.orm import Session

from dataplat.core.context import PlatformContext
from dataplat.db.models import VaultLoadState, VaultMapping, VaultObject
from dataplat.ingestion.runner import delta_compatible
from dataplat.transform import output
from dataplat.transform.locks import table_lock
from dataplat.transform.sandbox import sandbox
from dataplat.transform.triggers import notify_updated
from dataplat.vault import sql as vsql
from dataplat.vault.model import LOAD_DATE, Resolved

log = logging.getLogger(__name__)

ORDER = {"hub": 0, "link": 1, "sat": 2}
VAULT = "vault"


class VaultError(Exception):
    pass


# ------------------------------------------------------------------ metadata


def resolve(s: Session, name: str, _depth: int = 0) -> Resolved:
    obj = s.scalars(select(VaultObject).where(VaultObject.name == name)).first()
    if obj is None:
        raise VaultError(f"vault object {name} does not exist")
    r = Resolved(kind=obj.kind, name=obj.name, definition=obj.definition)
    if obj.kind == "link":
        roles = {}
        for h in obj.definition["hubs"]:
            hub = resolve(s, h["hub"], _depth + 1)
            if hub.kind != "hub":
                raise VaultError(f"{obj.name} references {h['hub']}, which is not a hub")
            roles[h.get("role") or h["hub"].removeprefix("hub_")] = (hub.name, hub.definition["business_keys"])
        r.parts = {"roles": roles}
    elif obj.kind == "sat":
        parent = resolve(s, obj.definition["parent"], _depth + 1)
        if parent.kind not in ("hub", "link"):
            raise VaultError(f"{obj.name}: a satellite's parent must be a hub or link")
        r.parts = {"parent": parent}
    elif obj.kind in ("pit", "bridge") and _depth == 0:
        r.parts = {"hub": resolve(s, obj.definition["hub"], 1)}
        members = obj.definition["satellites"] if obj.kind == "pit" else obj.definition["links"]
        r.parts["members"] = [resolve(s, m, 1) for m in members]
    return r


def dependents(s: Session, names: set[str]) -> list[Resolved]:
    """PIT and bridge tables built from any of ``names``."""
    out = []
    for obj in s.scalars(select(VaultObject).where(VaultObject.kind.in_(["pit", "bridge"]))):
        members = obj.definition.get("satellites") or obj.definition.get("links") or []
        if names & {obj.definition.get("hub"), *members}:
            out.append(resolve(s, obj.name))
    return out


# ------------------------------------------------------------------ loading


class VaultLoader:
    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx

    def uri(self, name: str) -> str:
        return self.ctx.config.lake.uri(VAULT, name)

    def load_source(self, layer: str, dataset: str, *, trigger: str = "manual") -> dict[str, Any]:
        with self.ctx.metadata.session() as s:
            mappings = list(
                s.scalars(
                    select(VaultMapping).where(
                        VaultMapping.source_layer == layer,
                        VaultMapping.source_dataset == dataset,
                        VaultMapping.enabled,
                    )
                )
            )
            plan = []
            for m in mappings:
                target = s.get(VaultObject, m.target_id)
                plan.append((m.id, resolve(s, target.name), dict(m.keys), dict(m.attributes), m.record_source))
        plan.sort(key=lambda p: (ORDER[p[1].kind], p[1].name))
        src_uri = self.ctx.config.lake.uri(layer, dataset)
        if not plan:
            return {"skipped": "no vault mappings for this dataset"}
        if not self.ctx.tables.exists(src_uri):
            return {"skipped": f"{layer}.{dataset} has no data yet"}

        changed: list[str] = []
        with output.tracked_run(
            self.ctx,
            "vault_load",
            f"{layer}.{dataset}",
            job_name=f"vault.load.{layer}.{dataset}",
            trigger=trigger,
            job_type="VAULT_LOAD",
        ) as rc:
            rc.inputs.append((layer, dataset))
            results = []
            for mapping_id, obj, keys, attributes, record_source in plan:
                res = self._load_one(mapping_id, obj, keys, attributes, record_source, layer, dataset, rc.run_id)
                results.append({"target": obj.name, "kind": obj.kind, **{k: v for k, v in res.items() if k != "out"}})
                rc.outputs.append(res["out"])
                rc.rows += res["rows"]
                if res["rows"]:
                    changed.append(obj.name)
            rc.details["targets"] = results

        built = self.rebuild_dependents(set(changed), trigger=trigger) if changed else []
        queued = notify_updated(self.ctx, [f"{VAULT}.{n}" for n in [*changed, *built]], trigger=f"vault:{dataset}")
        return {"source": f"{layer}.{dataset}", "targets": results, "rebuilt": built, "queued": queued}

    def _load_one(
        self,
        mapping_id: uuid.UUID,
        obj: Resolved,
        keys: dict[str, str],
        attributes: dict[str, str],
        record_source: str | None,
        layer: str,
        dataset: str,
        run_id: uuid.UUID,
    ) -> dict[str, Any]:
        tf = self.ctx.tables
        src_uri = self.ctx.config.lake.uri(layer, dataset)
        tgt_uri = self.uri(obj.name)
        with table_lock(self.ctx, f"{VAULT}.{obj.name}"):
            with self.ctx.metadata.session() as s:
                st = s.get(VaultLoadState, mapping_id)
                hwm = st.high_water if st else None
            src_ds = tf.dataset(src_uri)
            has_target = tf.exists(tgt_uri)
            relations: dict[str, Any] = {"src_all": src_ds}
            if has_target:
                relations["tgt"] = tf.dataset(tgt_uri)
            src = vsql.Source(src_ds.schema.names, f"{layer}.{dataset}", record_source)
            if obj.kind == "hub":
                sql, lineage = vsql.hub_sql(obj, keys, src, has_target)
            elif obj.kind == "link":
                sql, lineage = vsql.link_sql(obj, keys, src, has_target)
            else:
                sql, lineage = vsql.sat_sql(obj, keys, attributes, src, has_target)

            with sandbox(relations) as sb:
                if "_load_ts" not in src.columns:
                    raise VaultError(f"{layer}.{dataset} has no _load_ts column")
                # Views can't take parameters; the literal comes from a datetime, not user input.
                where = f"where _load_ts > timestamptz '{hwm.isoformat()}'" if hwm else ""
                sb.conn.execute(f"create temp view src as select * from src_all {where}")
                new_hwm: datetime | None = sb.scalar("select max(_load_ts) from src")
                staged = sb.scalar("select count(*) from src")
                rows = sb.query(sql)

            version = None
            if rows.num_rows:
                rows = _conform(delta_compatible(rows), tf.dataset(tgt_uri).schema if has_target else None)
                version = tf.write(rows, tgt_uri, "append")
            dataset_id = output.register(self.ctx, VAULT, obj.name, run_id) if tf.exists(tgt_uri) else None
            with self.ctx.metadata.session() as s:
                st = s.get(VaultLoadState, mapping_id) or VaultLoadState(mapping_id=mapping_id)
                if new_hwm is not None:
                    st.high_water = new_hwm
                st.last_loaded_at = datetime.now().astimezone()
                st.last_rows = rows.num_rows
                s.merge(st)

        out = output.Output(
            layer=VAULT,
            name=obj.name,
            columns={col: [(layer, dataset, c) for c in cols] for col, cols in lineage.items()},
            rows=rows.num_rows,
            version=version,
            dataset_id=dataset_id,
        )
        return {"rows": rows.num_rows, "staged": staged, "version": version, "out": out}

    # ------------------------------------------------------------------ business vault

    def rebuild_dependents(self, changed: set[str], *, trigger: str = "data") -> list[str]:
        with self.ctx.metadata.session() as s:
            objs = dependents(s, changed)
        built = []
        for obj in objs:
            try:
                self.build(obj.name, trigger=trigger)
                built.append(obj.name)
            except Exception:
                log.exception("rebuilding %s failed", obj.name)
        return built

    def build(self, name: str, *, trigger: str = "manual") -> dict[str, Any]:
        """Rebuilds a PIT or bridge table (full overwrite) from current vault tables."""
        with self.ctx.metadata.session() as s:
            obj = resolve(s, name)
        if obj.kind not in ("pit", "bridge"):
            raise VaultError(f"{name} is a {obj.kind}; only PIT and bridge tables are built")
        hub: Resolved = obj.parts["hub"]
        members: list[Resolved] = obj.parts["members"]
        tf = self.ctx.tables
        with (
            output.tracked_run(
                self.ctx,
                "vault_build",
                f"{VAULT}.{name}",
                job_name=f"vault.build.{name}",
                trigger=trigger,
                job_type="BUSINESS_VAULT",
            ) as rc,
            table_lock(self.ctx, f"{VAULT}.{name}"),
        ):
            if not tf.exists(self.uri(hub.name)):
                rc.details["skipped"] = f"{hub.name} has no data yet"
                return {"skipped": rc.details["skipped"]}
            relations: dict[str, Any] = {"hub": tf.dataset(self.uri(hub.name))}
            present = []
            for m in members:
                if tf.exists(self.uri(m.name)):
                    relations[f"{'s' if obj.kind == 'pit' else 'l'}{len(present)}"] = tf.dataset(self.uri(m.name))
                    present.append(m)
            if not present:
                rc.details["skipped"] = "no member tables have data yet"
                return {"skipped": rc.details["skipped"]}
            if obj.kind == "pit":
                for m in present:
                    if m.parts["parent"].name != hub.name:
                        raise VaultError(f"{m.name} does not belong to {hub.name}")
                query = vsql.pit_sql(hub, present)
                columns = {hub.hk: [(VAULT, hub.name, hub.hk)], "snapshot_date": []}
                for m in present:
                    columns["snapshot_date"].append((VAULT, m.name, LOAD_DATE))
                    columns[f"{m.name}_ldts"] = [(VAULT, m.name, LOAD_DATE)]
            else:
                query = vsql.bridge_sql(hub, present)
                columns = {hub.hk: [(VAULT, hub.name, hub.hk)]}
                for m in present:
                    columns[m.hk] = [(VAULT, m.name, m.hk)]
            with sandbox(relations) as sb:
                table = sb.query(query)
            version = tf.write(delta_compatible(table), self.uri(name), "overwrite")
            rc.inputs += [(VAULT, hub.name), *((VAULT, m.name) for m in present)]
            dataset_id = output.register(self.ctx, VAULT, name, rc.run_id)
            rc.outputs.append(
                output.Output(VAULT, name, columns, rows=table.num_rows, version=version, dataset_id=dataset_id)
            )
            rc.rows, rc.table_version = table.num_rows, version
        return {"target": name, "rows": table.num_rows, "version": version}


def _conform(table: pa.Table, schema: pa.Schema | None) -> pa.Table:
    """Casts new rows to the target's column types where they already exist."""
    if schema is None:
        return table
    for i, name in enumerate(table.column_names):
        if name in schema.names and table.schema.field(i).type != schema.field(name).type:
            try:
                table = table.set_column(i, name, table.column(i).cast(schema.field(name).type))
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as e:
                raise VaultError(f"column {name} changed type: {e}") from e
    return table
