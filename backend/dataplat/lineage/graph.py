"""Table-level lineage graph built from stored OpenLineage events.

Nodes are datasets (``dataset:<namespace>|<name>``), jobs (``job:<name>``) and
App Portal entries (``app:<name>``). Edges run input dataset -> job -> output
dataset -> app. Edges are the union of every successful run up to a point in
time, so the graph can also be viewed "as of" a past date.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from dataplat.db.models import Dataset, LineageEvent, PortalApp

LAYERS = ("bronze", "silver", "gold")


def dataset_node(namespace: str, name: str) -> str:
    return f"dataset:{namespace}|{name}"


def _layer_of(namespace: str, lake: dict[str, str]) -> str:
    for layer, root in lake.items():
        if namespace.rstrip("/") == root.rstrip("/"):
            return layer
    if namespace.startswith("serving://"):
        return "serving"
    return "source"


def build_graph(s: Session, lake: dict[str, str], as_of: datetime | None = None) -> dict[str, Any]:
    q = select(LineageEvent).where(LineageEvent.event_type == "COMPLETE").order_by(LineageEvent.event_time)
    if as_of is not None:
        q = q.where(LineageEvent.event_time <= as_of)
    # Table-level lineage is the union of everything a job has read and written: a run
    # that found no new files must not erase the files earlier runs loaded.
    latest: dict[str, dict[str, Any]] = {}
    inputs: dict[str, dict[str, dict[str, Any]]] = {}
    outputs: dict[str, dict[str, dict[str, Any]]] = {}
    for ev in s.scalars(q):
        latest[ev.job_name] = ev.event  # later events win for job details
        for ds in ev.event.get("inputs", []):
            inputs.setdefault(ev.job_name, {})[f"{ds['namespace']}|{ds['name']}"] = ds
        for ds in ev.event.get("outputs", []):
            outputs.setdefault(ev.job_name, {})[f"{ds['namespace']}|{ds['name']}"] = ds

    nodes: dict[str, dict[str, Any]] = {}
    edges: set[tuple[str, str]] = set()
    catalog = {(d.layer, d.name): d for d in s.scalars(select(Dataset))}

    def add_dataset(ds: dict[str, Any]) -> str:
        nid = dataset_node(ds["namespace"], ds["name"])
        layer = _layer_of(ds["namespace"], lake)
        node = nodes.setdefault(
            nid, {"id": nid, "type": "dataset", "label": ds["name"], "namespace": ds["namespace"], "layer": layer}
        )
        if (entry := catalog.get((layer, ds["name"]))) is not None:
            node.update(dataset_id=str(entry.id), row_count=entry.row_count, last_loaded_at=_iso(entry.last_loaded_at))
        if "dataplat_file" in ds.get("facets", {}):
            node["file"] = ds["facets"]["dataplat_file"]
        return nid

    for job_name, ev in latest.items():
        if not inputs.get(job_name) and not outputs.get(job_name):
            continue  # a job that never moved data has no place in lineage
        jid = f"job:{job_name}"
        nodes[jid] = {
            "id": jid,
            "type": "job",
            "label": job_name,
            "layer": "job",
            "last_run": ev["eventTime"],
            "sql": ev["job"].get("facets", {}).get("sql", {}).get("query"),
        }
        for ds in inputs.get(job_name, {}).values():
            edges.add((add_dataset(ds), jid))
        for ds in outputs.get(job_name, {}).values():
            edges.add((jid, add_dataset(ds)))

    for app in s.scalars(select(PortalApp)):
        aid = f"app:{app.name}"
        nodes[aid] = {"id": aid, "type": "app", "label": app.name, "layer": "report", "url": app.url}
        for ref in app.datasets or []:
            layer, _, name = ref.partition(".")
            if layer in lake and name:
                edges.add((add_dataset({"namespace": lake[layer], "name": name}), aid))

    return {"nodes": list(nodes.values()), "edges": [{"source": a, "target": b} for a, b in sorted(edges)]}


def subgraph(
    graph: dict[str, Any], node: str, direction: Literal["upstream", "downstream", "both"] = "both", depth: int = 10
) -> dict[str, Any]:
    ups: dict[str, list[str]] = {}
    downs: dict[str, list[str]] = {}
    for e in graph["edges"]:
        downs.setdefault(e["source"], []).append(e["target"])
        ups.setdefault(e["target"], []).append(e["source"])
    keep = {node}
    for adj, wanted in ((ups, ("upstream", "both")), (downs, ("downstream", "both"))):
        if direction not in wanted:
            continue
        frontier = deque([(node, 0)])
        while frontier:
            n, d = frontier.popleft()
            if d >= depth:
                continue
            for m in adj.get(n, []):
                if m not in keep:
                    keep.add(m)
                    frontier.append((m, d + 1))
    return {
        "nodes": [n for n in graph["nodes"] if n["id"] in keep],
        "edges": [e for e in graph["edges"] if e["source"] in keep and e["target"] in keep],
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
