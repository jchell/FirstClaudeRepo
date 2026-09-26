"""Shared contract suites. Every adapter for a port — local or cloud — must pass these.

Each suite is a function taking a fresh adapter instance; contract test modules
parametrize them over the adapters available in that environment.
"""

from __future__ import annotations

import uuid

import pytest

from dataplat.core.ports import EventBus, LineageSink, ObjectStore, SecretStore
from dataplat.core.secrets import SecretNotFound
from dataplat.lineage.openlineage import dataset, run_event


def secret_store_contract(store: SecretStore) -> None:
    assert isinstance(store, SecretStore)
    path = f"contract/{uuid.uuid4().hex[:8]}"
    assert store.write(path, {"user": "u", "password": "p1"}) == 1
    assert store.read(path) == {"user": "u", "password": "p1"}
    assert store.write(path, {"user": "u", "password": "p2"}) == 2
    ref = store.ref(path, "password")
    assert str(ref).startswith("vault://")
    assert store.resolve(ref) == "p2"
    assert store.resolve(str(ref)) == "p2"
    meta = store.metadata(path)
    assert meta["current_version"] == 2 and "p2" not in repr(meta)
    assert path.split("/")[-1] in store.list("contract")
    with pytest.raises(SecretNotFound):
        store.resolve(store.ref(path, "missing"))
    store.delete(path)
    with pytest.raises(SecretNotFound):
        store.read(path)
    with pytest.raises(ValueError):
        store.read("../escape")
    assert store.health()


def object_store_contract(store: ObjectStore) -> None:
    assert isinstance(store, ObjectStore)
    bucket = f"contract-{uuid.uuid4().hex[:8]}"
    store.ensure_bucket(bucket)
    store.ensure_bucket(bucket)  # idempotent
    store.put_bytes(bucket, "a/one.txt", b"1")
    store.put_bytes(bucket, "a/two.txt", b"22")
    store.put_bytes(bucket, "b/three.txt", b"333")
    assert store.get_bytes(bucket, "a/two.txt") == b"22"
    assert store.exists(bucket, "a/one.txt") and not store.exists(bucket, "nope")
    assert store.list(bucket, "a/") == ["a/one.txt", "a/two.txt"]
    assert len(store.list(bucket)) == 3
    store.delete(bucket, "a/one.txt")
    assert not store.exists(bucket, "a/one.txt")
    with pytest.raises(FileNotFoundError):
        store.get_bytes(bucket, "a/one.txt")
    assert store.uri(bucket, "k").endswith(f"{bucket}/k")
    assert store.health()


def event_bus_contract(bus: EventBus) -> None:
    assert isinstance(bus, EventBus)
    topic = f"contract.{uuid.uuid4().hex[:8]}"
    group = f"g-{uuid.uuid4().hex[:6]}"
    bus.ensure_topic(topic)
    bus.ensure_topic(topic)
    for i in range(3):
        bus.publish(topic, {"i": i}, key=f"k{i}")
    bus.flush()

    got: list[int] = []
    for batch in bus.consume([topic], group, timeout=2.0):
        got.extend(e.value["i"] for e in batch)
        if len(got) >= 3 or not batch and got:
            bus.commit()
            break
    assert got == [0, 1, 2]

    bus.publish(topic, {"i": 3})
    bus.flush()
    for batch in bus.consume([topic], group, timeout=2.0):
        if batch:
            # Committed offsets: only the new event is redelivered to the same group.
            assert [e.value["i"] for e in batch] == [3]
            bus.commit()
            break
    assert bus.health()


def lineage_sink_contract(sink: LineageSink) -> None:
    job = f"contract.job.{uuid.uuid4().hex[:6]}"
    start = run_event("START", "dataplat", job, inputs=[dataset("postgres://src", "public.customers")])
    sink.emit(start)
    sink.emit(
        run_event(
            "COMPLETE", "dataplat", job, run_id=start["run"]["runId"], outputs=[dataset("s3://bronze", "customers")]
        )
    )
    events = sink.events(job)
    assert [e["eventType"] for e in events] == ["COMPLETE", "START"]
    assert events[1]["inputs"][0]["name"] == "public.customers"
    with pytest.raises(ValueError):
        sink.emit({"eventType": "START"})
