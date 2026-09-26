"""Stream processor pieces that don't need Kafka."""

from __future__ import annotations

import json

import pyarrow as pa

from dataplat.streaming.processor import StreamRunner


def test_key_values_fill_mongodb_deletes() -> None:
    key = json.dumps({"id": "6ab7e8cba4ede4f8cea725b5"}).encode()
    assert StreamRunner._key_values(key, {"__deleted": True}) == {"_id": "6ab7e8cba4ede4f8cea725b5"}
    assert StreamRunner._key_values(json.dumps({"id": 3}).encode(), {"id": 3, "name": "x"}) == {"id": 3}
    assert StreamRunner._key_values(None, {}) == {}
    assert StreamRunner._key_values(b"not json", {}) == {}


def test_latest_change_per_key_wins_within_a_batch() -> None:
    r = StreamRunner.__new__(StreamRunner)
    r._key_columns = ["id"]
    t = pa.table({"id": [1, 2, 1, 3, 2], "v": ["a", "b", "a2", "c", "b2"]})
    out = r._latest_per_key(t)
    assert sorted(zip(out.column("id").to_pylist(), out.column("v").to_pylist(), strict=True)) == [
        (1, "a2"),
        (2, "b2"),
        (3, "c"),
    ]
