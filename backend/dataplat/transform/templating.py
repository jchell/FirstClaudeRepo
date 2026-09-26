"""SQL model templating (a small, safe subset of dbt's Jinja).

  {{ ref('model') }}              another model (silver or gold)
  {{ source('bronze', 'orders') }}  any lake dataset: bronze, silver, gold or vault
  {{ vault('sat_customer') }}      a vault table (same as source('vault', ...))
  {{ this }}                      the model's own table (incremental models)
  {% if is_incremental() %} ... {% else %} ... {% endif %}

References become relation names (``<layer>__<name>``) that the sandbox registers.
Nothing else is evaluated: this is text substitution, not a template language, so a
model can't run code on the worker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

LAYERS = ("bronze", "silver", "gold", "vault")
_NAME = r"[a-z][a-z0-9_]{0,127}"
_REF = re.compile(
    r"\{\{\s*(?:(ref)\(\s*'(" + _NAME + r")'\s*\)"
    r"|(source)\(\s*'([a-z]+)'\s*,\s*'(" + _NAME + r")'\s*\)"
    r"|(vault)\(\s*'(" + _NAME + r")'\s*\)"
    r"|(this))\s*\}\}"
)
_IF = re.compile(
    r"\{%-?\s*if\s+is_incremental\(\)\s*-?%\}(.*?)(?:\{%-?\s*else\s*-?%\}(.*?))?\{%-?\s*endif\s*-?%\}", re.S
)
_LEFTOVER = re.compile(r"\{\{|\{%|%\}|\}\}")


class TemplateError(ValueError):
    pass


@dataclass
class Rendered:
    sql: str
    # relation name -> (layer, dataset); models are resolved to their layer
    relations: dict[str, tuple[str, str]] = field(default_factory=dict)
    refs: list[str] = field(default_factory=list)  # model names referenced with ref()
    uses_this: bool = False


def relation_name(layer: str, name: str) -> str:
    return f"{layer}__{name}"


def render(
    sql: str, model_layers: dict[str, str], *, incremental: bool = False, this: tuple[str, str] | None = None
) -> Rendered:
    """``model_layers`` maps every known model name to its layer (for ref())."""
    out = Rendered(sql="")

    def branch(m: re.Match[str]) -> str:
        return m.group(1) if incremental else (m.group(2) or "")

    text = _IF.sub(branch, sql)

    def sub(m: re.Match[str]) -> str:
        if m.group(1):
            model = m.group(2)
            if model not in model_layers:
                raise TemplateError(f"ref('{model}'): no such model")
            out.refs.append(model)
            layer, name = model_layers[model], model
        elif m.group(3):
            layer, name = m.group(4), m.group(5)
            if layer not in LAYERS:
                raise TemplateError(f"source('{layer}', ...): layer must be one of {', '.join(LAYERS)}")
        elif m.group(6):
            layer, name = "vault", m.group(7)
        else:
            if this is None:
                raise TemplateError("{{ this }} is only available in incremental models")
            out.uses_this = True
            return "__this"
        rel = relation_name(layer, name)
        out.relations[rel] = (layer, name)
        return rel

    text = _REF.sub(sub, text)
    if _LEFTOVER.search(text):
        raise TemplateError("unsupported template syntax: use ref(), source(), vault(), this and is_incremental()")
    out.sql = text
    return out


def check_select(sql: str) -> exp.Expression:
    """Accepts exactly one read-only query (SELECT/WITH/UNION...)."""
    try:
        statements = [s for s in sqlglot.parse(sql, dialect="duckdb") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise TemplateError(f"SQL syntax error: {str(e).splitlines()[0]}") from e
    if len(statements) != 1:
        raise TemplateError("a model is exactly one SELECT statement")
    stmt = statements[0]
    if not isinstance(stmt, exp.Query):
        raise TemplateError(f"a model must be a SELECT query, not {stmt.key.upper()}")
    return stmt
