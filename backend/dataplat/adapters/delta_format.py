"""TableFormat on Delta Lake (delta-rs), stored in the platform object store."""

from __future__ import annotations

from typing import Any, Literal

import pyarrow as pa
from deltalake import DeltaTable, write_deltalake
from deltalake.exceptions import TableNotFoundError

from dataplat.core.config import PlatformConfig


class DeltaTableFormat:
    def __init__(self, storage_options: dict[str, str] | None = None) -> None:
        self.storage_options = storage_options or {}

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> DeltaTableFormat:
        return cls(registry.get("object_store").storage_options())

    def _opts(self, uri: str) -> dict[str, str] | None:
        return self.storage_options if uri.startswith(("s3://", "s3a://")) else None

    def exists(self, uri: str) -> bool:
        return DeltaTable.is_deltatable(uri, storage_options=self._opts(uri))

    def write(
        self,
        table: pa.Table,
        uri: str,
        mode: Literal["append", "overwrite"] = "append",
        schema_mode: Literal["merge", "overwrite"] | None = None,
    ) -> int:
        """Writes in a single commit and returns the new table version."""
        write_deltalake(
            uri,
            table,
            mode=mode,
            schema_mode=schema_mode or ("overwrite" if mode == "overwrite" else "merge"),
            storage_options=self._opts(uri),
        )
        return self.table(uri).version()

    def merge(self, table: pa.Table, uri: str, keys: list[str]) -> int:
        if not self.exists(uri):
            return self.write(table, uri, "overwrite")
        predicate = " AND ".join(f"t.`{k}` = s.`{k}`" for k in keys)
        (
            self.table(uri)
            .merge(table, predicate, source_alias="s", target_alias="t")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute()
        )
        return self.table(uri).version()

    def table(self, uri: str, version: int | None = None) -> DeltaTable:
        try:
            return DeltaTable(uri, version=version, storage_options=self._opts(uri))
        except TableNotFoundError as e:
            raise FileNotFoundError(uri) from e

    def read(self, uri: str, version: int | None = None, limit: int | None = None) -> pa.Table:
        ds = self.table(uri, version).to_pyarrow_dataset()
        return ds.head(limit) if limit is not None else ds.to_table()

    def dataset(self, uri: str) -> Any:
        """A pyarrow dataset for streaming scans (profiling, DuckDB)."""
        return self.table(uri).to_pyarrow_dataset()

    def history(self, uri: str) -> list[dict[str, Any]]:
        return self.table(uri).history()

    def stats(self, uri: str) -> dict[str, int]:
        """Row count and size from the Delta log, without scanning data."""
        actions = self.table(uri).get_add_actions(flatten=True)
        adds = pa.table(actions) if not isinstance(actions, pa.Table) else actions
        names = adds.column_names
        rows = sum(v or 0 for v in adds.column("num_records").to_pylist()) if "num_records" in names else 0
        size = sum(v or 0 for v in adds.column("size_bytes").to_pylist()) if "size_bytes" in names else 0
        return {"rows": int(rows), "bytes": int(size)}
