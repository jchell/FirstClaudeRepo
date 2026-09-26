"""Worker job handlers for connections and ingestion.

Anything that touches a source runs here, on the worker, as the connection's
service account: the API can't read connection secrets, so even "test connection"
and the wizard's table picker are short jobs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from dataplat.connectors.base import ConnectorError, ReadContext, ReadRequest
from dataplat.connectors.registry import get_connector_class
from dataplat.core.context import PlatformContext
from dataplat.core.logging import redact
from dataplat.core.ports.orchestration import Job
from dataplat.core.ports.secrets import SecretStore
from dataplat.db.models import Connection
from dataplat.ingestion.runner import IngestionRunner
from dataplat.orchestration.handlers import handler

PREVIEW_ROWS = 20
DISCOVER_LIMIT = 2000


def _connector(ctx: PlatformContext, connection_id: str, secrets: SecretStore | None) -> Any:
    with ctx.metadata.session() as s:
        conn = s.get(Connection, uuid.UUID(connection_id))
        if conn is None:
            raise LookupError("connection not found")
        return get_connector_class(conn.type)(dict(conn.config), secrets)


def _json_safe(v: Any) -> Any:
    if v is None or isinstance(v, bool | int | float | str):
        return v
    return str(v)


@handler("connection.test")
def test_connection(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    cid = job.payload["connection_id"]
    try:
        with _connector(ctx, cid, secrets) as c:
            info = c.test()
        ok, message = True, "Connection succeeded"
    except ConnectorError as e:
        ok, message, info = False, redact(str(e)), {}
    except Exception as e:  # driver errors, DNS, TLS...
        ok, message, info = False, redact(f"{type(e).__name__}: {e}")[:500], {}
    with ctx.metadata.session() as s:
        conn = s.get(Connection, uuid.UUID(cid))
        if conn is not None:
            conn.last_test_at, conn.last_test_ok, conn.last_test_message = datetime.now(UTC), ok, message
    return {"ok": ok, "message": message, "info": {k: _json_safe(v) for k, v in info.items()}}


@handler("connection.discover")
def discover(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    with _connector(ctx, job.payload["connection_id"], secrets) as c:
        objects = c.discover(job.payload.get("pattern"))
    return {
        "objects": [
            {
                "name": o.name,
                "kind": o.kind,
                "columns": o.columns,
                "size": o.size,
                "modified": o.modified.isoformat() if o.modified else None,
            }
            for o in objects[:DISCOVER_LIMIT]
        ],
        "truncated": len(objects) > DISCOVER_LIMIT,
    }


@handler("connection.preview")
def preview(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    """First rows of a source object, for the wizard. The API scrubs the result once read."""
    req = ReadRequest.model_validate(
        {**job.payload.get("request", {}), "max_records": PREVIEW_ROWS, "load_mode": "full"}
    )
    req.options = {**req.options, "consumer_group": f"dataplat-preview-{uuid.uuid4().hex[:8]}"}
    with _connector(ctx, job.payload["connection_id"], secrets) as c:
        rctx = ReadContext(namespace=c.namespace())
        batches = []
        for b in c.read(req, rctx):
            batches.append(b)
            if sum(x.data.num_rows for x in batches) >= PREVIEW_ROWS:
                break
    import pyarrow as pa

    if not batches:
        return {"columns": [], "rows": [], "files": []}
    table = pa.concat_tables(
        [pa.table(b.data) if isinstance(b.data, pa.RecordBatch) else b.data for b in batches],
        promote_options="permissive",
    ).slice(0, PREVIEW_ROWS)
    return {
        "columns": [{"name": f.name, "type": str(f.type)} for f in table.schema],
        "rows": [{k: _json_safe(v) for k, v in r.items()} for r in table.to_pylist()],
        "files": [f.path for f in rctx.files],
    }


@handler("ingestion.run")
def run_ingestion(ctx: PlatformContext, job: Job, secrets: SecretStore | None) -> dict[str, Any]:
    return IngestionRunner(ctx).run(
        uuid.UUID(job.payload["ingestion_job_id"]),
        secrets,
        queue_job_id=job.id,
        trigger=job.payload.get("trigger", "manual"),
    )
