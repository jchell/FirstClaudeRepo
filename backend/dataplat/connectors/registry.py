"""Connector types by name. Third-party packages can add more via entry points."""

from __future__ import annotations

from importlib.metadata import entry_points

from dataplat.connectors.api import RestApiConnector
from dataplat.connectors.base import Connector
from dataplat.connectors.events import KafkaConnector, WebhookConnector
from dataplat.connectors.files import FtpConnector, LocalFilesConnector, S3Connector, SftpConnector, SmbConnector
from dataplat.connectors.nosql import MongoConnector
from dataplat.connectors.rdbms import MySqlConnector, OracleConnector, PostgresConnector, SqlServerConnector

BUILTIN: list[type[Connector]] = [
    LocalFilesConnector,
    SftpConnector,
    FtpConnector,
    SmbConnector,
    S3Connector,
    PostgresConnector,
    MySqlConnector,
    SqlServerConnector,
    OracleConnector,
    MongoConnector,
    RestApiConnector,
    KafkaConnector,
    WebhookConnector,
]


def connector_types() -> dict[str, type[Connector]]:
    types = {c.type: c for c in BUILTIN}
    for ep in entry_points(group="dataplat.connectors"):
        cls = ep.load()
        types[cls.type] = cls
    return types


def get_connector_class(type_name: str) -> type[Connector]:
    try:
        return connector_types()[type_name]
    except KeyError as e:
        raise ValueError(f"unknown connection type {type_name!r}") from e
