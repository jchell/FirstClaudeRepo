"""Initializes, unseals and configures Vault for the platform. Safe to re-run.

Runs in a one-shot container (``docker compose run --rm bootstrap ...``) with the
host's DATAPLAT_HOME (default ``~/.dataplat``, outside the repo) mounted at
/dataplat-home:

    vault-init.json                  unseal key (protected with DPAPI by tasks.ps1 on Windows)
    postgres/bootstrap_password      one-time superuser password; replaced by a placeholder
                                     once Vault has rotated it
    minio/root_user, root_password   read by the MinIO container at start
    approle/<service>/role_id        per-service AppRole credentials, file-mounted
    approle/<service>/secret_id      into each container at /run/secrets/dataplat

The root token exists only for the duration of a run: the first run uses the token
from initialization; later runs generate a fresh one from the unseal key. Either
way it is revoked before exit.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import string
import sys
import time
from pathlib import Path

import hvac
from hvac.exceptions import InvalidPath, InvalidRequest

from dataplat.bootstrap.policies import DB_CREATION_SQL, DB_REVOCATION_SQL, SERVICES, service_policies
from dataplat.core.config import PlatformConfig, get_config

log = logging.getLogger("dataplat.bootstrap")

HOME = Path(os.environ.get("DATAPLAT_HOME", "/dataplat-home"))
INIT_FILE = HOME / "vault-init.json"
PG_BOOTSTRAP = HOME / "postgres" / "bootstrap_password"
PG_MANAGED = HOME / "postgres" / "managed-by-vault"
MINIO_DIR = HOME / "minio"
APPROLE_DIR = HOME / "approle"


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)


def _random(n: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


# ---------------------------------------------------------------- prepare

# The postgres image reads POSTGRES_PASSWORD_FILE on every start, so after Vault
# rotates the real password the file keeps this placeholder instead of disappearing.
PG_PLACEHOLDER = "rotated-and-managed-by-vault"


def _has_bootstrap_password() -> bool:
    return PG_BOOTSTRAP.exists() and PG_BOOTSTRAP.read_text().strip() != PG_PLACEHOLDER


def prepare() -> None:
    """Creates the bootstrap credentials Postgres and MinIO need on first start."""
    HOME.mkdir(parents=True, exist_ok=True)
    HOME.chmod(0o700)
    if not PG_MANAGED.exists() and not _has_bootstrap_password():
        _write_private(PG_BOOTSTRAP, _random())
        log.info("created one-time postgres bootstrap password")
    if not (MINIO_DIR / "root_password").exists():
        _write_private(MINIO_DIR / "root_user", "dataplat-" + _random(8).lower())
        _write_private(MINIO_DIR / "root_password", _random(40))
        log.info("created minio root credentials")
    # The postgres/minio containers run as non-root users and must read their files.
    for p in (PG_BOOTSTRAP, MINIO_DIR / "root_user", MINIO_DIR / "root_password"):
        if p.exists():
            p.chmod(0o644)
    for d in (PG_BOOTSTRAP.parent, MINIO_DIR):
        d.chmod(0o755)


# ---------------------------------------------------------------- unseal / root


def _unseal_key(from_stdin: bool) -> str:
    if from_stdin:
        return json.loads(sys.stdin.read())["unseal_keys_b64"][0]
    if not INIT_FILE.exists():
        raise SystemExit(f"{INIT_FILE} not found; pass the init JSON on stdin (--stdin)")
    return json.loads(INIT_FILE.read_text())["unseal_keys_b64"][0]


def _wait_for_vault(client: hvac.Client, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            client.sys.read_health_status(method="GET")
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def unseal(client: hvac.Client, key: str) -> None:
    if client.sys.is_sealed():
        client.sys.submit_unseal_key(key)
        if client.sys.is_sealed():
            raise SystemExit("vault is still sealed after submitting the unseal key")
        log.info("vault unsealed")


def _generate_root(client: hvac.Client, key: str) -> str:
    otp_len = client.sys.read_root_generation_progress()["otp_length"]
    otp = _random(otp_len) if otp_len else None
    client.sys.cancel_root_generation()
    start = client.sys.start_root_token_generation(otp=otp)
    resp = client.sys.generate_root(key=key, nonce=start["nonce"])
    if not resp.get("complete"):
        raise SystemExit("root token generation did not complete")
    encoded = base64.b64decode(resp["encoded_token"] + "==")
    return bytes(a ^ b for a, b in zip(encoded, otp.encode(), strict=True)).decode()


# ---------------------------------------------------------------- configure


def _ensure_mount(client: hvac.Client, path: str, backend: str, options: dict | None = None) -> None:
    mounts = client.sys.list_mounted_secrets_engines()["data"]
    if f"{path}/" not in mounts:
        client.sys.enable_secrets_engine(backend, path=path, options=options)
        log.info("enabled %s secrets engine at %s/", backend, path)


def configure(client: hvac.Client, cfg: PlatformConfig) -> None:
    v = cfg.vault

    if "file/" not in client.sys.list_enabled_audit_devices()["data"]:
        # 0644: the API reads it (read-only mount) to show admins who accessed which
        # secret; Vault HMACs every secret value in the log.
        client.sys.enable_audit_device("file", options={"file_path": "/vault/logs/audit.log", "mode": "0644"})
        log.info("enabled file audit device")

    _ensure_mount(client, v.kv_mount, "kv", {"version": "2"})
    _ensure_mount(client, v.transit_mount, "transit")
    _ensure_mount(client, v.database_mount, "database")
    if "approle/" not in client.sys.list_auth_methods()["data"]:
        client.sys.enable_auth_method("approle")

    # JWT signing key: ed25519, private half never leaves Vault.
    try:
        client.secrets.transit.read_key(name=cfg.auth.jwt_key, mount_point=v.transit_mount)
    except InvalidPath:
        client.secrets.transit.create_key(name=cfg.auth.jwt_key, key_type="ed25519", mount_point=v.transit_mount)
        log.info("created transit key %s", cfg.auth.jwt_key)

    # Platform infrastructure credentials.
    minio = {
        "root_user": (MINIO_DIR / "root_user").read_text().strip(),
        "root_password": (MINIO_DIR / "root_password").read_text().strip(),
    }
    path = f"{v.kv_prefix}/platform/minio"
    try:
        current = client.secrets.kv.v2.read_secret_version(
            path=path, mount_point=v.kv_mount, raise_on_deleted_version=True
        )
        unchanged = current["data"]["data"] == minio
    except InvalidPath:
        unchanged = False
    if not unchanged:
        client.secrets.kv.v2.create_or_update_secret(path=path, secret=minio, mount_point=v.kv_mount)
        log.info("stored minio credentials in vault")

    # Key for hash masking (keyed, so masked values can't be reversed by hashing guesses).
    # Generated once; rotating it changes every hash-masked value.
    mpath = f"{v.kv_prefix}/platform/masking"
    try:
        client.secrets.kv.v2.read_secret_version(path=mpath, mount_point=v.kv_mount, raise_on_deleted_version=True)
    except InvalidPath:
        client.secrets.kv.v2.create_or_update_secret(
            path=mpath, secret={"hmac_key": secrets.token_hex(32)}, mount_point=v.kv_mount
        )
        log.info("created the masking key in vault")

    _configure_database(client, cfg)

    for name, hcl in service_policies(cfg).items():
        client.sys.create_or_update_acl_policy(name=name, policy=hcl)

    # Token role used by the scheduler to mint service-account job tokens.
    client.write(
        f"auth/token/roles/{v.sa_token_role}",
        allowed_policies_glob=["sa-*"],
        orphan=True,
        renewable=True,
        token_explicit_max_ttl="24h",
    )

    for service, policy in SERVICES.items():
        client.auth.approle.create_or_update_approle(
            role_name=service,
            token_policies=[policy],
            token_ttl="1h",
            token_max_ttl="24h",
            secret_id_ttl="0",
        )
        role_id = client.auth.approle.read_role_id(role_name=service)["data"]["role_id"]
        d = APPROLE_DIR / service
        _write_private(d / "role_id", role_id)
        if not (d / "secret_id").exists():
            secret_id = client.auth.approle.generate_secret_id(role_name=service)["data"]["secret_id"]
            _write_private(d / "secret_id", secret_id)
            log.info("issued approle secret_id for %s", service)
        # Containers run as non-root users and need to read their own mount.
        for f in ("role_id", "secret_id"):
            (d / f).chmod(0o644)
        d.chmod(0o755)
    APPROLE_DIR.chmod(0o755)


def _configure_database(client: hvac.Client, cfg: PlatformConfig) -> None:
    v, pg = cfg.vault, cfg.postgres
    conn_name = "platform"
    if _has_bootstrap_password() and not PG_MANAGED.exists():
        client.secrets.database.configure(
            name=conn_name,
            plugin_name="postgresql-database-plugin",
            mount_point=v.database_mount,
            connection_url=f"postgresql://{{{{username}}}}:{{{{password}}}}@{pg.host}:{pg.port}/{pg.database}?sslmode=disable",
            username="postgres",
            password=PG_BOOTSTRAP.read_text().strip(),
            allowed_roles=[pg.vault_role],
            verify_connection=True,
            password_authentication="scram-sha-256",
        )
        # From here on only Vault knows the superuser password.
        client.secrets.database.rotate_root_credentials(name=conn_name, mount_point=v.database_mount)
        _write_private(PG_BOOTSTRAP, PG_PLACEHOLDER)
        PG_BOOTSTRAP.chmod(0o644)
        _write_private(PG_MANAGED, "postgres superuser password is managed by vault\n")
        log.info("vault now owns the postgres superuser password (rotated)")
    elif not PG_MANAGED.exists():
        raise SystemExit("no postgres bootstrap password and vault does not manage postgres; run `prepare` first")

    client.secrets.database.create_role(
        name=pg.vault_role,
        db_name=conn_name,
        creation_statements=DB_CREATION_SQL,
        revocation_statements=DB_REVOCATION_SQL,
        default_ttl="24h",
        max_ttl="720h",
        mount_point=v.database_mount,
    )


# ---------------------------------------------------------------- entry points


def run_vault(from_stdin: bool = False) -> dict | None:
    """Init (first run) + unseal + configure. Returns the init JSON on first run."""
    cfg = get_config()
    client = hvac.Client(url=cfg.vault.addr)
    _wait_for_vault(client)

    init_result = None
    if not client.sys.is_initialized():
        init_result = client.sys.initialize(secret_shares=1, secret_threshold=1)
        init_doc = {"unseal_keys_b64": init_result["keys_base64"], "initialized_at": time.time()}
        _write_private(INIT_FILE, json.dumps(init_doc, indent=2))
        log.info("vault initialized; unseal key written to %s", INIT_FILE)
        key, root = init_result["keys_base64"][0], init_result["root_token"]
        unseal(client, key)
    else:
        key = _unseal_key(from_stdin)
        unseal(client, key)
        root = _generate_root(client, key)

    client.token = root
    try:
        _wait_for_active(client)
        configure(client, cfg)
    finally:
        try:
            client.auth.token.revoke_self()
            log.info("root token revoked")
        except InvalidRequest:
            pass
    return init_result


def _wait_for_active(client: hvac.Client, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            client.sys.list_mounted_secrets_engines()
            return
        except Exception:
            time.sleep(1)
    raise SystemExit("vault did not become active")


def run_unseal(from_stdin: bool = False) -> None:
    cfg = get_config()
    client = hvac.Client(url=cfg.vault.addr)
    _wait_for_vault(client)
    unseal(client, _unseal_key(from_stdin))
