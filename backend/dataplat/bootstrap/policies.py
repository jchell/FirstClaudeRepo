"""Least-privilege Vault policies, one per platform service."""

from __future__ import annotations

from dataplat.core.config import PlatformConfig


def service_policies(cfg: PlatformConfig) -> dict[str, str]:
    v = cfg.vault
    kv, pre, transit, db = v.kv_mount, v.kv_prefix, v.transit_mount, v.database_mount
    jwt, role = cfg.auth.jwt_key, cfg.postgres.vault_role

    db_creds = f'path "{db}/creds/{role}" {{ capabilities = ["read"] }}\n'
    platform_read = f'path "{kv}/data/{pre}/platform/*" {{ capabilities = ["read"] }}\n'

    return {
        # Console/API: writes secrets but can never read them back; signs JWTs.
        "dataplat-api": db_creds
        + platform_read
        + f'path "{kv}/data/{pre}/connections/*" {{ capabilities = ["create", "update"] }}\n'
        + f'path "{kv}/data/{pre}/service-accounts/*" {{ capabilities = ["create", "update"] }}\n'
        + f'path "{kv}/metadata/{pre}/connections/*" {{ capabilities = ["read", "list", "delete"] }}\n'
        + f'path "{kv}/metadata/{pre}/service-accounts/*" {{ capabilities = ["read", "list", "delete"] }}\n'
        + f'path "{transit}/sign/{jwt}" {{ capabilities = ["update"] }}\n'
        + f'path "{transit}/keys/{jwt}" {{ capabilities = ["read"] }}\n'
        + 'path "sys/policies/acl/sa-*" { capabilities = ["create", "update", "delete"] }\n',
        # Workers: no access to service-account or connection secrets of their own;
        # each job brings a single-use token scoped to its service account.
        "dataplat-worker": db_creds + platform_read,
        "dataplat-stream-worker": db_creds + platform_read,
        # Scheduler: the only service that can mint service-account job tokens.
        "dataplat-scheduler": db_creds
        + platform_read
        + f'path "auth/token/create/{v.sa_token_role}" {{ capabilities = ["update"] }}\n'
        + 'path "sys/policies/acl/sa-*" { capabilities = ["read"] }\n',
        # Kafka Connect (Debezium) logs into CDC sources itself, resolving ${vault:...}
        # placeholders in connector configs with its Vault config provider. Its REST API
        # is only reachable on the compose network and 127.0.0.1, and only the platform
        # creates connectors on it.
        "dataplat-kafka-connect": (
            f'path "{kv}/data/{pre}/service-accounts/+/connections/*" {{ capabilities = ["read"] }}\n'
            f'path "{kv}/data/{pre}/cdc/*" {{ capabilities = ["read"] }}\n'
        ),
    }


# service name (as used for the AppRole and the compose secrets dir) -> policy
SERVICES = {
    "api": "dataplat-api",
    "worker": "dataplat-worker",
    "stream-worker": "dataplat-stream-worker",
    "scheduler": "dataplat-scheduler",
    "kafka-connect": "dataplat-kafka-connect",
}

DB_CREATION_SQL = [
    """CREATE ROLE "{{name}}" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}' """
    """IN ROLE dataplat_owner;""",
    # Every session acts as dataplat_owner, so objects never belong to an expiring login.
    """ALTER ROLE "{{name}}" SET role = 'dataplat_owner';""",
]
DB_REVOCATION_SQL = [
    """SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{{name}}';""",
    """REASSIGN OWNED BY "{{name}}" TO dataplat_owner;""",
    """DROP OWNED BY "{{name}}";""",
    """DROP ROLE IF EXISTS "{{name}}";""",
]
