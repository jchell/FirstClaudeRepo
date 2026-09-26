"""QueryEngine on DuckDB, reading lake data straight from the object store."""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import urlparse

import duckdb
import pyarrow as pa

from dataplat.core.config import PlatformConfig
from dataplat.core.ports.storage import ObjectStore


def _load_httpfs(conn: duckdb.DuckDBPyConnection) -> None:
    """Loads httpfs from the pip-installed extension wheel (no runtime download)."""
    try:
        from duckdb_extensions import import_extension

        import_extension("httpfs", con=conn)
    except ImportError:
        conn.execute("INSTALL httpfs")
    conn.execute("LOAD httpfs")


class DuckDBQueryEngine:
    def __init__(self, object_store: ObjectStore | None = None, database: str = ":memory:") -> None:
        self._conn = duckdb.connect(database)
        self._lock = threading.Lock()
        if object_store is not None:
            self._configure_s3(object_store.storage_options())

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> DuckDBQueryEngine:
        return cls(registry.get("object_store"))

    def _configure_s3(self, opts: dict[str, str]) -> None:
        endpoint = urlparse(opts["AWS_ENDPOINT_URL"])
        _load_httpfs(self._conn)
        # A DuckDB secret keeps credentials inside the engine; they never appear in SQL text.
        self._conn.execute(
            "CREATE OR REPLACE SECRET lake (TYPE S3, KEY_ID ?, SECRET ?, ENDPOINT ?, REGION ?, "
            "URL_STYLE 'path', USE_SSL ?)",
            [
                opts["AWS_ACCESS_KEY_ID"],
                opts["AWS_SECRET_ACCESS_KEY"],
                endpoint.netloc,
                opts.get("AWS_REGION", "us-east-1"),
                endpoint.scheme == "https",
            ],
        )

    def query(self, sql: str, params: list[Any] | None = None) -> pa.Table:
        # DuckDB connections aren't safe for concurrent use; each query gets a cursor.
        with self._lock:
            cur = self._conn.cursor()
        try:
            result = cur.execute(sql, params or [])
            to_arrow = getattr(result, "to_arrow_table", None) or result.fetch_arrow_table
            return to_arrow()
        finally:
            cur.close()

    def health(self) -> bool:
        try:
            return self.query("select 1 as ok").column("ok")[0].as_py() == 1
        except Exception:
            return False

    def close(self) -> None:
        self._conn.close()
