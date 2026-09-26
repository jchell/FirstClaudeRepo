"""EventBus on Redpanda (any Kafka-API broker) via confluent-kafka."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from confluent_kafka.admin import AdminClient, NewTopic

from dataplat.core.config import PlatformConfig
from dataplat.core.ports.events import Event

log = logging.getLogger(__name__)


class RedpandaEventBus:
    def __init__(self, bootstrap_servers: str) -> None:
        self.bootstrap_servers = bootstrap_servers
        self._admin = AdminClient({"bootstrap.servers": bootstrap_servers})
        self._producer: Producer | None = None
        self._consumer: Consumer | None = None

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> RedpandaEventBus:
        return cls(config.event_bus.bootstrap_servers)

    @property
    def producer(self) -> Producer:
        if self._producer is None:
            self._producer = Producer(
                {"bootstrap.servers": self.bootstrap_servers, "enable.idempotence": True, "acks": "all"}
            )
        return self._producer

    def ensure_topic(self, topic: str, partitions: int = 1) -> None:
        if topic in self._admin.list_topics(timeout=10).topics:
            return
        futures = self._admin.create_topics([NewTopic(topic, num_partitions=partitions, replication_factor=1)])
        try:
            futures[topic].result(timeout=10)
        except KafkaException as e:
            if e.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
                raise

    def publish(self, topic: str, value: dict[str, Any], key: str | None = None) -> None:
        self.producer.produce(topic, json.dumps(value, default=str).encode(), key=key.encode() if key else None)
        self.producer.poll(0)

    def flush(self, timeout: float = 10) -> None:
        if self._producer is not None:
            remaining = self._producer.flush(timeout)
            if remaining:
                raise TimeoutError(f"{remaining} events not delivered within {timeout}s")

    def consume(
        self, topics: list[str], group: str, timeout: float = 1.0, max_messages: int = 500
    ) -> Iterator[list[Event]]:
        self._consumer = Consumer(
            {
                "bootstrap.servers": self.bootstrap_servers,
                "group.id": group,
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
            }
        )
        self._consumer.subscribe(topics)
        try:
            while True:
                msgs = self._consumer.consume(num_messages=max_messages, timeout=timeout)
                batch = []
                for m in msgs:
                    if m.error():
                        if m.error().code() != KafkaError._PARTITION_EOF:
                            log.warning("consume error: %s", m.error())
                        continue
                    batch.append(
                        Event(
                            topic=m.topic(),
                            key=m.key().decode() if m.key() else None,
                            value=json.loads(m.value()),
                            offset=m.offset(),
                            partition=m.partition(),
                            timestamp_ms=m.timestamp()[1],
                        )
                    )
                yield batch
        finally:
            self._consumer.close()
            self._consumer = None

    def commit(self) -> None:
        if self._consumer is not None:
            self._consumer.commit(asynchronous=False)

    def health(self) -> bool:
        try:
            self._admin.list_topics(timeout=5)
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self._producer is not None:
            self._producer.flush(5)
