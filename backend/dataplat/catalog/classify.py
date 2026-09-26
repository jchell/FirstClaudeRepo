"""Classification suggestions (auto-PII) for catalog columns.

Three signals, each producing a *suggested* tag that a steward accepts or rejects:

  name     the column name looks like a PII field (email, phone, name, birth date...)
  pattern  the latest profile shows most values match a PII pattern (email, phone, card)
  lineage  the column is computed from a column that already carries a classification

Suggestions never override a steward's decision: an existing assignment (active or
rejected) for the same tag and column is left alone.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dataplat.catalog.service import AUDIT_COLUMNS
from dataplat.db.models import Dataset, DatasetColumn, DatasetProfile, Tag, TagAssignment

CLASSIFICATIONS = {
    "pii": "Personal data (any kind)",
    "pii.email": "Email address",
    "pii.phone": "Phone number",
    "pii.name": "Person name",
    "pii.address": "Postal address",
    "pii.birth_date": "Date of birth",
    "pii.national_id": "National identifier (SSN, passport, tax id)",
    "pii.card_number": "Payment card number (PCI)",
    "pii.ip_address": "IP address",
}

NAME_RULES: list[tuple[str, re.Pattern[str], float]] = [
    ("pii.email", re.compile(r"(^|_)e?_?mail(_?addr(ess)?)?($|_)"), 0.8),
    ("pii.phone", re.compile(r"(^|_)(phone|mobile|cell|tel|telephone|fax)(_?(no|number|nr))?($|_)"), 0.8),
    ("pii.name", re.compile(r"(^|_)(first|last|given|family|middle|full|sur)_?name($|_)|^surname$|^name$"), 0.7),
    (
        "pii.address",
        re.compile(r"(^|_)(street|(?<!mail_)address|addr|address_line_?\d?|postal_?code|zip_?code)($|_)"),
        0.6,
    ),
    ("pii.birth_date", re.compile(r"(^|_)(birth_?date|date_?of_?birth|dob|birthday)($|_)"), 0.8),
    ("pii.national_id", re.compile(r"(^|_)(ssn|national_?id|passport(_?no)?|tax_?id|nin|personnummer)($|_)"), 0.8),
    ("pii.card_number", re.compile(r"(^|_)(card_?(no|number|num)|cc_?number|pan)($|_)"), 0.8),
    ("pii.ip_address", re.compile(r"(^|_)(ip|ip_?addr(ess)?|client_?ip)($|_)"), 0.7),
]
PATTERN_RULES = {"email": "pii.email", "phone": "pii.phone", "card_number": "pii.card_number"}
PATTERN_MIN_PCT = 80.0
SKIP = set(AUDIT_COLUMNS) | {"_op", "_source_ts", "_offset", "record_source", "load_date", "hashdiff"}


def ensure_classifications(s: Session) -> None:
    existing = {t.name for t in s.scalars(select(Tag).where(Tag.name.in_(list(CLASSIFICATIONS))))}
    for name, desc in CLASSIFICATIONS.items():
        if name not in existing:
            s.add(Tag(name=name, category="classification", description=desc, created_by="system"))
    s.flush()


def name_signals(column: str) -> list[tuple[str, float, str]]:
    n = column.lower()
    return [
        (tag, conf, f"column name '{column}' looks like {CLASSIFICATIONS[tag].lower()}")
        for tag, rx, conf in NAME_RULES
        if rx.search(n)
    ]


def pattern_signals(profile_column: dict[str, Any]) -> list[tuple[str, float, str]]:
    out = []
    for pattern, pct in (profile_column.get("patterns") or {}).items():
        tag = PATTERN_RULES.get(pattern)
        if tag and pct >= PATTERN_MIN_PCT:
            out.append(
                (tag, round(min(pct / 100, 0.99), 2), f"{pct:.0f}% of values look like {pattern.replace('_', ' ')}s")
            )
    return out


def _latest_profile(s: Session, dataset_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    prof = s.scalars(
        select(DatasetProfile).where(DatasetProfile.dataset_id == dataset_id).order_by(DatasetProfile.ts.desc())
    ).first()
    return {c["name"]: c for c in (prof.columns if prof else [])}


def suggest(
    s: Session,
    dataset_ids: list[uuid.UUID] | None = None,
    upstream: dict[tuple[str, str, str], list[tuple[str, str, str]]] | None = None,
) -> list[dict[str, Any]]:
    """Adds suggested classifications; returns the new ones.

    ``upstream`` maps (layer, dataset, column) to the columns it is computed from
    (from column lineage); an upstream classification is suggested downstream.
    """
    ensure_classifications(s)
    q = select(Dataset)
    if dataset_ids is not None:
        q = q.where(Dataset.id.in_(dataset_ids))
    datasets = list(s.scalars(q))
    existing = {(a.tag, a.dataset_id, a.column) for a in s.scalars(select(TagAssignment))}
    active: dict[tuple[str, str, str], set[str]] = {}
    if upstream:
        by_id = {d.id: d for d in s.scalars(select(Dataset))}
        for a in s.scalars(select(TagAssignment).where(TagAssignment.status == "active", TagAssignment.column != "")):
            d = by_id.get(a.dataset_id)
            if d is not None and a.tag in CLASSIFICATIONS:
                active.setdefault((d.layer, d.name, a.column), set()).add(a.tag)
    added = []
    for ds in datasets:
        profile = _latest_profile(s, ds.id)
        cols = s.scalars(
            select(DatasetColumn).where(DatasetColumn.dataset_id == ds.id, DatasetColumn.removed_at.is_(None))
        )
        for col in cols:
            if col.name in SKIP or col.name.startswith("hk_"):
                continue
            signals: dict[str, tuple[float, str, str]] = {}
            for tag, conf, why in name_signals(col.name):
                signals[tag] = max(signals.get(tag, (0, "", "")), (conf, why, "name"))
            for tag, conf, why in pattern_signals(profile.get(col.name, {})):
                signals[tag] = max(signals.get(tag, (0, "", "")), (conf, why, "pattern"))
            for src in (upstream or {}).get((ds.layer, ds.name, col.name), []):
                for tag in active.get(src, ()):
                    why = f"computed from {src[0]}.{src[1]}.{src[2]}, which is classified {tag}"
                    signals[tag] = max(signals.get(tag, (0, "", "")), (0.9, why, "lineage"))
            for tag, (conf, why, source) in signals.items():
                if (tag, ds.id, col.name) in existing:
                    continue
                s.add(
                    TagAssignment(
                        tag=tag,
                        dataset_id=ds.id,
                        column=col.name,
                        status="suggested",
                        source=source,
                        confidence=conf,
                        reason=why,
                        assigned_by="system",
                    )
                )
                existing.add((tag, ds.id, col.name))
                added.append({"dataset": f"{ds.layer}.{ds.name}", "column": col.name, "tag": tag, "source": source})
    s.flush()
    return added
