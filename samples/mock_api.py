"""A small REST API for testing the REST connector: auth styles and pagination styles.

Runs in the dev compose profile at http://mock-api:8090 (localhost:18090).
Credentials here are for this mock only.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response

TOKEN = "dev-api-token"
API_KEY = "dev-api-key"
CLIENT_ID, CLIENT_SECRET = "dev-client", "dev-client-secret"
BASE = datetime(2026, 1, 1, tzinfo=UTC)

app = FastAPI()

CUSTOMERS = [
    {"id": i, "name": f"Customer {i}", "email": f"c{i}@example.com", "tier": ["gold", "silver", "bronze"][i % 3],
     "address": {"city": ["Oslo", "Lyon", "Porto"][i % 3], "zip": f"{1000 + i}"},
     "updated_at": (BASE + timedelta(hours=i)).isoformat()}
    for i in range(1, 58)
]
ORDERS = [{"order_id": i, "customer_id": (i % 57) + 1, "total": round(i * 3.7, 2)} for i in range(1, 124)]
EVENTS = [{"event_id": f"e{i:04d}", "type": ["view", "click", "buy"][i % 3]} for i in range(1, 41)]
TICKETS = [{"ticket": i, "status": "open" if i % 2 else "closed"} for i in range(1, 26)]


def _bearer(authorization: str | None) -> None:
    if authorization not in (f"Bearer {TOKEN}", "Bearer oauth-issued-token"):
        raise HTTPException(401, "bad token")


@app.get("/")
def root() -> dict:
    return {"ok": True}


@app.post("/oauth/token")
async def token(request: Request) -> dict:
    grant_type = parse_qs((await request.body()).decode()).get("grant_type", [""])[0]
    auth = request.headers.get("authorization", "")
    expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    if grant_type != "client_credentials" or auth != expected:
        raise HTTPException(401, "bad client")
    return {"access_token": "oauth-issued-token", "token_type": "bearer", "expires_in": 3600}


@app.get("/v1/customers")
def customers(page: int = 1, page_size: int = 20, since: str | None = None, authorization: str | None = Header(None)) -> dict:
    """Page-number pagination, bearer auth, optional incremental ?since=."""
    _bearer(authorization)
    rows = [c for c in CUSTOMERS if not since or c["updated_at"] > since]
    start = (page - 1) * page_size
    return {"data": rows[start : start + page_size], "page": page, "total": len(rows)}


@app.get("/v1/orders")
def orders(offset: int = 0, limit: int = 50, x_api_key: str | None = Header(None)) -> list:
    """Offset pagination, API key header, bare-list body."""
    if x_api_key != API_KEY:
        raise HTTPException(401, "bad key")
    return ORDERS[offset : offset + limit]


@app.get("/v1/events")
def events(cursor: str | None = None, authorization: str | None = Header(None)) -> dict:
    """Cursor pagination (OAuth2 client-credentials token)."""
    _bearer(authorization)
    start = int(cursor or 0)
    nxt = start + 15
    return {"data": EVENTS[start:nxt], "meta": {"next": str(nxt) if nxt < len(EVENTS) else None}}


@app.get("/v1/tickets")
def tickets(response: Response, page: int = 1) -> list:
    """Link-header pagination, no auth."""
    size = 10
    if page * size < len(TICKETS):
        response.headers["Link"] = f'</v1/tickets?page={page + 1}>; rel="next"'
    return TICKETS[(page - 1) * size : page * size]


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8090, log_level="warning")
