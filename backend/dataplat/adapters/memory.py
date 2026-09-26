"""In-memory adapters for unit tests and the shared contract test suite."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from dataplat.core.config import PlatformConfig
from dataplat.core.ports.events import Event
from dataplat.core.secrets import SecretNotFound, SecretRef
from dataplat.lineage.openlineage import validate_run_event


class InMemorySecretStore:
    def __init__(self, mount: str = "kv", prefix: str = "dataplat") -> None:
        self.mount = mount
        self.prefix = prefix
        self._data: dict[str, list[dict[str, str]]] = {}
        self._times: dict[str, list[datetime]] = {}

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> InMemorySecretStore:
        return cls(config.vault.kv_mount, config.vault.kv_prefix)

    def _full(self, path: str) -> str:
        path = path.strip("/")
        if ".." in path.split("/"):
            raise ValueError("path traversal in secret path")
        return path if path.startswith(self.prefix + "/") else f"{self.prefix}/{path}"

    def write(self, path: str, data: dict[str, str]) -> int:
        full = self._full(path)
        self._data.setdefault(full, []).append(dict(data))
        self._times.setdefault(full, []).append(datetime.now(UTC))
        return len(self._data[full])

    def read(self, path: str) -> dict[str, str]:
        full = self._full(path)
        if full not in self._data:
            raise SecretNotFound(path)
        return dict(self._data[full][-1])

    def resolve(self, ref: SecretRef | str) -> str:
        ref = SecretRef.parse(ref) if isinstance(ref, str) else ref
        if ref.mount != self.mount:
            raise ValueError(f"reference points at mount {ref.mount!r}, store serves {self.mount!r}")
        data = self.read(ref.path)
        if ref.key not in data:
            raise SecretNotFound(str(ref))
        return data[ref.key]

    def delete(self, path: str) -> None:
        full = self._full(path)
        self._data.pop(full, None)
        self._times.pop(full, None)

    def metadata(self, path: str) -> dict[str, Any]:
        full = self._full(path)
        if full not in self._data:
            raise SecretNotFound(path)
        times = self._times[full]
        return {
            "path": full,
            "current_version": len(self._data[full]),
            "created_time": times[0].isoformat(),
            "updated_time": times[-1].isoformat(),
            "versions": len(times),
        }

    def list(self, prefix: str) -> list[str]:
        base = self._full(prefix).rstrip("/") + "/"
        keys = set()
        for full in self._data:
            if full.startswith(base):
                rest = full[len(base) :]
                keys.add(rest.split("/", 1)[0] + ("/" if "/" in rest else ""))
        return sorted(keys)

    def ref(self, path: str, key: str) -> SecretRef:
        return SecretRef(self.mount, self._full(path), key)

    def health(self) -> bool:
        return True


class InMemoryObjectStore:
    def __init__(self) -> None:
        self._buckets: dict[str, dict[str, bytes]] = {}

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> InMemoryObjectStore:
        return cls()

    def ensure_bucket(self, bucket: str) -> None:
        self._buckets.setdefault(bucket, {})

    def _bucket(self, bucket: str) -> dict[str, bytes]:
        if bucket not in self._buckets:
            raise FileNotFoundError(bucket)
        return self._buckets[bucket]

    def put_bytes(self, bucket: str, key: str, data: bytes) -> None:
        self._bucket(bucket)[key] = bytes(data)

    def get_bytes(self, bucket: str, key: str) -> bytes:
        try:
            return self._bucket(bucket)[key]
        except KeyError as e:
            raise FileNotFoundError(f"{bucket}/{key}") from e

    def exists(self, bucket: str, key: str) -> bool:
        return key in self._buckets.get(bucket, {})

    def list(self, bucket: str, prefix: str = "") -> list[str]:
        return sorted(k for k in self._bucket(bucket) if k.startswith(prefix))

    def delete(self, bucket: str, key: str) -> None:
        self._bucket(bucket).pop(key, None)

    def uri(self, bucket: str, key: str = "") -> str:
        return f"memory://{bucket}/{key}"

    def storage_options(self) -> dict[str, str]:
        return {}

    def health(self) -> bool:
        return True


class InMemoryEventBus:
    def __init__(self) -> None:
        self._topics: dict[str, list[Event]] = {}
        self._offsets: dict[tuple[str, str], int] = {}
        self._pending: dict[tuple[str, str], int] = {}

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> InMemoryEventBus:
        return cls()

    def ensure_topic(self, topic: str, partitions: int = 1) -> None:
        self._topics.setdefault(topic, [])

    def publish(self, topic: str, value: dict[str, Any], key: str | None = None) -> None:
        log = self._topics.setdefault(topic, [])
        log.append(Event(topic, key, copy.deepcopy(value), offset=len(log), partition=0))

    def flush(self, timeout: float = 10) -> None:
        pass

    def consume(
        self, topics: list[str], group: str, timeout: float = 1.0, max_messages: int = 500
    ) -> Iterator[list[Event]]:
        while True:
            batch: list[Event] = []
            for t in topics:
                start = self._offsets.get((group, t), 0)
                events = self._topics.get(t, [])[start : start + max_messages - len(batch)]
                batch.extend(copy.deepcopy(events))
                self._pending[(group, t)] = start + len(events)
            yield batch

    def commit(self) -> None:
        self._offsets.update(self._pending)

    def health(self) -> bool:
        return True


class InMemoryLineageSink:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> InMemoryLineageSink:
        return cls()

    def emit(self, event: dict[str, Any]) -> None:
        validate_run_event(event)
        self._events.append(copy.deepcopy(event))

    def events(self, job_name: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        matching = [e for e in reversed(self._events) if job_name is None or e["job"]["name"] == job_name]
        return matching[:limit]
