"""A locked-down DuckDB for running generated and user-written SQL over lake tables.

Lake tables are handed to DuckDB as Arrow datasets (read by delta-rs with the
platform's storage credentials), then external access is switched off and the
configuration locked. SQL run here can read only the relations it was given: no
files, no object store, no ATTACH, no COPY, and no way to turn that back on.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import duckdb
import pyarrow as pa

IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class Sandbox:
    def __init__(self, relations: dict[str, Any], memory_limit: str = "2GB", threads: int = 4) -> None:
        self.conn = duckdb.connect(":memory:")
        self.conn.execute(f"SET memory_limit='{memory_limit}'")
        self.conn.execute(f"SET threads={int(threads)}")
        for name, rel in relations.items():
            self.register(name, rel)
        self.conn.execute("SET enable_external_access=false")
        self.conn.execute("SET lock_configuration=true")

    def register(self, name: str, relation: Any) -> None:
        if not IDENT.match(name):
            raise ValueError(f"bad relation name {name!r}")
        self.conn.register(name, relation)

    def query(self, sql: str, params: list[Any] | None = None) -> pa.Table:
        result = self.conn.execute(sql, params or [])
        return (getattr(result, "to_arrow_table", None) or result.fetch_arrow_table)()

    def scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        row = self.conn.execute(sql, params or []).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self.conn.close()


@contextmanager
def sandbox(relations: dict[str, Any]) -> Iterator[Sandbox]:
    sb = Sandbox(relations)
    try:
        yield sb
    finally:
        sb.close()


def empty_like(schema: pa.Schema) -> pa.Table:
    return schema.empty_table()
