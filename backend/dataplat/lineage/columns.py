"""Column-level lineage, path tracing, impact analysis and run/batch tracing.

Column edges come from the OpenLineage ``columnLineage`` facets every writer emits:
ingestion (source column -> bronze column), vault loads (bronze -> hub/link/sat
columns, from the mapping metadata), and models (parsed from SQL with sqlglot, or
derived from the SCD2/fact/date builders). Column node ids are
``column:<namespace>|<dataset>|<column>``; their dataset is ``dataset:<namespace>|<dataset>``.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Literal

import pyarrow.compute as pc
from sqlalchemy import select
from sqlalchemy.orm import Session

from dataplat.db.models import (
    Alert,
    Dataset,
    IngestionJob,
    IngestionRun,
    LineageEvent,
    PortalApp,
    StreamState,
    TransformRun,
)
from dataplat.lineage.graph import _layer_of, dataset_node


def column_node(namespace: str, dataset: str, column: str) -> str:
    return f"column:{namespace}|{dataset}|{column}"


def split_column(node: str) -> tuple[str, str, str]:
    ns, ds, col = node.removeprefix("column:").rsplit("|", 2)
    return ns, ds, col


def build_column_graph(s: Session, lake: dict[str, str], as_of: datetime | None = None) -> dict[str, Any]:
    """Column nodes and edges (with the job that computes each edge)."""
    q = select(LineageEvent).where(LineageEvent.event_type == "COMPLETE").order_by(LineageEvent.event_time)
    if as_of is not None:
        q = q.where(LineageEvent.event_time <= as_of)
    latest: dict[str, dict[str, Any]] = {}  # job -> output -> column lineage (latest definition wins)
    jobs: dict[str, dict[str, Any]] = {}
    for ev in s.scalars(q):
        for out in ev.event.get("outputs", []):
            fields = out.get("facets", {}).get("columnLineage", {}).get("fields")
            if fields is None:
                continue
            latest.setdefault(ev.job_name, {})[f"{out['namespace']}|{out['name']}"] = (out, fields)
            jobs[ev.job_name] = ev.event
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str], dict[str, Any]] = {}

    def add(ns: str, ds: str, col: str) -> str:
        nid = column_node(ns, ds, col)
        nodes.setdefault(
            nid,
            {
                "id": nid,
                "type": "column",
                "label": col,
                "dataset": ds,
                "namespace": ns,
                "dataset_node": dataset_node(ns, ds),
                "layer": _layer_of(ns, lake),
            },
        )
        return nid

    for job, outputs in latest.items():
        for out, fields in outputs.values():
            for col, spec in fields.items():
                target = add(out["namespace"], out["name"], col)
                for f in spec.get("inputFields", []):
                    src = add(f["namespace"], f["name"], f["field"])
                    edges.setdefault((src, target), {"source": src, "target": target, "job": job})
    return {
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "jobs": {j: {"sql": e["job"].get("facets", {}).get("sql", {}).get("query")} for j, e in jobs.items()},
    }


def column_upstream(s: Session, lake: dict[str, str]) -> dict[tuple[str, str, str], list[tuple[str, str, str]]]:
    """(layer, dataset, column) -> the lake columns it is directly computed from."""
    g = build_column_graph(s, lake)
    nodes = {n["id"]: n for n in g["nodes"]}
    out: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {}
    for e in g["edges"]:
        src, dst = nodes[e["source"]], nodes[e["target"]]
        if src["layer"] in lake and dst["layer"] in lake:
            out.setdefault((dst["layer"], dst["dataset"], dst["label"]), []).append(
                (src["layer"], src["dataset"], src["label"])
            )
    return out


def walk(
    graph: dict[str, Any], start: set[str], direction: Literal["upstream", "downstream"], depth: int = 50
) -> set[str]:
    adj: dict[str, list[str]] = {}
    for e in graph["edges"]:
        a, b = (e["target"], e["source"]) if direction == "upstream" else (e["source"], e["target"])
        adj.setdefault(a, []).append(b)
    seen = set(start)
    frontier = deque((n, 0) for n in start)
    while frontier:
        n, d = frontier.popleft()
        if d >= depth:
            continue
        for m in adj.get(n, []):
            if m not in seen:
                seen.add(m)
                frontier.append((m, d + 1))
    return seen


def trace(
    graph: dict[str, Any], node: str, direction: Literal["upstream", "downstream"] = "upstream"
) -> dict[str, Any]:
    """The column path to (or from) ``node``, with the transformation behind each step."""
    keep = walk(graph, {node}, direction)
    edges = [e for e in graph["edges"] if e["source"] in keep and e["target"] in keep]
    steps = [
        {
            "from": e["source"],
            "to": e["target"],
            "job": e["job"],
            "sql": graph["jobs"].get(e["job"], {}).get("sql"),
        }
        for e in edges
    ]
    return {"nodes": [n for n in graph["nodes"] if n["id"] in keep], "edges": edges, "steps": steps}


def impact(table_graph: dict[str, Any], column_graph: dict[str, Any], node: str) -> dict[str, Any]:
    """Everything downstream of a dataset or column: datasets, jobs and apps affected."""
    if node.startswith("column:"):
        cols = walk(column_graph, {node}, "downstream")
        datasets = {n["dataset_node"] for n in column_graph["nodes"] if n["id"] in cols}
        # Jobs that compute the affected columns, and apps reading affected datasets
        jobs = {e["job"] for e in column_graph["edges"] if e["target"] in cols}
        affected = walk(table_graph, datasets, "downstream", depth=1)
        apps = {n for n in affected if n.startswith("app:")}
        start = datasets | {f"job:{j}" for j in jobs} | apps
        columns = sorted(cols - {node})
    else:
        start = walk(table_graph, {node}, "downstream")
        columns = []
    by_id = {n["id"]: n for n in table_graph["nodes"]}
    rows = [
        {
            "id": nid,
            "type": by_id[nid]["type"],
            "name": by_id[nid]["label"],
            "layer": by_id[nid].get("layer"),
            "status": by_id[nid].get("status"),
        }
        for nid in sorted(start - {node})
        if nid in by_id
    ]
    return {"node": node, "affected": rows, "columns": [split_column(c)[1:] for c in columns]}


# ------------------------------------------------------------------ live status overlays


def annotate_status(s: Session, graph: dict[str, Any]) -> dict[str, Any]:
    """Marks failed/late/lagging nodes: last run status, open alerts, stream lag."""
    ingest = {}
    for job, run in s.execute(
        select(IngestionJob.name, IngestionRun)
        .join(IngestionRun, IngestionRun.job_id == IngestionJob.id)
        .order_by(IngestionRun.started_at)
    ):
        ingest[f"ingest.{job}"] = run
    transform: dict[str, TransformRun] = {}
    for r in s.scalars(select(TransformRun).order_by(TransformRun.started_at)):
        if r.kind == "model":
            transform[f"model.{r.target.split('.', 1)[1]}"] = r
        elif r.kind == "vault_load":
            transform[f"vault.load.{r.target}"] = r
        elif r.kind == "vault_build":
            transform[f"vault.build.{r.target.split('.', 1)[1]}"] = r
    streams = {
        f"stream.{name}": st
        for name, st in s.execute(
            select(IngestionJob.name, StreamState).join(StreamState, StreamState.job_id == IngestionJob.id)
        )
    }
    alerts = {a.target: a for a in s.scalars(select(Alert).where(Alert.resolved_at.is_(None)))}
    for n in graph["nodes"]:
        if n["type"] == "job":
            name = n["label"]
            if name in streams:
                st = streams[name]
                n["status"] = "failed" if st.status == "failed" else st.status
                n["lag"] = (st.metrics or {}).get("lag")
                n["latency_p95_ms"] = (st.metrics or {}).get("latency_p95_ms")
            elif (run := ingest.get(name) or transform.get(name)) is not None:
                n["status"] = run.status
                n["last_error"] = (run.error or "")[:300] or None
        elif n["type"] == "dataset":
            ref = f"{n['layer']}.{n['label']}"
            if (a := alerts.get(ref)) is not None:
                n["status"] = "late" if a.kind == "freshness" else "alert"
                n["alert"] = a.message
    return graph


# ------------------------------------------------------------------ batch trace


def batch_trace(s: Session, tables: Any, lake: dict[str, str], batch_id: str) -> dict[str, Any]:
    """Where one ingestion batch went: its source files/query, and rows carrying its id."""
    run = s.scalars(select(IngestionRun).where(IngestionRun.batch_id == batch_id)).first()
    origin = None
    if run is not None:
        job = s.get(IngestionJob, run.job_id)
        origin = {
            "job": job.name if job else None,
            "run_id": str(run.id),
            "status": run.status,
            "started_at": run.started_at.isoformat(),
            "files": run.details.get("files", []),
            "rows_written": run.rows_written,
            "target": f"{job.spec['target']['layer']}.{job.spec['target']['dataset']}" if job else None,
        }
    found = []
    for ds in s.scalars(select(Dataset).order_by(Dataset.layer, Dataset.name)):
        if ds.layer not in lake:
            continue
        uri = f"{lake[ds.layer].rstrip('/')}/{ds.name}"
        try:
            if not tables.exists(uri):
                continue
            arrow = tables.dataset(uri)
            if "_batch_id" not in arrow.schema.names:
                continue
            n = arrow.count_rows(filter=pc.field("_batch_id") == batch_id)
        except Exception:
            continue
        if n:
            found.append({"dataset": f"{ds.layer}.{ds.name}", "layer": ds.layer, "rows": n, "dataset_id": str(ds.id)})
    apps = [a.name for a in s.scalars(select(PortalApp)) if set(a.datasets or []) & {f["dataset"] for f in found}]
    return {"batch_id": batch_id, "origin": origin, "datasets": found, "apps": apps}


# ------------------------------------------------------------------ access control


def _placeholder(node_id: str) -> str:
    import hashlib

    return "restricted:" + hashlib.sha1(node_id.encode()).hexdigest()[:12]


def restricted_nodes(graph: dict[str, Any], hidden_dataset_ids: set[str]) -> set[str]:
    """Dataset node ids (table graph) backed by datasets the viewer may not read."""
    return {n["id"] for n in graph["nodes"] if n.get("type") == "dataset" and n.get("dataset_id") in hidden_dataset_ids}


def mask_graph(graph: dict[str, Any], hidden: set[str]) -> dict[str, Any]:
    """Replaces restricted dataset (and column) nodes with anonymous placeholders.

    ``hidden`` holds table-level dataset node ids; column nodes belonging to them are
    masked too. Edges are kept, so paths through a restricted dataset stay visible.
    """
    if not hidden:
        return graph
    remap: dict[str, str] = {}
    nodes = []
    for n in graph["nodes"]:
        owner = n["id"] if n.get("type") == "dataset" else n.get("dataset_node")
        if owner in hidden:
            new_id = _placeholder(n["id"])
            remap[n["id"]] = new_id
            nodes.append(
                {
                    "id": new_id,
                    "type": n["type"],
                    "label": "restricted" if n.get("type") == "column" else "Restricted dataset",
                    "layer": n.get("layer"),
                    "restricted": True,
                    **(
                        {"dataset": "restricted", "dataset_node": _placeholder(owner)}
                        if n.get("type") == "column"
                        else {}
                    ),
                }
            )
        else:
            nodes.append(n)
    out = {**graph, "nodes": nodes}
    out["edges"] = [
        {**e, "source": remap.get(e["source"], e["source"]), "target": remap.get(e["target"], e["target"])}
        for e in graph["edges"]
    ]
    if "steps" in graph:
        out["steps"] = [
            {**st, "from": remap.get(st["from"], st["from"]), "to": remap.get(st["to"], st["to"])}
            for st in graph["steps"]
        ]
    return out


def annotate_governance(s: Session, graph: dict[str, Any]) -> dict[str, Any]:
    """Adds active tags and the DQ score to dataset nodes (for the node details panel)."""
    import uuid as _uuid

    from dataplat.db.models import TagAssignment
    from dataplat.quality.summary import dataset_dq

    ids = [n["dataset_id"] for n in graph["nodes"] if n.get("type") == "dataset" and n.get("dataset_id")]
    if not ids:
        return graph
    tags: dict[str, set[str]] = {}
    for a in s.scalars(
        select(TagAssignment).where(
            TagAssignment.dataset_id.in_([_uuid.UUID(i) for i in ids]), TagAssignment.status == "active"
        )
    ):
        tags.setdefault(str(a.dataset_id), set()).add(a.tag)
    for n in graph["nodes"]:
        if n.get("dataset_id"):
            n["tags"] = sorted(tags.get(n["dataset_id"], ()))
            dq = dataset_dq(s, _uuid.UUID(n["dataset_id"]))
            n["dq_score"] = dq["score"] if dq else None
    return graph
