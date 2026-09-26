from __future__ import annotations

from collections.abc import Callable, Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from dataplat.adapters.jwt_identity import InvalidToken
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal

_bearer = HTTPBearer(auto_error=False)


def get_ctx(request: Request) -> PlatformContext:
    return request.app.state.ctx


def get_session(ctx: PlatformContext = Depends(get_ctx)) -> Iterator[Session]:
    with ctx.metadata.session() as s:
        yield s


def current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ctx: PlatformContext = Depends(get_ctx),
) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated", {"WWW-Authenticate": "Bearer"})
    try:
        return ctx.identity.verify_access_token(creds.credentials)
    except InvalidToken as e:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid or expired token", {"WWW-Authenticate": "Bearer"}
        ) from e


def require_roles(*roles: str) -> Callable[[Principal], Principal]:
    """Allows admins plus any of ``roles``."""

    def check(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has_role(*roles):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        return principal

    return check


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None
