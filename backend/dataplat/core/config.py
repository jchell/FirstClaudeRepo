"""Loads config/platform.yaml with ${ENV:-default} interpolation."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from dataplat.core.secrets import SecretRef

_ENV_RE = re.compile(r"\$\{(?P<name>[A-Z0-9_]+)(?::-(?P<default>[^}]*))?\}")


class VaultConfig(BaseModel):
    addr: str
    approle_dir: str
    kv_mount: str = "kv"
    kv_prefix: str = "dataplat"
    transit_mount: str = "transit"
    database_mount: str = "database"
    sa_token_role: str = "sa-job"


class PostgresConfig(BaseModel):
    host: str
    port: int = 5432
    database: str = "dataplat"
    vault_role: str = "dataplat-app"


class ObjectStoreConfig(BaseModel):
    endpoint: str
    credentials: str
    secret: str
    buckets: list[str] = []


class LakeConfig(BaseModel):
    bronze: str = "s3://bronze"
    silver: str = "s3://silver"
    gold: str = "s3://gold"
    vault: str = "s3://vault"  # raw vault + business vault (PIT, bridge)

    def uri(self, layer: str, name: str) -> str:
        return f"{getattr(self, layer).rstrip('/')}/{name}"


class EventBusConfig(BaseModel):
    bootstrap_servers: str
    schema_registry: str | None = None


class ChangeCaptureConfig(BaseModel):
    url: str = "http://kafka-connect:8083"


class SmtpConfig(BaseModel):
    host: str | None = None  # unset: email channels are disabled
    port: int = 25
    sender: str = "dataplat@localhost"
    starttls: bool = False
    username: str | None = None
    password: str | None = None  # vault:// reference


class NotificationsConfig(BaseModel):
    smtp: SmtpConfig = SmtpConfig()
    timeout_seconds: float = 10


class StreamingConfig(BaseModel):
    lineage_every_seconds: float = 300
    profile_every_seconds: float = 600


class ServingConfig(BaseModel):
    database: str = "serving"


class KnowledgeGraphConfig(BaseModel):
    url: str


class AuthConfig(BaseModel):
    jwt_key: str = "dataplat-jwt"
    issuer: str = "dataplat"
    audience: str = "dataplat-console"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 604800
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    secure_cookies: bool = False


class WorkerConfig(BaseModel):
    poll_interval_seconds: float = 2
    max_attempts: int = 3


class SchedulerConfig(BaseModel):
    tick_seconds: float = 15


class PlatformConfig(BaseModel):
    adapters: dict[str, str]
    vault: VaultConfig
    postgres: PostgresConfig
    object_store: ObjectStoreConfig
    lake: LakeConfig = LakeConfig()
    event_bus: EventBusConfig
    knowledge_graph: KnowledgeGraphConfig
    change_capture: ChangeCaptureConfig = ChangeCaptureConfig()
    streaming: StreamingConfig = StreamingConfig()
    serving: ServingConfig = ServingConfig()
    notifications: NotificationsConfig = NotificationsConfig()
    auth: AuthConfig = AuthConfig()
    worker: WorkerConfig = WorkerConfig()
    scheduler: SchedulerConfig = SchedulerConfig()

    def model_post_init(self, _: Any) -> None:
        for field in ("credentials", "secret"):
            SecretRef.parse(getattr(self.object_store, field))  # config may only hold references


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m["name"], m["default"] or ""), value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def default_config_path() -> Path:
    if env := os.environ.get("DATAPLAT_CONFIG"):
        return Path(env)
    # backend/dataplat/core/config.py -> repo root /config/platform.yaml
    return Path(__file__).resolve().parents[3] / "config" / "platform.yaml"


def load_config(path: Path | None = None) -> PlatformConfig:
    raw = yaml.safe_load((path or default_config_path()).read_text())
    return PlatformConfig.model_validate(_interpolate(raw))


@lru_cache
def get_config() -> PlatformConfig:
    return load_config()
