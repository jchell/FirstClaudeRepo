"""Connector SDK.

A connector knows how to talk to one kind of source. It can test the connection,
discover what can be read (tables, collections, files, topics, endpoints) and read
a source object as a stream of Arrow record batches. While reading it records what
it actually touched — the query text, the files and their checksums, the new
watermark — so ingestion can emit precise lineage and resume incrementally.

Secret configuration values are ``vault://`` references, resolved through the
job's service-account-scoped SecretStore only at the moment they're needed.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal

import pyarrow as pa
from pydantic import BaseModel

from dataplat.core.ports.secrets import SecretStore
from dataplat.core.secrets import SecretRef

LoadMode = Literal["full", "incremental", "append"]


class ConnectorError(Exception):
    """A problem with the source (unreachable, bad credentials, bad query...)."""


@dataclass
class SourceObject:
    """Something a connector can read."""

    name: str  # table "public.customers", file path, topic, endpoint path
    kind: str  # table | view | collection | file | topic | endpoint
    columns: list[dict[str, str]] = field(default_factory=list)
    size: int | None = None
    modified: datetime | None = None


class ReadRequest(BaseModel):
    """What to read. Which fields apply depends on the connector category."""

    object: str | None = None  # table/collection/topic/endpoint
    query: str | None = None  # SQL for queryable sources
    path_template: str | None = None  # files: /in/orders_{yyyyMMdd}*.csv
    format: Literal["csv", "json", "jsonl", "parquet"] | None = None
    format_options: dict[str, Any] = {}
    load_mode: LoadMode = "full"
    watermark_column: str | None = None
    last_watermark: Any = None
    # Files already ingested (path -> fingerprint), so "new files only" skips them.
    seen_files: dict[str, str] = {}
    batch_size: int = 50_000
    max_records: int | None = None  # previews
    run_date: datetime | None = None
    options: dict[str, Any] = {}


@dataclass
class FileRead:
    path: str
    size: int
    checksum: str  # sha256
    fingerprint: str  # size+mtime(+etag); cheap change detection before hashing
    rows: int = 0


@dataclass
class ReadContext:
    """Filled in by the connector while it reads."""

    namespace: str  # lineage namespace of the source, e.g. postgres://db:5432/sales
    inputs: list[str] = field(default_factory=list)  # lineage dataset names read
    query: str | None = None
    files: list[FileRead] = field(default_factory=list)
    new_watermark: Any = None
    rows: int = 0
    bytes: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Batch:
    data: pa.RecordBatch | pa.Table
    source: str  # value for the _source audit column
    file: str | None = None  # value for the _file audit column


class Connector(abc.ABC):
    """Base class; subclasses set the class attributes and implement the methods."""

    type: ClassVar[str]
    label: ClassVar[str]
    category: ClassVar[Literal["file", "database", "nosql", "api", "event"]]
    Config: ClassVar[type[BaseModel]]
    # Config fields that hold secrets (dotted for nested); stored in Vault, config keeps vault:// refs.
    secret_fields: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: dict[str, Any], secrets: SecretStore | None = None) -> None:
        self.config = self.Config.model_validate(config)
        self._secrets = secrets

    def secret(self, name: str) -> str | None:
        """Resolves a secret config field (a vault:// reference) at the point of use.

        ``name`` may be dotted for nested config, e.g. ``auth.token``.
        """
        value: Any = self.config
        for part in name.split("."):
            value = getattr(value, part, None)
        if value is None or value == "":
            return None
        if not SecretRef.is_ref(value):
            raise ConnectorError(f"config field {name!r} must be a vault:// reference")
        if self._secrets is None:
            raise ConnectorError("this connection needs secrets but the job has no service account")
        return self._secrets.resolve(value)

    @abc.abstractmethod
    def test(self) -> dict[str, Any]:
        """Connects and returns a few facts (server version...). Raises ConnectorError."""

    @abc.abstractmethod
    def discover(self, pattern: str | None = None) -> list[SourceObject]: ...

    @abc.abstractmethod
    def read(self, request: ReadRequest, ctx: ReadContext) -> Iterator[Batch]: ...

    @abc.abstractmethod
    def namespace(self) -> str:
        """Lineage namespace (no credentials), e.g. postgres://db:5432/sales."""

    def close(self) -> None:  # noqa: B027 - optional hook
        pass

    def __enter__(self) -> Connector:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def config_schema(cls: type[Connector]) -> dict[str, Any]:
    """JSON schema of a connector's config, with secret fields marked for the console."""
    schema = cls.Config.model_json_schema()
    schema["x-secret-fields"] = list(cls.secret_fields)
    for name in cls.secret_fields:
        props = schema.get("properties", {})
        head, _, rest = name.partition(".")
        if rest:  # nested model: follow its $ref into $defs
            ref = props.get(head, {}).get("$ref") or next(
                (a.get("$ref") for a in props.get(head, {}).get("allOf", []) if "$ref" in a), None
            )
            props = schema.get("$defs", {}).get(ref.rsplit("/", 1)[-1], {}).get("properties", {}) if ref else {}
            head = rest
        prop = props.get(head)
        if prop is not None:
            prop["format"] = "password"
            prop["writeOnly"] = True
            prop["x-secret"] = True
    return schema
