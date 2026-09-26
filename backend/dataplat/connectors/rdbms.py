"""Relational sources through SQLAlchemy: Postgres, MySQL, SQL Server, Oracle."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any, ClassVar

import pyarrow as pa
from pydantic import BaseModel, Field
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import SQLAlchemyError

from dataplat.connectors.base import Batch, Connector, ConnectorError, ReadContext, ReadRequest, SourceObject

SYSTEM_SCHEMAS = {
    "information_schema",
    "pg_catalog",
    "pg_toast",
    "mysql",
    "performance_schema",
    "sys",
    "guest",
    "INFORMATION_SCHEMA",
    "db_owner",
    "db_accessadmin",
    "db_securityadmin",
    "db_ddladmin",
    "db_backupoperator",
    "db_datareader",
    "db_datawriter",
    "db_denydatareader",
    "db_denydatawriter",
}


class DatabaseConfig(BaseModel):
    host: str
    port: int | None = None
    database: str
    username: str
    password: str | None = Field(default=None, description="vault:// reference")
    options: dict[str, str] = Field(default_factory=dict, description="Extra driver options (query string)")


class DatabaseConnector(Connector):
    category = "database"
    Config = DatabaseConfig
    secret_fields = ("password",)
    driver: ClassVar[str]
    default_port: ClassVar[int]
    scheme: ClassVar[str]  # lineage namespace scheme

    def __init__(self, config: dict[str, Any], secrets: Any = None) -> None:
        super().__init__(config, secrets)
        self._engine: Engine | None = None

    @property
    def port(self) -> int:
        return self.config.port or self.default_port

    def url(self) -> URL:
        c = self.config
        return URL.create(
            self.driver,
            username=c.username,
            password=self.secret("password"),
            host=c.host,
            port=self.port,
            database=c.database,
            query=c.options,
        )

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(self.url(), pool_pre_ping=True, pool_size=1, max_overflow=0)
        return self._engine

    def namespace(self) -> str:
        return f"{self.scheme}://{self.config.host}:{self.port}/{self.config.database}"

    def version_query(self) -> str:
        return "select version()"

    def test(self) -> dict[str, Any]:
        try:
            with self.engine.connect() as conn:
                version = conn.execute(text(self.version_query())).scalar()
        except SQLAlchemyError as e:
            raise ConnectorError(_clean(e)) from e
        return {"server_version": str(version)[:200]}

    def discover(self, pattern: str | None = None) -> list[SourceObject]:
        try:
            insp = inspect(self.engine)
            out = []
            for schema in insp.get_schema_names():
                if schema in SYSTEM_SCHEMAS or schema.startswith("pg_"):
                    continue
                for kind, names in (("table", insp.get_table_names(schema)), ("view", insp.get_view_names(schema))):
                    for name in names:
                        full = f"{schema}.{name}"
                        if pattern and pattern.lower() not in full.lower():
                            continue
                        cols = [{"name": c["name"], "type": str(c["type"])} for c in insp.get_columns(name, schema)]
                        out.append(SourceObject(name=full, kind=kind, columns=cols))
            return out
        except SQLAlchemyError as e:
            raise ConnectorError(_clean(e)) from e

    def _statement(self, request: ReadRequest) -> tuple[Any, dict[str, Any], str]:
        """Builds the SELECT, its parameters, and the lineage dataset name."""
        params: dict[str, Any] = {}
        incremental = request.load_mode == "incremental" and request.watermark_column
        if request.query:
            sql = request.query.strip().rstrip(";")
            name = "query:" + hashlib.sha256(sql.encode()).hexdigest()[:12]
            if incremental:
                wm = self.engine.dialect.identifier_preparer.quote(request.watermark_column)
                where = f" WHERE {wm} > :_wm" if request.last_watermark is not None else ""
                sql = f"SELECT * FROM ({sql}) dp_src{where} ORDER BY {wm}"
                if request.last_watermark is not None:
                    params["_wm"] = request.last_watermark
            return text(sql), params, name

        if not request.object:
            raise ConnectorError("choose a table or write a query")
        schema, _, table_name = request.object.rpartition(".")
        try:
            table = Table(table_name, MetaData(), schema=schema or None, autoload_with=self.engine)
        except SQLAlchemyError as e:
            raise ConnectorError(_clean(e)) from e
        stmt = select(table)
        if incremental:
            if request.watermark_column not in table.c:
                raise ConnectorError(f"watermark column {request.watermark_column!r} not in {request.object}")
            col = table.c[request.watermark_column]
            if request.last_watermark is not None:
                stmt = stmt.where(col > request.last_watermark)
            stmt = stmt.order_by(col)
        if request.max_records is not None:
            stmt = stmt.limit(request.max_records)
        return stmt, params, request.object

    def read(self, request: ReadRequest, ctx: ReadContext) -> Iterator[Batch]:
        stmt, params, name = self._statement(request)
        ctx.inputs.append(name)
        ctx.query = str(stmt.compile(self.engine, compile_kwargs={"literal_binds": False}))
        wm_col = request.watermark_column if request.load_mode == "incremental" else None
        new_wm = request.last_watermark
        try:
            with self.engine.connect().execution_options(
                stream_results=True, max_row_buffer=request.batch_size
            ) as conn:
                result = conn.execute(stmt, params)
                columns = list(result.keys())
                while rows := result.fetchmany(request.batch_size):
                    if request.max_records is not None:
                        rows = rows[: max(request.max_records - ctx.rows, 0)]
                    table = rows_to_arrow(columns, rows)
                    if wm_col:
                        values = [v for v in table.column(wm_col).to_pylist() if v is not None]
                        if values:
                            top = max(values)
                            new_wm = top if new_wm is None or top > new_wm else new_wm
                    ctx.rows += table.num_rows
                    yield Batch(table, source=f"{self.namespace()}/{name}")
                    if request.max_records is not None and ctx.rows >= request.max_records:
                        break
        except SQLAlchemyError as e:
            raise ConnectorError(_clean(e)) from e
        ctx.new_watermark = new_wm

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()


def rows_to_arrow(columns: list[str], rows: list[Any]) -> pa.Table:
    data = {c: [r[i] for r in rows] for i, c in enumerate(columns)}
    arrays = {}
    for c, values in data.items():
        try:
            arrays[c] = pa.array(values)
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            # Mixed or driver-specific types (e.g. UUID objects): keep them as text.
            arrays[c] = pa.array([None if v is None else str(v) for v in values], pa.string())
    return pa.table(arrays)


def _clean(e: Exception) -> str:
    """Driver messages without the SQLAlchemy boilerplate (and never the URL)."""
    msg = str(getattr(e, "orig", None) or e).strip().splitlines()[0]
    return msg[:500]


class PostgresConnector(DatabaseConnector):
    type = "postgres"
    label = "PostgreSQL"
    driver = "postgresql+psycopg"
    default_port = 5432
    scheme = "postgres"


class MySqlConnector(DatabaseConnector):
    type = "mysql"
    label = "MySQL / MariaDB"
    driver = "mysql+pymysql"
    default_port = 3306
    scheme = "mysql"


class SqlServerConnector(DatabaseConnector):
    type = "sqlserver"
    label = "Microsoft SQL Server"
    driver = "mssql+pymssql"
    default_port = 1433
    scheme = "sqlserver"

    def version_query(self) -> str:
        return "select @@version"


class OracleConnector(DatabaseConnector):
    type = "oracle"
    label = "Oracle"
    driver = "oracle+oracledb"
    default_port = 1521
    scheme = "oracle"

    def url(self) -> URL:
        # `database` is the service name.
        c = self.config
        return URL.create(
            self.driver,
            username=c.username,
            password=self.secret("password"),
            host=c.host,
            port=self.port,
            query={"service_name": c.database, **c.options},
        )

    def version_query(self) -> str:
        return "select banner from v$version where rownum = 1"
