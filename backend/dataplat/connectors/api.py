"""REST API sources: auth, pagination and JSONPath record extraction."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any, Literal
from urllib.parse import urljoin

import httpx
import pyarrow as pa
from jsonpath_ng.ext import parse as jsonpath
from pydantic import BaseModel, Field

from dataplat.connectors.base import Batch, Connector, ConnectorError, ReadContext, ReadRequest, SourceObject
from dataplat.connectors.files import normalize_records


class RestAuth(BaseModel):
    type: Literal["none", "basic", "bearer", "api_key", "oauth2_client_credentials"] = "none"
    username: str | None = None
    password: str | None = Field(default=None, description="vault:// reference (basic)")
    token: str | None = Field(default=None, description="vault:// reference (bearer / api key)")
    header_name: str = Field(default="X-API-Key", description="api_key: header name")
    query_param: str | None = Field(default=None, description="api_key: send as this query parameter instead")
    token_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = Field(default=None, description="vault:// reference (oauth2)")
    scope: str | None = None


class RestConfig(BaseModel):
    base_url: str
    auth: RestAuth = RestAuth()
    headers: dict[str, str] = {}
    timeout_seconds: float = 30
    verify_tls: bool = True


class Pagination(BaseModel):
    type: Literal["none", "page", "offset", "cursor", "link_header", "next_url"] = "none"
    page_param: str = "page"
    start_page: int = 1
    size_param: str | None = "page_size"
    page_size: int = 100
    offset_param: str = "offset"
    limit_param: str = "limit"
    cursor_param: str = "cursor"
    cursor_path: str | None = Field(default=None, description="JSONPath to the next cursor, e.g. $.meta.next")
    next_url_path: str | None = Field(default=None, description="JSONPath to the next page URL")
    max_pages: int = 1000


class RestReadOptions(BaseModel):
    method: Literal["GET", "POST"] = "GET"
    params: dict[str, Any] = {}
    body: dict[str, Any] | None = None
    records_path: str = Field(default="$", description="JSONPath to the list of records, e.g. $.data[*]")
    pagination: Pagination = Pagination()
    # Incremental: send the last watermark as this query parameter.
    watermark_param: str | None = None


class RestApiConnector(Connector):
    type = "rest_api"
    label = "REST API"
    category = "api"
    Config = RestConfig

    secret_fields = ("auth.password", "auth.token", "auth.client_secret")

    def __init__(self, config: dict[str, Any], secrets: Any = None) -> None:
        super().__init__(config, secrets)
        self._http: httpx.Client | None = None
        self._oauth: tuple[str, float] | None = None

    def namespace(self) -> str:
        return self.config.base_url.rstrip("/")

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            c = self.config
            self._http = httpx.Client(
                base_url=c.base_url, headers=c.headers, timeout=c.timeout_seconds, verify=c.verify_tls
            )
        return self._http

    def _auth(self) -> tuple[dict[str, str], dict[str, str], Any]:
        """Returns (headers, query params, httpx auth)."""
        a = self.config.auth
        if a.type == "basic":
            return {}, {}, httpx.BasicAuth(a.username or "", self.secret("auth.password") or "")
        if a.type == "bearer":
            return {"Authorization": f"Bearer {self.secret('auth.token')}"}, {}, None
        if a.type == "api_key":
            key = self.secret("auth.token") or ""
            return ({}, {a.query_param: key}, None) if a.query_param else ({a.header_name: key}, {}, None)
        if a.type == "oauth2_client_credentials":
            if self._oauth is None or self._oauth[1] < time.monotonic():
                if not a.token_url:
                    raise ConnectorError("oauth2 needs a token_url")
                r = httpx.post(
                    a.token_url,
                    data={"grant_type": "client_credentials", **({"scope": a.scope} if a.scope else {})},
                    auth=(a.client_id or "", self.secret("auth.client_secret") or ""),
                    timeout=self.config.timeout_seconds,
                )
                if r.status_code >= 400:
                    raise ConnectorError(f"token endpoint returned HTTP {r.status_code}")
                body = r.json()
                self._oauth = (body["access_token"], time.monotonic() + float(body.get("expires_in", 300)) - 30)
            return {"Authorization": f"Bearer {self._oauth[0]}"}, {}, None
        return {}, {}, None

    def _request(self, method: str, url: str, params: dict[str, Any], body: Any) -> httpx.Response:
        headers, auth_params, auth = self._auth()
        for attempt in range(4):
            try:
                r = self.http.request(
                    method, url, params={**params, **auth_params}, json=body, headers=headers, auth=auth
                )
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise ConnectorError(f"request failed: {type(e).__name__}") from e
                time.sleep(2**attempt)
                continue
            if r.status_code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(float(r.headers.get("Retry-After", 2**attempt)))
                continue
            if r.status_code >= 400:
                raise ConnectorError(f"HTTP {r.status_code} from {r.request.url.copy_with(query=None)}")
            return r
        raise ConnectorError("request kept failing")

    def test(self) -> dict[str, Any]:
        r = self._request("GET", self.config.base_url, {}, None)
        return {"status": r.status_code}

    def discover(self, pattern: str | None = None) -> list[SourceObject]:
        # REST APIs don't list their resources in a standard way; endpoints are entered.
        return []

    def read(self, request: ReadRequest, ctx: ReadContext) -> Iterator[Batch]:
        if not request.object:
            raise ConnectorError("enter an endpoint path, e.g. /v1/customers")
        opts = RestReadOptions.model_validate(request.options)
        pg = opts.pagination
        records_expr = jsonpath(opts.records_path)
        params = dict(opts.params)
        if request.load_mode == "incremental" and opts.watermark_param and request.last_watermark is not None:
            params[opts.watermark_param] = request.last_watermark
        url: str | None = request.object
        page, offset, cursor = pg.start_page, 0, None
        ctx.inputs.append(request.object)
        new_wm = request.last_watermark

        for _ in range(pg.max_pages):
            page_params = dict(params)
            if pg.type == "page":
                page_params[pg.page_param] = page
                if pg.size_param:
                    page_params[pg.size_param] = pg.page_size
            elif pg.type == "offset":
                page_params[pg.offset_param] = offset
                page_params[pg.limit_param] = pg.page_size
            elif pg.type == "cursor" and cursor is not None:
                page_params[pg.cursor_param] = cursor
            assert url is not None
            r = self._request(opts.method, url, page_params, opts.body)
            ctx.bytes += len(r.content)
            doc = r.json()
            records = [m.value for m in records_expr.find(doc)]
            if len(records) == 1 and isinstance(records[0], list):
                records = records[0]
            if request.max_records is not None:
                records = records[: max(request.max_records - ctx.rows, 0)]
            if records:
                table = pa.Table.from_pylist(normalize_records(records))
                if request.watermark_column and request.watermark_column in table.column_names:
                    vals = [v for v in table.column(request.watermark_column).to_pylist() if v is not None]
                    if vals and (new_wm is None or max(vals) > new_wm):
                        new_wm = max(vals)
                ctx.rows += table.num_rows
                yield Batch(table, source=f"{self.namespace()}{request.object}")
            if request.max_records is not None and ctx.rows >= request.max_records:
                break
            # Next page?
            if pg.type == "none" or not records:
                break
            if pg.type == "page":
                page += 1
                if len(records) < pg.page_size:
                    break
            elif pg.type == "offset":
                offset += len(records)
                if len(records) < pg.page_size:
                    break
            elif pg.type == "cursor":
                found = jsonpath(pg.cursor_path or "$.next_cursor").find(doc)
                cursor = found[0].value if found else None
                if not cursor:
                    break
            elif pg.type == "next_url":
                found = jsonpath(pg.next_url_path or "$.next").find(doc)
                nxt = found[0].value if found else None
                if not nxt:
                    break
                url, params = urljoin(str(r.url), nxt), {}
            elif pg.type == "link_header":
                nxt = r.links.get("next", {}).get("url")
                if not nxt:
                    break
                url, params = nxt, {}
        ctx.new_watermark = new_wm

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
