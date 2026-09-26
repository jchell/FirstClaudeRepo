"""Username/password login, refresh-token rotation and account lockout."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from dataplat.core.config import AuthConfig
from dataplat.core.ports.identity import IdentityProvider, Principal
from dataplat.db.models import GroupMember, GroupRole, RefreshToken, User, UserRole
from dataplat.security.passwords import hash_password, needs_rehash, verify_password


class AuthError(Exception):
    """Login failed. ``reason`` maps to an HTTP status in the API layer."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # invalid_credentials | account_locked | invalid_refresh


@dataclass
class IssuedSession:
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_at: datetime
    principal: Principal
    user: User


def _now() -> datetime:
    return datetime.now(UTC)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def user_roles(session: Session, user_id: uuid.UUID) -> frozenset[str]:
    direct = session.scalars(select(UserRole.role).where(UserRole.user_id == user_id))
    via_groups = session.scalars(
        select(GroupRole.role)
        .join(GroupMember, GroupMember.group_id == GroupRole.group_id)
        .where(GroupMember.user_id == user_id)
    )
    return frozenset([*direct, *via_groups])


def principal_for(session: Session, user: User) -> Principal:
    return Principal(id=str(user.id), name=user.username, kind="user", roles=user_roles(session, user.id))


class AuthService:
    def __init__(self, identity: IdentityProvider, cfg: AuthConfig) -> None:
        self.identity = identity
        self.cfg = cfg

    def _issue(self, session: Session, user: User, family_id: uuid.UUID | None = None) -> IssuedSession:
        principal = principal_for(session, user)
        access, ttl = self.identity.issue_access_token(principal)
        refresh = secrets.token_urlsafe(48)
        expires = _now() + timedelta(seconds=self.cfg.refresh_token_ttl_seconds)
        session.add(
            RefreshToken(
                user_id=user.id,
                family_id=family_id or uuid.uuid4(),
                token_hash=_hash_token(refresh),
                expires_at=expires,
            )
        )
        return IssuedSession(access, ttl, refresh, expires, principal, user)

    def login(self, session: Session, username: str, password: str) -> IssuedSession:
        user = session.scalars(select(User).where(User.username == username.strip().lower()).with_for_update()).first()
        now = _now()

        if user is not None and user.locked_until and _aware(user.locked_until) > now:
            verify_password(None, password)  # keep timing uniform
            raise AuthError("account_locked")

        if not verify_password(user.password_hash if user else None, password) or user is None:
            if user is not None:
                user.failed_logins += 1
                if user.failed_logins >= self.cfg.max_failed_logins:
                    user.locked_until = now + timedelta(minutes=self.cfg.lockout_minutes)
                    user.failed_logins = 0
            raise AuthError("invalid_credentials")

        if not user.is_active:
            raise AuthError("invalid_credentials")

        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        user.failed_logins = 0
        user.locked_until = None
        user.last_login_at = now
        return self._issue(session, user)

    def refresh(self, session: Session, refresh_token: str) -> IssuedSession:
        row = session.scalars(
            select(RefreshToken).where(RefreshToken.token_hash == _hash_token(refresh_token)).with_for_update()
        ).first()
        now = _now()
        if row is None or row.revoked_at is not None or _aware(row.expires_at) <= now:
            raise AuthError("invalid_refresh")
        if row.used_at is not None:
            # Replay of a rotated token: assume theft and revoke the whole family.
            self._revoke_family(session, row.family_id)
            raise AuthError("invalid_refresh")

        user = session.get(User, row.user_id)
        if user is None or not user.is_active:
            raise AuthError("invalid_refresh")
        row.used_at = now
        return self._issue(session, user, family_id=row.family_id)

    def logout(self, session: Session, refresh_token: str) -> None:
        row = session.scalars(select(RefreshToken).where(RefreshToken.token_hash == _hash_token(refresh_token))).first()
        if row is not None:
            self._revoke_family(session, row.family_id)

    def revoke_user_sessions(self, session: Session, user_id: uuid.UUID) -> None:
        session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_now())
        )

    @staticmethod
    def _revoke_family(session: Session, family_id: uuid.UUID) -> None:
        session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_now())
        )
