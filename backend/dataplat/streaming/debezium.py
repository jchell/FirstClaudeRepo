"""Builds Debezium connector configs for CDC ingestion jobs.

Credentials appear only as ``${vault:<path>:<key>}`` placeholders, resolved inside
Kafka Connect by the platform's Vault config provider. Change events are flattened
(ExtractNewRecordState) into plain JSON rows with ``__op``, ``__source_ts_ms`` and a
``__deleted`` flag, on topics named ``cdc.<source>.<schema>.<table>``.
"""

from __future__ import annotations

import re
import uuid
import zlib
from typing import Any
from urllib.parse import quote

from dataplat.core.config import PlatformConfig
from dataplat.core.secrets import SecretRef
from dataplat.ingestion.spec import JobSpec

CONNECTORS = {
    "postgres": "io.debezium.connector.postgresql.PostgresConnector",
    "mysql": "io.debezium.connector.mysql.MySqlConnector",
    "sqlserver": "io.debezium.connector.sqlserver.SqlServerConnector",
    "mongodb": "io.debezium.connector.mongodb.MongoDbConnector",
}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "src"


def vault_placeholder(ref: str) -> str:
    """vault://kv/dataplat/x#password -> ${vault:kv/data/dataplat/x:password} (KV v2 data path)."""
    r = SecretRef.parse(ref)
    return f"${{vault:{r.mount}/data/{r.path}:{r.key}}}"


def connector_name(job_id: uuid.UUID) -> str:
    return f"dataplat-cdc-{job_id.hex[:12]}"


def topic_prefix(connection_name: str) -> str:
    return f"cdc.{slug(connection_name)}"


def cdc_topic(conn_type: str, connection_name: str, config: dict[str, Any], table: str) -> str:
    """The topic Debezium writes the table's changes to."""
    prefix = topic_prefix(connection_name)
    if conn_type == "sqlserver":
        return f"{prefix}.{config['database']}.{table}"  # <db>.<schema>.<table>
    if conn_type in ("mysql", "mongodb"):
        return f"{prefix}.{config['database']}.{table.split('.')[-1]}"
    return f"{prefix}.{table}"  # postgres: <schema>.<table>


def build_config(
    cfg: PlatformConfig,
    job_id: uuid.UUID,
    conn_type: str,
    connection_name: str,
    config: dict[str, Any],
    spec: JobSpec,
) -> dict[str, str]:
    if conn_type not in CONNECTORS:
        raise ValueError(f"no CDC support for {conn_type}")
    table = spec.source.object or ""
    short = job_id.hex[:12]
    prefix = topic_prefix(connection_name)
    password = config.get("password")
    common: dict[str, Any] = {
        "connector.class": CONNECTORS[conn_type],
        "tasks.max": "1",
        "topic.prefix": prefix,
        "snapshot.mode": "initial" if spec.stream.snapshot == "initial" else "no_data",
        "tombstones.on.delete": "false",
        "decimal.handling.mode": "string",
        "transforms": "unwrap",
        "transforms.unwrap.delete.tombstone.handling.mode": "rewrite",
        "transforms.unwrap.add.fields": "op,source.ts_ms",
        "key.converter": "org.apache.kafka.connect.json.JsonConverter",
        "key.converter.schemas.enable": "false",
        "value.converter": "org.apache.kafka.connect.json.JsonConverter",
        "value.converter.schemas.enable": "false",
        # Errors surface in the connector status instead of silently skipping changes.
        "errors.tolerance": "none",
    }
    history = {
        "schema.history.internal.kafka.bootstrap.servers": cfg.event_bus.bootstrap_servers,
        "schema.history.internal.kafka.topic": f"cdc.history.{short}",
    }
    port = config.get("port")

    if conn_type == "postgres":
        c = {
            **common,
            "transforms.unwrap.type": "io.debezium.transforms.ExtractNewRecordState",
            "database.hostname": config["host"],
            "database.port": str(port or 5432),
            "database.user": config["username"],
            "database.dbname": config["database"],
            "table.include.list": table,
            "plugin.name": "pgoutput",
            "slot.name": f"dataplat_{short}",
            "publication.name": f"dataplat_{short}",
            "publication.autocreate.mode": "filtered",
        }
    elif conn_type == "mysql":
        c = {
            **common,
            **history,
            "transforms.unwrap.type": "io.debezium.transforms.ExtractNewRecordState",
            "database.hostname": config["host"],
            "database.port": str(port or 3306),
            "database.user": config["username"],
            "database.server.id": str(10_000 + zlib.crc32(job_id.bytes) % 2_000_000_000),
            "database.include.list": config["database"],
            "table.include.list": f"{config['database']}.{table.split('.')[-1]}",
        }
    elif conn_type == "sqlserver":
        c = {
            **common,
            **history,
            "transforms.unwrap.type": "io.debezium.transforms.ExtractNewRecordState",
            "database.hostname": config["host"],
            "database.port": str(port or 1433),
            "database.user": config["username"],
            "database.names": config["database"],
            "database.encrypt": "false",
            "table.include.list": table,
        }
    else:  # mongodb
        auth = ""
        if config.get("username"):
            user = quote(config["username"], safe="")
            auth = f"{user}:{vault_placeholder(password)}@" if password else f"{user}@"
        options = f"authSource={config.get('auth_source', 'admin')}"
        if config.get("replica_set"):
            options += f"&replicaSet={config['replica_set']}"
        if config.get("tls"):
            options += "&tls=true"
        c = {
            **common,
            "transforms.unwrap.type": "io.debezium.connector.mongodb.transforms.ExtractNewDocumentState",
            "mongodb.connection.string": f"mongodb://{auth}{config['host']}:{port or 27017}/?{options}",
            "collection.include.list": f"{config['database']}.{table}",
            "capture.mode": "change_streams_update_full",
        }
        return {k: str(v) for k, v in c.items()}

    if password:
        c["database.password"] = vault_placeholder(password)
    return {k: str(v) for k, v in c.items()}
