"""Data Vault 2.0: hashing, insert-only loads, status satellites, PIT and bridge builds."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import duckdb
import pyarrow as pa
import pytest

from dataplat.core.context import PlatformContext
from dataplat.db.models import TransformRun, VaultMapping, VaultObject
from dataplat.ingestion.runner import with_audit_columns
from dataplat.vault import sql as vsql
from dataplat.vault.loader import VaultError, VaultLoader
from dataplat.vault.model import validate_object

T0 = datetime(2026, 1, 1, tzinfo=UTC)


# ------------------------------------------------------------------ pure functions


def test_sql_hash_matches_python_reference() -> None:
    con = duckdb.connect()
    con.execute("create table t as select * from (values (' ab ', 1, null), ('x', null, 'Y')) v(a, b, c)")
    rows = con.execute(
        f"select {vsql.hash_expr(['a', 'b'])}, {vsql.hashdiff_expr(['a', 'b', 'c'])}, a, b, c from t"
    ).fetchall()
    for hk, hd, a, b, c in rows:
        assert hk == vsql.hash_key(a, b)
        assert hd == vsql.hashdiff(a, b, c)
    # business keys ignore case and padding; attributes keep case
    assert vsql.hash_key(" Ann ") == vsql.hash_key("ANN")
    assert vsql.hashdiff("Ann") != vsql.hashdiff("ANN")
    # adding a trailing, empty attribute does not change the hashdiff
    assert vsql.hashdiff("a", "b") == vsql.hashdiff("a", "b", None)


@pytest.mark.parametrize(
    ("kind", "name", "definition", "error"),
    [
        ("hub", "customer", {"business_keys": ["id"]}, "hub_<name>"),
        ("hub", "hub_customer", {"business_keys": []}, "at least 1"),
        ("link", "link_x", {"hubs": [{"hub": "hub_a"}, {"hub": "hub_a"}]}, "distinct role"),
        ("sat", "sat_x", {"parent": "hub_a", "attributes": ["load_date"]}, "reserved"),
        ("sat", "sat_x", {"parent": "hub_a", "attributes": ["a"], "multi_active_key": ["b"]}, "must also be"),
    ],
)
def test_definitions_are_validated(kind: str, name: str, definition: dict, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        validate_object(kind, name, definition)


def test_status_satellite_definition() -> None:
    d = validate_object("sat", "sat_customer_status", {"parent": "hub_customer", "status": True})
    assert d["attributes"] == ["is_deleted"]


# ------------------------------------------------------------------ loads


def _bronze(ctx: PlatformContext, name: str, rows: list[dict], load_ts: datetime, mode: str = "append") -> str:
    batch = uuid.uuid4().hex
    table = with_audit_columns(pa.Table.from_pylist(rows), load_ts, f"test/{name}", batch, None)
    ctx.tables.write(table, ctx.config.lake.uri("bronze", name), mode)
    return batch


def _obj(ctx: PlatformContext, kind: str, name: str, definition: dict) -> uuid.UUID:
    with ctx.metadata.session() as s:
        o = VaultObject(kind=kind, name=name, definition=validate_object(kind, name, definition))
        s.add(o)
        s.flush()
        return o.id


def _map(ctx: PlatformContext, source: str, target: uuid.UUID, keys: dict, attributes: dict | None = None) -> None:
    with ctx.metadata.session() as s:
        s.add(VaultMapping(source_dataset=source, target_id=target, keys=keys, attributes=attributes or {}))


def _vault(ctx: PlatformContext, name: str) -> list[dict]:
    return ctx.tables.read(ctx.config.lake.uri("vault", name)).to_pylist()


@pytest.fixture
def customer_vault(ctx: PlatformContext) -> PlatformContext:
    hub = _obj(ctx, "hub", "hub_customer", {"business_keys": ["customer_id"]})
    sat = _obj(ctx, "sat", "sat_customer_details", {"parent": "hub_customer", "attributes": ["name", "city"]})
    status = _obj(ctx, "sat", "sat_customer_status", {"parent": "hub_customer", "status": True})
    _map(ctx, "customers", hub, {"customer_id": "id"})
    _map(ctx, "customers", sat, {"customer_id": "id"}, {"name": "name", "city": "city"})
    _map(ctx, "customers", status, {"customer_id": "id"})
    _obj(ctx, "pit", "pit_customer", {"hub": "hub_customer", "satellites": ["sat_customer_details"]})
    return ctx


def test_hub_and_satellite_loads_are_insert_only_and_idempotent(customer_vault: PlatformContext) -> None:
    ctx = customer_vault
    loader = VaultLoader(ctx)
    batch1 = _bronze(
        ctx, "customers", [{"id": 1, "name": "Ann", "city": "Oslo"}, {"id": 2, "name": "Bob", "city": "Rome"}], T0
    )
    res = loader.load_source("bronze", "customers")
    assert {t["target"]: t["rows"] for t in res["targets"]} == {
        "hub_customer": 2,
        "sat_customer_details": 2,
        "sat_customer_status": 2,
    }
    hub = {r["customer_id"]: r for r in _vault(ctx, "hub_customer")}
    assert hub["1"]["hk_customer"] == vsql.hash_key(1)
    assert hub["1"]["_batch_id"] == batch1 and hub["1"]["record_source"] == "test/customers"

    # Same data again (e.g. a full reload): nothing new anywhere.
    _bronze(
        ctx,
        "customers",
        [{"id": 1, "name": "Ann", "city": "Oslo"}, {"id": 2, "name": "Bob", "city": "Rome"}],
        T0 + timedelta(hours=1),
        "overwrite",
    )
    res = loader.load_source("bronze", "customers")
    assert all(t["rows"] == 0 for t in res["targets"]) and res["targets"][0]["staged"] == 2

    # Bob moves: one new satellite row, hub untouched; a new customer adds a hub row.
    _bronze(
        ctx,
        "customers",
        [{"id": 2, "name": "Bob", "city": "Paris"}, {"id": 3, "name": "Cy", "city": None}],
        T0 + timedelta(hours=2),
        "overwrite",
    )
    loader.load_source("bronze", "customers")
    assert len(_vault(ctx, "hub_customer")) == 3
    sat = [r for r in _vault(ctx, "sat_customer_details") if r["hk_customer"] == vsql.hash_key(2)]
    assert sorted(r["city"] for r in sat) == ["Paris", "Rome"]
    assert len({r["hashdiff"] for r in sat}) == 2

    # PIT: one row per change date of customer 2, pointing at the satellite row valid then
    pit = [r for r in _vault(ctx, "pit_customer") if r["hk_customer"] == vsql.hash_key(2)]
    assert len(pit) == 2 and all(r["sat_customer_details_ldts"] == r["snapshot_date"] for r in pit)
    with ctx.metadata.session() as s:
        kinds = {r.kind for r in s.query(TransformRun).all()}
    assert kinds == {"vault_load", "vault_build"}


def test_changes_within_one_batch_and_cdc_deletes(customer_vault: PlatformContext) -> None:
    ctx = customer_vault
    rows = [
        {"id": 1, "name": "Ann", "city": "Oslo", "_op": "c", "_source_ts": T0},
        {"id": 1, "name": "Ann", "city": "Oslo", "_op": "u", "_source_ts": T0 + timedelta(seconds=1)},  # no change
        {"id": 1, "name": "Ann", "city": "Bergen", "_op": "u", "_source_ts": T0 + timedelta(seconds=2)},
        {"id": 1, "name": None, "city": None, "_op": "d", "_source_ts": T0 + timedelta(seconds=3)},
    ]
    _bronze(ctx, "customers", rows, T0 + timedelta(minutes=1))
    VaultLoader(ctx).load_source("bronze", "customers")
    sat = sorted(_vault(ctx, "sat_customer_details"), key=lambda r: r["load_date"])
    assert [r["city"] for r in sat] == ["Oslo", "Bergen"]  # the delete isn't an attribute change
    assert sat[0]["load_date"] == T0  # the source commit time, not the load time
    status = sorted(_vault(ctx, "sat_customer_status"), key=lambda r: r["load_date"])
    assert [r["is_deleted"] for r in status] == [False, True]


def test_link_and_bridge(ctx: PlatformContext) -> None:
    hub_c = _obj(ctx, "hub", "hub_customer", {"business_keys": ["customer_id"]})
    hub_o = _obj(ctx, "hub", "hub_order", {"business_keys": ["order_id"]})
    link = _obj(ctx, "link", "link_customer_order", {"hubs": [{"hub": "hub_customer"}, {"hub": "hub_order"}]})
    _obj(ctx, "bridge", "bridge_customer_orders", {"hub": "hub_customer", "links": ["link_customer_order"]})
    _map(ctx, "orders", hub_c, {"customer_id": "cust"})
    _map(ctx, "orders", hub_o, {"order_id": "oid"})
    _map(ctx, "orders", link, {"customer.customer_id": "cust", "order.order_id": "oid"})
    _bronze(ctx, "orders", [{"oid": 10, "cust": 1}, {"oid": 11, "cust": 1}, {"oid": 12, "cust": None}], T0)
    res = VaultLoader(ctx).load_source("bronze", "orders")
    assert {t["target"]: t["rows"] for t in res["targets"]} == {
        "hub_customer": 1,
        "hub_order": 3,
        "link_customer_order": 2,
    }
    links = _vault(ctx, "link_customer_order")
    assert {r["hk_order"] for r in links} == {vsql.hash_key(10), vsql.hash_key(11)}
    assert links[0]["hk_customer_order"] == vsql.hash_key(1, links[0]["hk_order"] == vsql.hash_key(10) and 10 or 11)
    assert res["rebuilt"] == ["bridge_customer_orders"]
    bridge = _vault(ctx, "bridge_customer_orders")
    assert len(bridge) == 2 and {r["hk_customer"] for r in bridge} == {vsql.hash_key(1)}


def test_mapping_to_missing_column_fails_the_run(customer_vault: PlatformContext) -> None:
    ctx = customer_vault
    _bronze(ctx, "customers", [{"id": 1, "full_name": "Ann", "city": "Oslo"}], T0)
    with pytest.raises(KeyError, match="name"):
        VaultLoader(ctx).load_source("bronze", "customers")
    with ctx.metadata.session() as s:
        run = s.query(TransformRun).one()
    assert run.status == "failed" and "name" in run.error


def test_pit_rejects_foreign_satellite(ctx: PlatformContext) -> None:
    _obj(ctx, "hub", "hub_a", {"business_keys": ["k"]})
    _obj(ctx, "hub", "hub_b", {"business_keys": ["k"]})
    _obj(ctx, "sat", "sat_b", {"parent": "hub_b", "attributes": ["v"]})
    _obj(ctx, "pit", "pit_a", {"hub": "hub_a", "satellites": ["sat_b"]})
    for name, keyed in (("hub_a", False), ("sat_b", True)):
        t = pa.table({"hk_a" if not keyed else "hk_b": ["x"], "load_date": [T0]})
        ctx.tables.write(t, ctx.config.lake.uri("vault", name))
    with pytest.raises(VaultError, match="does not belong"):
        VaultLoader(ctx).build("pit_a")
