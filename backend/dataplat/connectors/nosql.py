"""MongoDB collections."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pyarrow as pa
from pydantic import BaseModel, Field

from dataplat.connectors.base import Batch, Connector, ConnectorError, ReadContext, ReadRequest, SourceObject
from dataplat.connectors.files import normalize_records


class MongoConfig(BaseModel):
    host: str
    port: int = 27017
    database: str
    username: str | None = None
    password: str | None = Field(default=None, description="vault:// reference")
    auth_source: str = "admin"
    tls: bool = False


class MongoConnector(Connector):
    type = "mongodb"
    label = "MongoDB"
    category = "nosql"
    Config = MongoConfig
    secret_fields = ("password",)

    def __init__(self, config: dict[str, Any], secrets: Any = None) -> None:
        super().__init__(config, secrets)
        self._client: Any = None

    @property
    def db(self) -> Any:
        from pymongo import MongoClient

        if self._client is None:
            c = self.config
            kwargs: dict[str, Any] = {"serverSelectionTimeoutMS": 10_000, "tls": c.tls}
            if c.username:
                kwargs.update(username=c.username, password=self.secret("password"), authSource=c.auth_source)
            self._client = MongoClient(c.host, c.port, **kwargs)
        return self._client[self.config.database]

    def namespace(self) -> str:
        return f"mongodb://{self.config.host}:{self.config.port}/{self.config.database}"

    def test(self) -> dict[str, Any]:
        from pymongo.errors import PyMongoError

        try:
            info = self.db.client.server_info()
            self.db.list_collection_names()
        except PyMongoError as e:
            raise ConnectorError(str(e).splitlines()[0][:300]) from e
        return {"server_version": info.get("version")}

    def discover(self, pattern: str | None = None) -> list[SourceObject]:
        names = sorted(self.db.list_collection_names())
        return [SourceObject(name=n, kind="collection") for n in names if not pattern or pattern in n]

    def read(self, request: ReadRequest, ctx: ReadContext) -> Iterator[Batch]:
        from pymongo.errors import PyMongoError

        if not request.object:
            raise ConnectorError("choose a collection")
        coll = self.db[request.object]
        query: dict[str, Any] = dict(request.options.get("filter") or {})
        wm = request.watermark_column if request.load_mode == "incremental" else None
        if wm and request.last_watermark is not None:
            query[wm] = {"$gt": request.last_watermark}
        cursor = coll.find(query, batch_size=min(request.batch_size, 10_000))
        if wm:
            cursor = cursor.sort(wm, 1)
        if request.max_records is not None:
            cursor = cursor.limit(request.max_records)
        ctx.inputs.append(request.object)
        ctx.query = f"db.{request.object}.find({query!r})"
        new_wm = request.last_watermark
        buf: list[dict[str, Any]] = []
        try:
            for doc in cursor:
                if wm and doc.get(wm) is not None and (new_wm is None or doc[wm] > new_wm):
                    new_wm = doc[wm]
                doc["_id"] = str(doc["_id"])
                buf.append(doc)
                if len(buf) >= request.batch_size:
                    yield self._batch(buf, request, ctx)
                    buf = []
        except PyMongoError as e:
            raise ConnectorError(str(e).splitlines()[0][:300]) from e
        if buf:
            yield self._batch(buf, request, ctx)
        ctx.new_watermark = new_wm

    def _batch(self, docs: list[dict[str, Any]], request: ReadRequest, ctx: ReadContext) -> Batch:
        table = pa.Table.from_pylist(normalize_records(docs))
        ctx.rows += table.num_rows
        return Batch(table, source=f"{self.namespace()}/{request.object}")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
