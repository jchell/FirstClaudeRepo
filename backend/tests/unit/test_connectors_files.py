from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dataplat.connectors.base import ConnectorError, ReadContext, ReadRequest, config_schema
from dataplat.connectors.files import LocalFilesConnector, infer_format, render_template
from dataplat.connectors.registry import connector_types

RUN = datetime(2026, 3, 5, 14, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("/in/orders_{yyyyMMdd}*.csv", "/in/orders_20260305*.csv"),
        ("/d/{yyyy}/{MM}/{dd}/x.json", "/d/2026/03/05/x.json"),
        ("/d/{yyyy-MM-dd}.csv", "/d/2026-03-05.csv"),
        ("/d/{yyyyMMdd-1d}.csv", "/d/20260304.csv"),
        ("/d/{yyyyMMdd+1w}.csv", "/d/20260312.csv"),
        ("/d/{yyyyMMddHH}.csv", "/d/2026030514.csv"),
        ("/d/{yyyyMMdd-2h}.csv", "/d/20260305.csv"),
        ("/no/tokens/*.csv", "/no/tokens/*.csv"),
    ],
)
def test_render_template(template: str, expected: str) -> None:
    assert render_template(template, RUN) == expected


def test_render_template_rejects_unknown_tokens() -> None:
    with pytest.raises(ValueError):
        render_template("/{yyyyQQ}", RUN)


def test_infer_format() -> None:
    assert [infer_format(p) for p in ("a.csv", "a.CSV.gz", "a.tsv", "b.jsonl", "c.json", "d.parquet")] == [
        "csv",
        "csv",
        "csv",
        "jsonl",
        "json",
        "parquet",
    ]
    with pytest.raises(ConnectorError):
        infer_format("x.bin")


@pytest.fixture
def landing(tmp_path: Path) -> Path:
    d = tmp_path / "landing" / "in"
    d.mkdir(parents=True)
    (d / "orders_20260305_a.csv").write_text("id,amount,email\n1,10.5,a@x.io\n2,,b@y.org\n")
    (d / "orders_20260305_b.csv.gz").write_bytes(gzip.compress(b"id,amount,email\n3,7,c@z.com\n"))
    (d / "orders_20260304.csv").write_text("id,amount,email\n9,1,old@x.io\n")
    (d / "events.jsonl").write_text('{"k": 1, "nested": {"a": 1}}\n{"k": 2, "nested": null}\n')
    (d / "api.json").write_text(json.dumps({"data": [{"id": 1, "tags": ["x"]}, {"id": 2, "tags": []}]}))
    pq.write_table(pa.table({"n": [1, 2, 3]}), d / "nums.parquet")
    return tmp_path / "landing"


def _read(conn: LocalFilesConnector, **req) -> tuple[pa.Table, ReadContext]:
    ctx = ReadContext(namespace=conn.namespace())
    batches = [b for b in conn.read(ReadRequest(run_date=RUN, **req), ctx)]
    if not batches:
        return pa.table({}), ctx
    return pa.concat_tables([pa.table(b.data) for b in batches], promote_options="permissive"), ctx


def test_reads_template_matched_csv_including_gzip(landing: Path) -> None:
    conn = LocalFilesConnector({"base_path": str(landing)})
    table, ctx = _read(conn, path_template="/in/orders_{yyyyMMdd}*.csv*")
    assert sorted(table.column("id").to_pylist()) == [1, 2, 3]
    assert [f.path for f in ctx.files] == ["/in/orders_20260305_a.csv", "/in/orders_20260305_b.csv.gz"]
    assert all(len(f.checksum) == 64 for f in ctx.files) and ctx.rows == 3


def test_new_files_only_skips_unchanged_files(landing: Path) -> None:
    conn = LocalFilesConnector({"base_path": str(landing)})
    _, first = _read(conn, path_template="/in/orders_*.csv", load_mode="incremental")
    seen = {f.path: f.fingerprint for f in first.files}
    _, again = _read(conn, path_template="/in/orders_*.csv", load_mode="incremental", seen_files=seen)
    assert again.files == []
    (landing / "in" / "orders_20260304.csv").write_text("id,amount,email\n9,2,changed@x.io\n10,3,n@x.io\n")
    table, changed = _read(conn, path_template="/in/orders_*.csv", load_mode="incremental", seen_files=seen)
    assert [f.path for f in changed.files] == ["/in/orders_20260304.csv"] and table.num_rows == 2


def test_json_formats_flatten_nested_values(landing: Path) -> None:
    conn = LocalFilesConnector({"base_path": str(landing)})
    jl, _ = _read(conn, path_template="/in/events.jsonl")
    assert jl.column("k").to_pylist() == [1, 2]
    js, _ = _read(conn, path_template="/in/api.json", format_options={"records_key": "data"})
    assert js.column("tags").to_pylist() == ['["x"]', "[]"]
    pqt, _ = _read(conn, path_template="/in/*.parquet")
    assert pqt.column("n").to_pylist() == [1, 2, 3]


def test_base_path_cannot_be_escaped(landing: Path) -> None:
    conn = LocalFilesConnector({"base_path": str(landing / "in")})
    with pytest.raises(ConnectorError, match="escapes"):
        _read(conn, path_template="/../../etc/passwd")


def test_discover_and_test(landing: Path) -> None:
    conn = LocalFilesConnector({"base_path": str(landing)})
    assert conn.test()["entries_in_base_path"] == 1
    names = [o.name for o in conn.discover("*.csv*")]
    assert "/in/orders_20260305_a.csv" in names and "/in/events.jsonl" not in names


def test_bad_file_is_a_connector_error(landing: Path) -> None:
    (landing / "in" / "broken.parquet").write_bytes(b"not parquet")
    with pytest.raises(ConnectorError, match="cannot parse"):
        _read(LocalFilesConnector({"base_path": str(landing)}), path_template="/in/broken.parquet")


def test_every_connector_declares_valid_secret_fields() -> None:
    for cls in connector_types().values():
        schema = config_schema(cls)
        assert schema["x-secret-fields"] == list(cls.secret_fields)
        assert cls.category in {"file", "database", "nosql", "api", "event"}


def test_secret_fields_must_be_vault_references() -> None:
    from dataplat.connectors.rdbms import PostgresConnector

    c = PostgresConnector({"host": "h", "database": "d", "username": "u", "password": "plain"})
    with pytest.raises(ConnectorError, match="vault://"):
        c.url()


def test_normalize_keeps_fields_missing_from_the_first_record() -> None:
    from dataplat.connectors.files import normalize_records

    table = pa.Table.from_pylist(normalize_records([{"a": 1}, {"a": 2, "b": {"x": 1}}, {"c": True}]))
    assert table.column_names == ["a", "b", "c"]
    assert table.column("b").to_pylist() == [None, '{"x": 1}', None]
