"""Service accounts bound to their own Vault path and least-privilege policy.

Each service account ``<name>`` owns ``kv/dataplat/service-accounts/<name>/*`` and a
Vault policy ``sa-<name>`` that can read only that path. Jobs that run as a service
account carry a single-use, response-wrapped Vault token minted with that policy;
the worker's own AppRole has no access to any service account's secrets.
"""

from __future__ import annotations

import re
from typing import Any

from dataplat.adapters.vault_client import VaultClient
from dataplat.core.config import VaultConfig

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise ValueError("service account names are 2-63 chars: lowercase letters, digits and '-'")
    return name


def policy_name(name: str) -> str:
    return f"sa-{name}"


def kv_path(cfg: VaultConfig, name: str) -> str:
    return f"{cfg.kv_prefix}/service-accounts/{name}"


def policy_hcl(cfg: VaultConfig, name: str) -> str:
    path = kv_path(cfg, name)
    return f"""# Managed by dataplat: service account {name}
path "{cfg.kv_mount}/data/{path}/*" {{
  capabilities = ["read"]
}}
path "{cfg.kv_mount}/metadata/{path}/*" {{
  capabilities = ["read", "list"]
}}
"""


class ServiceAccountVault:
    def __init__(self, vault: VaultClient, cfg: VaultConfig) -> None:
        self.vault = vault
        self.cfg = cfg

    def create_policy(self, name: str) -> str:
        policy = policy_name(validate_name(name))
        self.vault.call(lambda c: c.sys.create_or_update_acl_policy(name=policy, policy=policy_hcl(self.cfg, name)))
        return policy

    def delete_policy(self, name: str) -> None:
        self.vault.call(lambda c: c.sys.delete_acl_policy(name=policy_name(validate_name(name))))

    def verify_policy(self, name: str) -> None:
        """Refuses to mint for a policy whose rules were changed from the managed template."""
        resp = self.vault.call(lambda c: c.sys.read_acl_policy(name=policy_name(validate_name(name))))
        if resp["data"]["policy"].strip() != policy_hcl(self.cfg, name).strip():
            raise PermissionError(f"Vault policy {policy_name(name)} was modified outside dataplat")

    def mint_wrapped_token(self, name: str, ttl: str = "4h", wrap_ttl: str = "24h") -> str:
        """A token limited to the service account's policy, wrapped for single use."""
        policy = policy_name(validate_name(name))
        self.verify_policy(name)

        def _create(c: Any) -> Any:
            return c.adapter.post(
                f"/v1/auth/token/create/{self.cfg.sa_token_role}",
                json={"policies": [policy], "ttl": ttl, "renewable": True},
                headers={"X-Vault-Wrap-TTL": wrap_ttl},
            )

        resp = self.vault.call(_create)
        return resp["wrap_info"]["token"]
