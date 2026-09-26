"""ServingStore on the Postgres ``serving`` database.

Gold/silver tables are replicated here for BI tools and direct SQL. Connections use
the same short-lived Vault database credentials as the metadata store (the dynamic
login acts as dataplat_owner, which owns the serving database).

  replace  load into a new table, then swap it in within one transaction, so readers
           see either the old or the new copy, never a half-loaded one
  upsert   copy rows into a temp table and INSERT ... ON CONFLICT on the key columns
"""

from __future__ import annotations

import io
import os
import re
from typing import Any

import pyarrow as pa
import pyarrow.csv as pacsv
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from dataplat.core.config import PlatformConfig

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"bad serving identifier {name!r}")
    return _q(name)


def pg_type(t: pa.DataType) -> str:
    if pa.types.is_boolean(t):
        return "boolean"
    if pa.types.is_int8(t) or pa.types.is_int16(t):
        return "smallint"
    if pa.types.is_int32(t):
        return "integer"
    if pa.types.is_integer(t):
        return "bigint"
    if pa.types.is_floating(t):
        return "double precision"
    if pa.types.is_decimal(t):
        return f"numeric({t.precision},{t.scale})"
    if pa.types.is_timestamp(t):
        return "timestamptz" if t.tz else "timestamp"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_binary(t) or pa.types.is_large_binary(t):
        return "bytea"
    return "text"


def _textual(table: pa.Table) -> pa.Table:
    """Nested and binary values go over COPY as text."""
    cols = []
    for field, col in zip(table.schema, table.columns, strict=True):
        t = field.type
        if pa.types.is_nested(t) or pa.types.is_binary(t) or pa.types.is_large_binary(t):
            col = pa.array([None if v is None else str(v) for v in col.to_pylist()], pa.string())
        cols.append(col)
    return pa.Table.from_arrays(cols, names=table.column_names)


class PostgresServingStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> PostgresServingStore:
        if url := os.environ.get("DATAPLAT_TEST_SERVING_URL"):
            return cls(create_engine(url, pool_pre_ping=True))
        from dataplat.adapters.pg_metadata import VaultDbCredentials
        from dataplat.adapters.vault_secrets import shared_vault

        pg = config.postgres
        creds = VaultDbCredentials(shared_vault(registry), pg.vault_role, config.vault.database_mount)
        engine = create_engine(
            f"postgresql+psycopg://{pg.host}:{pg.port}/{config.serving.database}", pool_pre_ping=True, pool_recycle=1800
        )

        @event.listens_for(engine, "do_connect")
        def _inject(dialect: Any, conn_rec: Any, cargs: Any, cparams: dict[str, Any]) -> None:
            cparams["user"], cparams["password"] = creds.get()

        return cls(engine)

    # ---------------------------------------------------------------- helpers

    def _columns(self, schema: pa.Schema) -> str:
        return ", ".join(f"{_ident(f.name)} {pg_type(f.type)}" for f in schema)

    def _copy(self, raw: Any, qualified: str, table: pa.Table) -> None:
        if table.num_rows == 0:
            return
        buf = io.BytesIO()
        pacsv.write_csv(_textual(table), buf, pacsv.WriteOptions(include_header=False, quoting_style="needed"))
        cols = ", ".join(_ident(c) for c in table.column_names)
        with raw.cursor() as cur, cur.copy(f"COPY {qualified} ({cols}) FROM STDIN WITH (FORMAT csv)") as cp:
            cp.write(buf.getvalue())

    def _ensure_schema(self, raw: Any, schema: str) -> None:
        with raw.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {_ident(schema)}")

    def table_columns(self, schema: str, table: str) -> list[str] | None:
        with self.engine.connect() as c:
            rows = c.execute(
                text(
                    "select column_name from information_schema.columns where table_schema = :s and table_name = :t"
                    " order by ordinal_position"
                ),
                {"s": schema, "t": table},
            ).all()
        return [r[0] for r in rows] or None

    # ---------------------------------------------------------------- ServingStore

    def replace(self, schema: str, table: str, rows: pa.Table) -> int:
        s, t, tmp = _ident(schema), _ident(table), _ident(f"{table}__load")
        raw = self.engine.raw_connection()
        try:
            dbapi = raw.driver_connection
            self._ensure_schema(dbapi, schema)
            with dbapi.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {s}.{tmp}")
                cur.execute(f"CREATE TABLE {s}.{tmp} ({self._columns(rows.schema)})")
            self._copy(dbapi, f"{s}.{tmp}", rows)
            with dbapi.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {s}.{t}")
                cur.execute(f"ALTER TABLE {s}.{tmp} RENAME TO {t}")
            dbapi.commit()
        except Exception:
            raw.driver_connection.rollback()
            raise
        finally:
            raw.close()
        return rows.num_rows

    def upsert(self, schema: str, table: str, rows: pa.Table, keys: list[str]) -> int:
        s, t = _ident(schema), _ident(table)
        existing = self.table_columns(schema, table)
        if existing is None:
            raise LookupError(f"{schema}.{table} does not exist in the serving database")
        raw = self.engine.raw_connection()
        try:
            dbapi = raw.driver_connection
            with dbapi.cursor() as cur:
                cur.execute(f"CREATE TEMP TABLE __rows ({self._columns(rows.schema)}) ON COMMIT DROP")
            self._copy(dbapi, "__rows", rows)
            cols = [_ident(c) for c in rows.column_names]
            key_cols = ", ".join(_ident(k) for k in keys)
            updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in {_ident(k) for k in keys})
            with dbapi.cursor() as cur:
                cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {_ident(f'{table}__key')} ON {s}.{t} ({key_cols})")
                cur.execute(
                    f"INSERT INTO {s}.{t} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM __rows "
                    f"ON CONFLICT ({key_cols}) DO {'UPDATE SET ' + updates if updates else 'NOTHING'}"
                )
            dbapi.commit()
        except Exception:
            raw.driver_connection.rollback()
            raise
        finally:
            raw.close()
        return rows.num_rows

    def read(self, schema: str, table: str, limit: int = 1000) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            result = c.execute(text(f"select * from {_ident(schema)}.{_ident(table)} limit :n"), {"n": limit})
            return [dict(r._mapping) for r in result]

    def grant_read(self, schema: str, table: str, role: str) -> None:
        with self.engine.begin() as c:
            c.execute(text(f"GRANT USAGE ON SCHEMA {_ident(schema)} TO {_ident(role)}"))
            c.execute(text(f"GRANT SELECT ON {_ident(schema)}.{_ident(table)} TO {_ident(role)}"))

    def health(self) -> bool:
        try:
            with self.engine.connect() as c:
                c.execute(text("select 1"))
            return True
        except Exception:
            return False

    def close(self) -> None:
        self.engine.dispose()
