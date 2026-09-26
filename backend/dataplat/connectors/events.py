"""Kafka topics (Redpanda, MSK, Confluent...) read as batch: everything since the last run."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Literal

import pyarrow as pa
from pydantic import BaseModel, Field

from dataplat.connectors.base import Batch, Connector, ConnectorError, ReadContext, ReadRequest, SourceObject
from dataplat.connectors.files import normalize_records


class KafkaConfig(BaseModel):
    bootstrap_servers: str
    security_protocol: Literal["PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"] = "PLAINTEXT"
    sasl_mechanism: Literal["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"] | None = None
    username: str | None = None
    password: str | None = Field(default=None, description="vault:// reference")


class KafkaConnector(Connector):
    """Each ingestion job is its own consumer group, so each run picks up where the last stopped.

    Offsets are committed only after the batch has been written (ReadContext.extra["commit"]).
    """

    type = "kafka"
    label = "Kafka topic"
    category = "event"
    Config = KafkaConfig
    secret_fields = ("password",)

    def _conf(self) -> dict[str, Any]:
        c = self.config
        conf: dict[str, Any] = {"bootstrap.servers": c.bootstrap_servers, "security.protocol": c.security_protocol}
        if c.sasl_mechanism:
            conf.update(
                {
                    "sasl.mechanism": c.sasl_mechanism,
                    "sasl.username": c.username,
                    "sasl.password": self.secret("password"),
                }
            )
        return conf

    def namespace(self) -> str:
        return f"kafka://{self.config.bootstrap_servers.split(',')[0]}"

    def test(self) -> dict[str, Any]:
        from confluent_kafka.admin import AdminClient

        try:
            md = AdminClient(self._conf()).list_topics(timeout=10)
        except Exception as e:
            raise ConnectorError(f"cannot reach the brokers: {e}") from e
        return {"brokers": len(md.brokers), "topics": len(md.topics)}

    def discover(self, pattern: str | None = None) -> list[SourceObject]:
        from confluent_kafka.admin import AdminClient

        md = AdminClient(self._conf()).list_topics(timeout=10)
        return [
            SourceObject(name=t, kind="topic")
            for t in sorted(md.topics)
            if not t.startswith("_") and (not pattern or pattern in t)
        ]

    def read(self, request: ReadRequest, ctx: ReadContext) -> Iterator[Batch]:
        from confluent_kafka import Consumer, KafkaError, TopicPartition

        if not request.object:
            raise ConnectorError("choose a topic")
        group = request.options.get("consumer_group") or f"dataplat-ingest-{request.options.get('job_id', 'preview')}"
        preview = request.max_records is not None
        consumer = Consumer(
            {
                **self._conf(),
                "group.id": group,
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest" if request.options.get("from_beginning", True) else "latest",
                "enable.partition.eof": True,
            }
        )
        md = consumer.list_topics(request.object, timeout=10)
        topic_md = md.topics.get(request.object)
        if topic_md is None or topic_md.error is not None:
            consumer.close()
            raise ConnectorError(f"topic {request.object!r} not found")
        partitions = list(topic_md.partitions)
        consumer.assign([TopicPartition(request.object, p) for p in partitions])
        committed = {
            tp.partition: tp.offset
            for tp in consumer.committed([TopicPartition(request.object, p) for p in partitions], timeout=10)
        }
        done: set[int] = set()
        ctx.inputs.append(request.object)
        positions: dict[int, int] = {}
        buf: list[dict[str, Any]] = []
        try:
            while len(done) < len(partitions):
                msgs = consumer.consume(num_messages=min(request.batch_size, 5000), timeout=5)
                if not msgs:
                    break
                for m in msgs:
                    if m.error():
                        if m.error().code() == KafkaError._PARTITION_EOF:
                            done.add(m.partition())
                        continue
                    try:
                        value = json.loads(m.value()) if m.value() else None
                    except json.JSONDecodeError:
                        value = {"value": m.value().decode("utf-8", "replace")}
                    record = value if isinstance(value, dict) else {"value": value}
                    record = {
                        **record,
                        "_kafka_partition": m.partition(),
                        "_kafka_offset": m.offset(),
                        "_kafka_timestamp_ms": m.timestamp()[1],
                        "_kafka_key": m.key().decode() if m.key() else None,
                    }
                    positions[m.partition()] = m.offset() + 1
                    buf.append(record)
                    ctx.bytes += len(m.value() or b"")
                if len(buf) >= request.batch_size or (preview and len(buf) >= (request.max_records or 0)):
                    yield self._batch(buf[: request.max_records] if preview else buf, request, ctx)
                    buf = []
                    if preview:
                        return
            if buf:
                yield self._batch(buf, request, ctx)
        finally:
            if not preview and positions:
                offsets = [TopicPartition(request.object, p, o) for p, o in positions.items()]

                def commit() -> None:
                    c = Consumer({**self._conf(), "group.id": group, "enable.auto.commit": False})
                    try:
                        c.commit(offsets=offsets, asynchronous=False)
                    finally:
                        c.close()

                ctx.extra["commit"] = commit
            ctx.extra["start_offsets"] = {str(p): o for p, o in committed.items()}
            ctx.extra["end_offsets"] = {str(p): o for p, o in positions.items()}
            consumer.close()

    def _batch(self, records: list[dict[str, Any]], request: ReadRequest, ctx: ReadContext) -> Batch:
        table = pa.Table.from_pylist(normalize_records(records))
        ctx.rows += table.num_rows
        return Batch(table, source=f"{self.namespace()}/{request.object}")


class WebhookConfig(BaseModel):
    stream_name: str = Field(
        pattern=r"^[a-z][a-z0-9_-]{1,62}$", description="Events are posted to /ingest/events/<name>"
    )


class WebhookConnector(Connector):
    """Events pushed to the platform over HTTP (POST /ingest/events/{stream_name}).

    The API publishes accepted events to the platform topic ``webhook.<stream_name>``;
    a continuous ingestion job streams that topic into bronze.
    """

    type = "webhook"
    label = "Webhook (HTTP push)"
    category = "event"
    Config = WebhookConfig

    def topic(self) -> str:
        return f"webhook.{self.config.stream_name}"

    def namespace(self) -> str:
        return "webhook://dataplat"

    def test(self) -> dict[str, Any]:
        return {"endpoint": f"/ingest/events/{self.config.stream_name}", "topic": self.topic()}

    def discover(self, pattern: str | None = None) -> list[SourceObject]:
        return [SourceObject(name=self.topic(), kind="topic")]

    def read(self, request: ReadRequest, ctx: ReadContext) -> Iterator[Batch]:
        raise ConnectorError("webhook streams are read continuously by the stream worker")
