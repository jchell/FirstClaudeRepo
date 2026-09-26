"""Column-level lineage for SQL models, parsed with sqlglot.

For each output column, the leaf columns of the source relations it is computed
from. Columns that come from no source column (literals, count(*)) map to nothing.
Parsing is best effort: a query sqlglot can't qualify yields table-level lineage only.
"""

from __future__ import annotations

import logging

import pyarrow as pa
from sqlglot import exp, parse_one
from sqlglot.lineage import lineage
from sqlglot.optimizer import optimize

from dataplat.transform.output import ColumnLineage

log = logging.getLogger(__name__)


def _duck_type(t: pa.DataType) -> str:
    if pa.types.is_integer(t):
        return "bigint"
    if pa.types.is_floating(t):
        return "double"
    if pa.types.is_boolean(t):
        return "boolean"
    if pa.types.is_timestamp(t):
        return "timestamptz" if t.tz else "timestamp"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_decimal(t):
        return f"decimal({t.precision},{t.scale})"
    return "varchar"


def column_lineage(
    sql: str,
    schemas: dict[str, pa.Schema],
    relations: dict[str, tuple[str, str]],
    output_columns: list[str],
) -> ColumnLineage:
    """``schemas`` and ``relations`` are keyed by relation name (e.g. ``vault__hub_customer``)."""
    schema = {rel: {f.name: _duck_type(f.type) for f in s} for rel, s in schemas.items()}
    try:
        query = optimize(parse_one(sql, dialect="duckdb"), schema=schema, dialect="duckdb")
    except Exception as e:
        log.info("column lineage unavailable: %s", e)
        return {}
    result: ColumnLineage = {}
    for col in output_columns:
        try:
            node = lineage(col, query, schema=schema, dialect="duckdb")
        except Exception:
            continue
        leaves: list[tuple[str, str, str]] = []
        for n in node.walk():
            if n.downstream or not isinstance(n.expression, exp.Table):
                continue
            rel = n.expression.name
            if rel not in relations:
                continue
            column = n.name.rsplit(".", 1)[-1].strip('"')
            layer, dataset = relations[rel]
            if (layer, dataset, column) not in leaves:
                leaves.append((layer, dataset, column))
        result[col] = leaves
    return result
