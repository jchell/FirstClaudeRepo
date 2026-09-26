"""Phase 3: DQ rules and scorecards, classification suggestions, access/masking/row-filter policies."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest
from sqlalchemy import select

from dataplat.catalog import service as catalog
from dataplat.catalog.classify import name_signals, suggest
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.db.models import (
    AccessGrant,
    Alert,
    Dataset,
    DatasetProfile,
    DqResult,
    DqRule,
    DqScorecard,
    DqScorecardResult,
    MaskingPolicy,
    RowFilter,
    Tag,
    TagAssignment,
)
from dataplat.quality.profiler import profile
from dataplat.quality.rules import RuleParams, RuleRunner, RuleSpec
from dataplat.security.policy import (
    AccessDenied,
    PolicyEngine,
    mask_profile,
    read_secured,
    secured_relations,
    validate_predicate,
)

NOW = datetime.now(UTC)


def _dataset(ctx: PlatformContext, layer: str, name: str, table: pa.Table, domain: str | None = None) -> Dataset:
    uri = ctx.config.lake.uri(layer, name)
    ctx.tables.write(table, uri, "overwrite")
    with ctx.metadata.session() as s:
        ds, _ = catalog.register(s, layer=layer, name=name, uri=uri, schema=table.schema, stats=ctx.tables.stats(uri))
        ds.domain = domain
        s.add(DatasetProfile(dataset_id=ds.id, row_count=table.num_rows, columns=profile(table)["columns"]))
        s.flush()
        s.expunge(ds)
        return ds


@pytest.fixture
def customers(ctx: PlatformContext) -> Dataset:
    t = pa.table(
        {
            "id": [1, 2, 3, 4, 4],
            "email": ["ann@x.io", "bob@y.io", None, "dee@z.io", "dee@z.io"],
            "phone_number": ["+47 22 33 44 55", "+1 555 0100", "+44 20 7946 0000", "n/a", "n/a"],
            "country": ["NO", "US", "UK", "XX", "XX"],
            "age": [34, 51, 17, 230, 230],
            "_load_ts": pa.array([NOW - timedelta(hours=2)] * 5, pa.timestamp("us", tz="UTC")),
        }
    )
    return _dataset(ctx, "silver", "customers", t, domain="sales")


def _rule(
    ctx, ds: Dataset, name: str, rule_type: str, column: str | None = None, severity="warning", threshold=1.0, **params
):
    RuleSpec(rule_type=rule_type, column=column, params=RuleParams(**params))  # validates
    with ctx.metadata.session() as s:
        r = DqRule(
            name=name,
            dataset_id=ds.id,
            column=column,
            rule_type=rule_type,
            params=params,
            dimension={"not_null": "completeness", "unique": "uniqueness", "freshness": "timeliness"}.get(
                rule_type, "validity"
            ),
            severity=severity,
            threshold=threshold,
        )
        s.add(r)
        s.flush()
        return r.id


# ------------------------------------------------------------------ rules


def test_every_rule_type(ctx: PlatformContext, customers: Dataset) -> None:
    orders = _dataset(ctx, "silver", "orders", pa.table({"order_id": [1, 2, 3], "customer_id": [1, 2, 99]}))
    ids = {
        "nn": _rule(ctx, customers, "email_not_null", "not_null", "email"),
        "uq": _rule(ctx, customers, "id_unique", "unique", "id"),
        "rg": _rule(ctx, customers, "age_range", "range", "age", min=0, max=120),
        "rx": _rule(ctx, customers, "phone_format", "regex", "phone_number", pattern=r"^\+[0-9 ]+$"),
        "av": _rule(ctx, customers, "country_code", "allowed_values", "country", values=["NO", "US", "UK"]),
        "rf": _rule(
            ctx,
            orders,
            "orders_customer",
            "referential",
            "customer_id",
            ref_dataset="silver.customers",
            ref_column="id",
        ),
        "fr": _rule(ctx, customers, "fresh", "freshness", max_age_minutes=60),
        "rc": _rule(ctx, customers, "enough_rows", "row_count", min=3),
        "cs": _rule(ctx, customers, "adults", "custom_sql", sql="select * from data where age < 18"),
        "ok": _rule(ctx, customers, "mostly_emails", "not_null", "email", threshold=0.75),
    }
    out = RuleRunner(ctx).run(list(ids.values()))
    by = {r["rule"]: r for r in out["rules"]}
    assert (by["email_not_null"]["rows_checked"], by["email_not_null"]["rows_failed"]) == (5, 1)
    assert by["id_unique"]["rows_failed"] == 2  # both rows of the duplicated key
    assert by["age_range"]["rows_failed"] == 2
    assert by["phone_format"]["rows_failed"] == 2
    assert by["country_code"]["rows_failed"] == 2
    assert by["orders_customer"]["rows_failed"] == 1 and by["orders_customer"]["score"] == pytest.approx(66.67)
    assert by["fresh"]["passed"] is False  # loaded 2 h ago, max age 1 h
    assert by["enough_rows"]["passed"] is True
    assert by["adults"]["rows_failed"] == 1
    assert by["mostly_emails"]["passed"] is True and by["email_not_null"]["passed"] is False  # threshold
    with ctx.metadata.session() as s:
        sample = s.scalars(select(DqResult).where(DqResult.rule_id == ids["av"])).one().sample
    assert sample == ["XX", "XX"]


@pytest.mark.parametrize(
    ("rule_type", "column", "params", "error"),
    [
        ("not_null", None, {}, "check a column"),
        ("range", "x", {}, "min and/or max"),
        ("regex", "x", {"pattern": "("}, "missing|unterminated|pattern"),
        ("custom_sql", None, {"sql": "delete from data"}, "SELECT"),
        ("referential", "x", {"ref_dataset": "silver.x"}, "referenced"),
    ],
)
def test_rule_validation(rule_type: str, column: str | None, params: dict, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        RuleSpec(rule_type=rule_type, column=column, params=RuleParams(**params))


def test_rule_errors_are_recorded_not_raised(ctx: PlatformContext, customers: Dataset) -> None:
    rid = _rule(ctx, customers, "bad_col", "not_null", "missing_column", severity="critical")
    out = RuleRunner(ctx).run([rid])
    assert out["rules"][0]["passed"] is False and "missing_column" in out["rules"][0]["error"]
    with ctx.metadata.session() as s:
        alert = s.scalars(select(Alert).where(Alert.kind == "dq_failed")).one()
    assert alert.target == "rule:bad_col" and alert.resolved_at is None


def test_scorecard_scores_and_degradation_alert(ctx: PlatformContext, customers: Dataset) -> None:
    good = _rule(ctx, customers, "ids_present", "not_null", "id", severity="critical")
    emails = _rule(ctx, customers, "emails_present", "not_null", "email")
    with ctx.metadata.session() as s:
        card = DqScorecard(name="customers", rule_ids=[str(good), str(emails)], degradation_pct=10, baseline_runs=3)
        s.add(card)
    runner = RuleRunner(ctx)
    first = runner.run([good, emails])["scorecards"][0]
    # (100*2 + 80*1) / 3
    assert first["score"] == pytest.approx(93.33) and first["dimensions"] == {"completeness": pytest.approx(93.33)}
    assert first["baseline"] is None and not first["degraded"]
    runner.run([good, emails])

    # Quality drops: most emails vanish
    t = ctx.tables.read(customers.uri)
    t = t.set_column(t.column_names.index("email"), "email", pa.array([None, None, None, None, "x@y.z"]))
    ctx.tables.write(t, customers.uri, "overwrite")
    third = runner.run([good, emails])["scorecards"][0]
    assert third["score"] == pytest.approx(73.33) and third["baseline"] == pytest.approx(93.33) and third["degraded"]
    with ctx.metadata.session() as s:
        assert s.scalars(select(Alert).where(Alert.kind == "dq_degradation")).one().resolved_at is None
        assert s.scalars(select(DqScorecardResult)).all()[-1].degraded


# ------------------------------------------------------------------ classification


def test_name_signals() -> None:
    tags = lambda c: {t for t, _, _ in name_signals(c)}  # noqa: E731
    assert tags("email") == {"pii.email"} and tags("customer_email_address") == {"pii.email"}
    assert tags("phone_number") == {"pii.phone"} and tags("first_name") == {"pii.name"}
    assert tags("date_of_birth") == {"pii.birth_date"} and tags("emailed_count") == set()
    assert tags("country") == set() and tags("zipcode") == {"pii.address"}


def test_suggestions_from_names_patterns_and_lineage(ctx: PlatformContext, customers: Dataset) -> None:
    contacts = _dataset(ctx, "silver", "contacts", pa.table({"cid": [1, 2], "contact": ["a@b.io", "c@d.io"]}))
    report = _dataset(ctx, "gold", "report", pa.table({"customer_contact": ["x"]}))
    with ctx.metadata.session() as s:
        added = suggest(s)
    found = {(a["dataset"], a["column"], a["tag"], a["source"]) for a in added}
    assert ("silver.customers", "email", "pii.email", "pattern") in found  # 75% emails < 80%? no: 4/4 non-null
    assert ("silver.customers", "phone_number", "pii.phone", "name") in found
    assert ("silver.contacts", "contact", "pii.email", "pattern") in found  # only the values give it away
    assert not any(a["column"] in ("id", "country", "_load_ts") for a in added)

    # A steward accepts one; the classification then flows to derived columns
    with ctx.metadata.session() as s:
        a = s.scalars(
            select(TagAssignment).where(TagAssignment.dataset_id == contacts.id, TagAssignment.column == "contact")
        ).one()
        a.status = "active"
    with ctx.metadata.session() as s:
        again = suggest(s, upstream={("gold", "report", "customer_contact"): [("silver", "contacts", "contact")]})
    assert [(a["dataset"], a["column"], a["source"]) for a in again] == [("gold.report", "customer_contact", "lineage")]
    with ctx.metadata.session() as s:
        assert suggest(s) == []  # idempotent
    assert report  # created


# ------------------------------------------------------------------ policies


def _engine(s, *roles: str, name: str = "u1") -> PolicyEngine:
    return PolicyEngine(s, Principal(id=str(uuid.uuid4()), name=name, kind="user", roles=frozenset(roles)))


def test_access_grants_on_datasets_and_domains(ctx: PlatformContext, customers: Dataset) -> None:
    other = _dataset(ctx, "gold", "kpis", pa.table({"k": [1]}))
    with ctx.metadata.session() as s:
        assert _engine(s, "analyst").can_read(customers)  # no grants: open
        s.add(AccessGrant(target_type="domain", target="sales", principal_type="role", principal="sales_analyst"))
        s.add(AccessGrant(target_type="dataset", target=str(customers.id), principal_type="user", principal="bea"))
    with ctx.metadata.session() as s:
        assert not _engine(s, "analyst").can_read(customers)
        assert _engine(s, "sales_analyst").can_read(customers)
        assert _engine(s, "analyst", name="bea").can_read(customers)
        assert _engine(s, "steward").can_read(customers)
        e = _engine(s, "analyst")
        assert e.can_read(other) and e.readable_ids() == {other.id}
        with pytest.raises(AccessDenied):
            read_secured(ctx, e, customers, limit=5)


def test_masking_and_row_filters(ctx: PlatformContext, customers: Dataset) -> None:
    with ctx.metadata.session() as s:
        s.add(Tag(name="pii", category="classification"))
        s.add(Tag(name="pii.email", category="classification"))
        s.add(Tag(name="pii.phone", category="classification"))
        s.flush()
        s.add(TagAssignment(tag="pii.email", dataset_id=customers.id, column="email"))
        s.add(TagAssignment(tag="pii.phone", dataset_id=customers.id, column="phone_number"))
        s.add(TagAssignment(tag="pii.phone", dataset_id=customers.id, column="country", status="suggested"))
        s.add(MaskingPolicy(name="emails", tag="pii.email", method="hash", exempt_roles=["steward"]))
        s.add(MaskingPolicy(name="phones", tag="pii", method="partial", exempt_roles=["steward"]))  # prefix: pii.*
        s.add(
            RowFilter(
                name="no_xx",
                dataset_id=customers.id,
                predicate=validate_predicate("country <> 'XX'"),
                exempt_roles=["steward"],
            )
        )
    with ctx.metadata.session() as s:
        analyst = _engine(s, "analyst")
        rows = read_secured(ctx, analyst, customers).to_pylist()
        assert [r["id"] for r in rows] == [1, 2, 3]  # filtered
        assert rows[0]["email"] != "ann@x.io" and len(rows[0]["email"]) == 16 and rows[2]["email"] is None
        assert rows[0]["phone_number"] == "*" * 11 + "4 55" and rows[0]["country"] == "NO"  # suggested tag: no effect
        assert read_secured(ctx, analyst, customers).to_pylist()[0]["email"] == rows[0]["email"]  # stable hash
        steward = read_secured(ctx, _engine(s, "steward"), customers).to_pylist()
        assert len(steward) == 5 and steward[0]["email"] == "ann@x.io"

        pol = analyst.for_dataset(s.get(Dataset, customers.id))
        prof = s.scalars(select(DatasetProfile).where(DatasetProfile.dataset_id == customers.id)).one()
        masked = {c["name"]: c for c in mask_profile(prof.columns, pol)}
        assert all(t["value"] == "****" for t in masked["email"]["top_values"])
        assert masked["id"]["top_values"][0]["value"] in ("4", "1")

        # a user's query only ever sees the secured copy
        rels = secured_relations(ctx, analyst, {"silver__customers": ("silver", "customers")})
        assert rels["silver__customers"].num_rows == 3


@pytest.mark.parametrize("bad", ["1=1; drop table x", "id in (select id from other)", "id >"])
def test_row_filter_predicates_are_validated(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_predicate(bad)
