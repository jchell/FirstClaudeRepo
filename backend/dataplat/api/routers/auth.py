"""Console authentication. Contract used by console/src/auth/login.ts."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, SecretStr

from dataplat.api.deps import client_ip, current_principal, get_ctx
from dataplat.api.ratelimit import SlidingWindowLimiter
from dataplat.core import audit
from dataplat.core.context import PlatformContext
from dataplat.core.ports.identity import Principal
from dataplat.security.auth import AuthError, AuthService, IssuedSession

router = APIRouter(prefix="/api/auth", tags=["auth"])

REFRESH_COOKIE = "dataplat_refresh"
# Per client and username (account lockout covers brute force against one account),
# plus a looser cap per client address.
_login_limiter = SlidingWindowLimiter(limit=20, window_seconds=300)
_login_ip_limiter = SlidingWindowLimiter(limit=200, window_seconds=300)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=1, max_length=1024)


class UserOut(BaseModel):
    id: str
    username: str
    roles: list[str]


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


def _service(ctx: PlatformContext) -> AuthService:
    return AuthService(ctx.identity, ctx.config.auth)


def _respond(ctx: PlatformContext, response: Response, issued: IssuedSession) -> LoginResponse:
    response.set_cookie(
        REFRESH_COOKIE,
        issued.refresh_token,
        max_age=ctx.config.auth.refresh_token_ttl_seconds,
        httponly=True,
        secure=ctx.config.auth.secure_cookies,
        samesite="strict",
        path="/api/auth",
    )
    response.headers["Cache-Control"] = "no-store"
    p = issued.principal
    return LoginResponse(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        user=UserOut(id=p.id, username=p.name, roles=sorted(p.roles)),
    )


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, response: Response, ctx: PlatformContext = Depends(get_ctx)):
    ip = client_ip(request)
    if not _login_ip_limiter.allow(ip or "unknown") or not _login_limiter.allow(
        f"{ip}|{body.username.strip().lower()}"
    ):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many login attempts")

    failure: AuthError | None = None
    # Commit even when login fails, so failed-attempt counters and lockouts persist.
    with ctx.metadata.session() as s:
        try:
            issued = _service(ctx).login(s, body.username, body.password.get_secret_value())
            audit.record(s, actor=issued.principal.name, action="auth.login", ip=ip)
        except AuthError as e:
            failure = e
            audit.record(
                s,
                actor=body.username.strip().lower()[:128],
                action="auth.login",
                outcome="denied",
                detail={"reason": e.reason},
                ip=ip,
            )
    if failure is not None:
        if failure.reason == "account_locked":
            raise HTTPException(status.HTTP_423_LOCKED, "account locked")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")
    return _respond(ctx, response, issued)


@router.post("/refresh", response_model=LoginResponse)
def refresh(
    response: Response,
    ctx: PlatformContext = Depends(get_ctx),
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
):
    if not refresh_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no session")
    failed = False
    # Commit even on failure: replaying a rotated token revokes its whole family.
    with ctx.metadata.session() as s:
        try:
            issued = _service(ctx).refresh(s, refresh_token)
        except AuthError:
            failed = True
    if failed:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired", headers={"set-cookie": _clear_cookie()})
    return _respond(ctx, response, issued)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    ctx: PlatformContext = Depends(get_ctx),
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
):
    if refresh_token:
        with ctx.metadata.session() as s:
            _service(ctx).logout(s, refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"set-cookie": _clear_cookie()})


@router.get("/me", response_model=UserOut)
def me(principal: Principal = Depends(current_principal)):
    return UserOut(id=principal.id, username=principal.name, roles=sorted(principal.roles))


def _clear_cookie() -> str:
    return f"{REFRESH_COOKIE}=; Max-Age=0; Path=/api/auth; HttpOnly; SameSite=Strict"
