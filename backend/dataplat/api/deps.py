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
    """Request-scoped DB session, committed when the endpoint returns.

    Always depend on it with ``scope="function"``: the default ("request") commits only
    after the response is sent, so a fast client could act on a row that isn't
    committed yet (e.g. run a job it has just created and get a 404).
    """
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


def get_policy(
    s: Session = Depends(get_session, scope="function"), principal: Principal = Depends(current_principal)
):  # -> PolicyEngine
    """Data access policies for the caller (grants, masking, row filters)."""
    from dataplat.security.policy import PolicyEngine

    return PolicyEngine(s, principal)


def task_principal(p: Principal) -> dict[str, object]:
    """Payload fields that make a worker task act for (and belong to) the caller."""
    return {
        "principal": {"id": p.id, "name": p.name, "kind": p.kind, "roles": sorted(p.roles)},
        "requested_by": p.name,
    }
