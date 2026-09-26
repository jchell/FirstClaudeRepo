from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Principal:
    """An authenticated caller: a console user or a service account."""

    id: str
    name: str
    kind: str  # "user" | "service_account"
    roles: frozenset[str] = field(default_factory=frozenset)

    def has_role(self, *roles: str) -> bool:
        return "admin" in self.roles or any(r in self.roles for r in roles)


@runtime_checkable
class IdentityProvider(Protocol):
    """Issues and verifies platform access tokens.

    Local: built-in users/groups + JWT. Later: Keycloak (OIDC), Entra ID, Okta, Cognito.
    """

    def issue_access_token(self, principal: Principal) -> tuple[str, int]:
        """Returns (token, lifetime in seconds)."""
        ...

    def verify_access_token(self, token: str) -> Principal: ...
