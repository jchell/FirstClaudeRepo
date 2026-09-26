"""Secrets must never be serialized, logged or placed in config."""

from __future__ import annotations

import inspect
import logging
import typing

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel, SecretStr

from dataplat.api.app import create_app
from dataplat.core.logging import RedactingFilter, redact
from dataplat.core.secrets import SecretRef, WriteOnlySecret


class Conn(BaseModel):
    host: str
    password: WriteOnlySecret


def test_write_only_secret_accepts_input_but_refuses_serialization() -> None:
    c = Conn(host="db", password="hunter2hunter2")
    assert c.password.get_secret_value() == "hunter2hunter2"
    with pytest.raises(Exception, match="never be serialized"):
        c.model_dump()
    with pytest.raises(Exception, match="never be serialized"):
        c.model_dump_json()
    assert "hunter2" not in repr(c)


def test_write_only_secret_is_marked_write_only_in_openapi() -> None:
    schema = Conn.model_json_schema()
    assert schema["properties"]["password"]["writeOnly"] is True


def _contains_secret(tp: typing.Any, seen: set[int]) -> bool:
    if id(tp) in seen:
        return False
    seen.add(id(tp))
    if tp is SecretStr:
        return True
    if inspect.isclass(tp) and issubclass(tp, BaseModel):
        return any(_contains_secret(f.annotation, seen) for f in tp.model_fields.values())
    return any(_contains_secret(a, seen) for a in typing.get_args(tp))


def iter_api_routes(routes) -> list[APIRoute]:
    """Flattens routes, including FastAPI's wrappers around included routers."""
    out: list[APIRoute] = []
    for r in routes:
        if isinstance(r, APIRoute):
            out.append(r)
        elif (inner := getattr(r, "original_router", None)) is not None:
            out.extend(iter_api_routes(inner.routes))
    return out


def test_no_api_response_model_contains_a_secret_field() -> None:
    """Build breaker: any response model that could carry a secret value fails CI."""
    routes = iter_api_routes(create_app().routes)
    assert len(routes) >= 40  # guard against checking nothing
    offenders = [r.path for r in routes if r.response_model is not None and _contains_secret(r.response_model, set())]
    assert offenders == []


@pytest.mark.parametrize(
    ("raw", "leak"),
    [
        ("postgresql://app:S3cr3t!@db:5432/x", "S3cr3t!"),
        ("password=abc123xyz", "abc123xyz"),
        ('{"api_key": "k-123456"}', "k-123456"),
        ("Authorization: Bearer eyJhbGciOi.eyJzdWIiOiIx.c2lnbmF0dXJl", "eyJzdWIiOiIx"),
        ("token hvs.CAESIAbcdefghijklmnopqrstuvwxyz123456", "hvs.CAESIAbcdefghijklmnop"),
        ("secret_id: 1f2e3d4c-aaaa-bbbb", "1f2e3d4c"),
    ],
)
def test_redaction(raw: str, leak: str) -> None:
    assert leak not in redact(raw)


def test_log_filter_redacts_args_and_exceptions() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "connecting to %s", ("mysql://u:pw123@h/db",), None)
    RedactingFilter().filter(record)
    assert "pw123" not in record.getMessage()
    try:
        raise ValueError("password=topsecret99")
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    RedactingFilter().filter(record)
    assert "topsecret99" not in record.exc_text


@pytest.mark.parametrize(
    "ref",
    ["vault://kv/dataplat/connections/42#password", "vault://kv/a/b-c_d.e#key.1"],
)
def test_secret_ref_roundtrip(ref: str) -> None:
    assert str(SecretRef.parse(ref)) == ref


@pytest.mark.parametrize(
    "bad",
    ["kv/dataplat/x#y", "vault://kv/dataplat/x", "vault://kv/../etc#x", "vault://KV/x#y", "vault://kv/a b#c"],
)
def test_secret_ref_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        SecretRef.parse(bad)


def test_db_sessions_commit_before_the_response_is_sent() -> None:
    """Every endpoint's session must use scope="function" (see api/deps.get_session)."""
    from dataplat.api.deps import get_session

    def walk(dependant, out):
        for d in dependant.dependencies:
            if d.call is get_session:
                out.append(getattr(d, "scope", None) or getattr(d, "computed_scope", None))
            walk(d, out)

    scopes: list = []
    for route in iter_api_routes(create_app().routes):
        walk(route.dependant, scopes)
    assert scopes and set(scopes) == {"function"}, set(scopes)
