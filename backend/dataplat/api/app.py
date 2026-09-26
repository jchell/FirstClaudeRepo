from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from dataplat.api.routers import admin, auth, catalog, connections, ingestion, platform, streams
from dataplat.core.context import PlatformContext
from dataplat.core.logging import configure_logging


def create_app(ctx: PlatformContext | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.ctx = ctx or PlatformContext()
        yield
        app.state.ctx.close()

    app = FastAPI(
        title="Data Platform API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI echoes submitted values in validation errors by default; those can be
        # passwords or secrets, so return only where and why validation failed.
        detail = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": detail})

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(platform.router)
    app.include_router(connections.router)
    app.include_router(ingestion.router)
    app.include_router(catalog.router)
    app.include_router(streams.router)
    return app


def main() -> None:
    import uvicorn

    configure_logging()
    # The API is reachable only on the compose network and 127.0.0.1, behind the console's
    # nginx, which overwrites X-Forwarded-For with the real client address; trusting it
    # gives per-client rate limits and audit IPs instead of the proxy's address.
    uvicorn.run(
        create_app(), host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="*", log_config=None
    )
