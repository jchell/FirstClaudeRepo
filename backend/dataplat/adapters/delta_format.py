"""TableFormat on Delta Lake (delta-rs), stored in the platform object store."""

from __future__ import annotations

from typing import Any, Literal

import pyarrow as pa
import pyarrow.compute  # noqa: F401  (pa.compute)
from deltalake import CommitProperties, DeltaTable, Transaction, write_deltalake
from deltalake.exceptions import TableNotFoundError

from dataplat.core.config import PlatformConfig


def _commit(app_transactions: dict[str, int] | None) -> CommitProperties | None:
    if not app_transactions:
        return None
    return CommitProperties(app_transactions=[Transaction(app, version) for app, version in app_transactions.items()])


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
        app_transactions: dict[str, int] | None = None,
    ) -> int:
        """Writes in a single commit and returns the new table version.

        ``app_transactions`` (app id -> version) are recorded in the same commit; streams
        use them to store the source offsets they wrote, which makes replays idempotent.
        """
        write_deltalake(
            uri,
            table,
            mode=mode,
            schema_mode=schema_mode or ("overwrite" if mode == "overwrite" else "merge"),
            storage_options=self._opts(uri),
            commit_properties=_commit(app_transactions),
        )
        return self.table(uri).version()

    def merge(
        self,
        table: pa.Table,
        uri: str,
        keys: list[str],
        delete_flag: str | None = None,
        app_transactions: dict[str, int] | None = None,
    ) -> int:
        """Upserts rows by ``keys``; rows whose ``delete_flag`` column is true delete the match."""
        if not self.exists(uri):
            if delete_flag and delete_flag in table.column_names:
                table = table.filter(pa.compute.invert(pa.compute.fill_null(table.column(delete_flag), False)))
                table = table.drop_columns([delete_flag])
            return self.write(table, uri, "append", app_transactions=app_transactions)
        predicate = " AND ".join(f"t.`{k}` = s.`{k}`" for k in keys)
        m = self.table(uri).merge(
            table,
            predicate,
            source_alias="s",
            target_alias="t",
            merge_schema=True,
            commit_properties=_commit(app_transactions),
        )
        if delete_flag:
            # The flag drives the merge but is not stored in the target table.
            cols = {c: f"s.`{c}`" for c in table.column_names if c != delete_flag}
            m = (
                m.when_matched_delete(f"s.`{delete_flag}` = true")
                .when_matched_update(cols, f"s.`{delete_flag}` = false")
                .when_not_matched_insert(cols, f"s.`{delete_flag}` = false")
            )
        else:
            m = m.when_matched_update_all().when_not_matched_insert_all()
        m.execute()
        return self.table(uri).version()

    def app_transaction_version(self, uri: str, app_id: str) -> int | None:
        if not self.exists(uri):
            return None
        return self.table(uri).transaction_version(app_id)

    def compact(self, uri: str, retention_hours: int = 168) -> dict[str, Any]:
        """Merges small files (streams write many) and removes files older than the retention."""
        dt = self.table(uri)
        metrics = dt.optimize.compact()
        removed = dt.vacuum(retention_hours=retention_hours, dry_run=False, enforce_retention_duration=True)
        return {
            "compaction": {k: v for k, v in metrics.items() if isinstance(v, int | float)},
            "vacuumed_files": len(removed),
        }

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
