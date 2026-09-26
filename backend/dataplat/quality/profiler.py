"""Column profiling, run after every ingestion.

Computes per-column statistics with DuckDB over the Delta table's Arrow dataset (a
streaming scan, so the table never has to fit in memory), plus value-pattern
matches that Phase 3 uses to suggest PII classifications.
"""

from __future__ import annotations

from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.types as pat

# Pattern name -> DuckDB regular expression (full match).
PATTERNS = {
    "email": r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",
    "phone": r"^\+?[0-9][0-9 ()./-]{6,18}[0-9]$",
    "url": r"^https?://\S+$",
    "uuid": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    "ipv4": r"^((25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])$",
    "iso_date": r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?.*)?$",
    "card_number": r"^(?:\d[ -]?){13,19}$",
    "postal_code": r"^[0-9]{4,5}(-[0-9]{4})?$",
}

TOP_N = 5


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def profile(data: Any, skip: set[str] | None = None) -> dict[str, Any]:
    """Profiles a pyarrow Table or Dataset. Returns {"row_count", "columns": [...]}."""
    skip = skip or set()
    con = duckdb.connect()
    try:
        con.register("t", data)
        schema: pa.Schema = data.schema
        row_count = con.execute("select count(*) from t").fetchone()[0]
        columns = []
        for field in schema:
            if field.name in skip:
                continue
            columns.append(_profile_column(con, field, row_count))
        return {"row_count": int(row_count), "columns": columns}
    finally:
        con.close()


def _profile_column(con: duckdb.DuckDBPyConnection, field: pa.Field, row_count: int) -> dict[str, Any]:
    c = _q(field.name)
    t = field.type
    out: dict[str, Any] = {"name": field.name, "type": str(t)}
    nulls, distinct = con.execute(f"select count(*) - count({c}), approx_count_distinct({c}) from t").fetchone()
    out["nulls"] = int(nulls)
    out["null_pct"] = round(100 * nulls / row_count, 2) if row_count else 0.0
    out["distinct"] = int(distinct)
    out["distinct_pct"] = round(100 * distinct / max(row_count - nulls, 1), 2)

    if pat.is_integer(t) or pat.is_floating(t) or pat.is_decimal(t):
        mn, mx, mean, sd = con.execute(
            f"select min({c}), max({c}), avg({c}::double), stddev_samp({c}::double) from t"
        ).fetchone()
        out.update(min=_num(mn), max=_num(mx), mean=_num(mean), stddev=_num(sd))
    elif pat.is_temporal(t):
        mn, mx = con.execute(f"select min({c}), max({c}) from t").fetchone()
        out.update(min=str(mn) if mn is not None else None, max=str(mx) if mx is not None else None)
    elif pat.is_string(t) or pat.is_large_string(t):
        mn_len, mx_len, avg_len, blanks = con.execute(
            f"select min(length({c})), max(length({c})), avg(length({c})), "
            f"count(*) filter (where trim({c}) = '') from t"
        ).fetchone()
        out.update(min_length=mn_len, max_length=mx_len, avg_length=_num(avg_len), blanks=int(blanks))
        non_null = row_count - nulls
        if non_null:
            exprs = ", ".join(f"count(*) filter (where regexp_full_match({c}, '{rx}'))" for rx in PATTERNS.values())
            counts = con.execute(f"select {exprs} from t").fetchone()
            out["patterns"] = {
                name: round(100 * n / non_null, 2) for name, n in zip(PATTERNS, counts, strict=True) if n
            }
    elif pat.is_boolean(t):
        true_count = con.execute(f"select count(*) filter (where {c}) from t").fetchone()[0]
        out["true_pct"] = round(100 * true_count / max(row_count - nulls, 1), 2)

    if not (pat.is_nested(t) or pat.is_binary(t)):
        top = con.execute(
            f"select {c}::varchar as v, count(*) as n from t where {c} is not null "
            f"group by 1 order by 2 desc, 1 limit {TOP_N}"
        ).fetchall()
        out["top_values"] = [
            {"value": v if v is None or len(v) <= 100 else v[:100] + "…", "count": int(n)} for v, n in top
        ]
    return out


def _num(v: Any) -> float | int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return None
