"""REST connector against an in-process mock transport."""

from __future__ import annotations

import httpx
import pytest

from dataplat.adapters.memory import InMemorySecretStore
from dataplat.connectors.api import RestApiConnector
from dataplat.connectors.base import ConnectorError, ReadContext, ReadRequest

ROWS = [{"id": i, "meta": {"n": i}} for i in range(1, 26)]


def handler(request: httpx.Request) -> httpx.Response:
    path, q = request.url.path, request.url.params
    if path == "/links":
        page = int(q.get("page", 1))
        headers = {"Link": f'</links?page={page + 1}>; rel="next"'} if page * 10 < len(ROWS) else {}
        return httpx.Response(200, json=ROWS[(page - 1) * 10 : page * 10], headers=headers)
    if path == "/pages":
        if request.headers.get("authorization") != "Bearer tok":
            return httpx.Response(401)
        page, size = int(q["page"]), int(q["page_size"])
        return httpx.Response(200, json={"data": ROWS[(page - 1) * size : page * size]})
    if path == "/cursor":
        start = int(q.get("cursor", 0))
        nxt = start + 7
        return httpx.Response(200, json={"items": ROWS[start:nxt], "next": str(nxt) if nxt < len(ROWS) else None})
    return httpx.Response(404)


def make(config: dict, secrets: InMemorySecretStore | None = None) -> RestApiConnector:
    c = RestApiConnector({"base_url": "http://mock", **config}, secrets)
    c._http = httpx.Client(base_url="http://mock", transport=httpx.MockTransport(handler))
    return c


def read(c: RestApiConnector, endpoint: str, options: dict) -> list[dict]:
    ctx = ReadContext(namespace=c.namespace())
    rows = []
    for b in c.read(ReadRequest(object=endpoint, options=options), ctx):
        rows.extend(b.data.to_pylist())
    return rows


def test_link_header_pagination_keeps_the_next_urls_query() -> None:
    rows = read(make({}), "/links", {"pagination": {"type": "link_header"}})
    assert [r["id"] for r in rows] == list(range(1, 26))


def test_page_pagination_with_bearer_secret_and_jsonpath() -> None:
    secrets = InMemorySecretStore()
    secrets.write("sa/api", {"token": "tok"})
    c = make({"auth": {"type": "bearer", "token": "vault://kv/dataplat/sa/api#token"}}, secrets)
    rows = read(c, "/pages", {"records_path": "$.data[*]", "pagination": {"type": "page", "page_size": 10}})
    assert len(rows) == 25 and rows[0]["meta"] == '{"n": 1}'


def test_cursor_pagination() -> None:
    rows = read(
        make({}), "/cursor", {"records_path": "$.items[*]", "pagination": {"type": "cursor", "cursor_path": "$.next"}}
    )
    assert len(rows) == 25


def test_http_errors_become_connector_errors_without_query_strings() -> None:
    with pytest.raises(ConnectorError, match="HTTP 401"):
        read(make({"auth": {"type": "none"}}), "/pages", {"pagination": {"type": "page"}})
