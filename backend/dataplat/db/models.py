"""Metadata schema (Postgres). Migrations live in backend/alembic."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB on Postgres, plain JSON elsewhere (unit tests on SQLite).
Json = JSON().with_variant(JSONB(), "postgresql")

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(128), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(256))
    email: Mapped[str | None] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Role(Base):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text, default="")


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(ForeignKey("roles.name", ondelete="CASCADE"), primary_key=True)


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")


class GroupMember(Base):
    __tablename__ = "group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


class GroupRole(Base):
    __tablename__ = "group_roles"

    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(ForeignKey("roles.name", ondelete="CASCADE"), primary_key=True)


class RefreshToken(Base):
    """Opaque refresh tokens, stored only as SHA-256 hashes.

    Tokens rotate on every use. Reusing an already-rotated token revokes its whole
    family, which cuts off a stolen token as soon as either party uses it again.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ServiceAccount(Base):
    """A non-human identity bound to its own Vault path and least-privilege policy."""

    __tablename__ = "service_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    vault_path: Mapped[str] = mapped_column(String(256))
    vault_policy: Mapped[str] = mapped_column(String(128))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128), index=True)
    target: Mapped[str | None] = mapped_column(String(512))
    outcome: Mapped[str] = mapped_column(String(16), default="success")
    detail: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))


class PlatformSetting(Base):
    """Runtime config registry for non-secret settings editable from the console."""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(Json)
    updated_by: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class JobRow(Base):
    """Postgres-backed job queue (claimed with FOR UPDATE SKIP LOCKED)."""

    __tablename__ = "job_queue"
    __table_args__ = (
        Index("ix_job_queue_claim", "status", "run_after"),
        # At most one queued/running job per dedupe key.
        Index(
            "uq_job_queue_active_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|succeeded|failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dedupe_key: Mapped[str | None] = mapped_column(String(256))
    service_account: Mapped[str | None] = mapped_column(String(64))
    # Response-wrapped, single-use Vault token (not a secret value itself: it can be
    # unwrapped exactly once, and a failed unwrap reveals tampering).
    vault_wrap_token: Mapped[str | None] = mapped_column(String(256))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(Json)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    cron: Mapped[str | None] = mapped_column(String(128))
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    service_account: Mapped[str | None] = mapped_column(String(64))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LineageEvent(Base):
    """OpenLineage RunEvents as received; graph views are derived from these."""

    __tablename__ = "lineage_events"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event_type: Mapped[str] = mapped_column(String(16))
    job_namespace: Mapped[str] = mapped_column(String(256))
    job_name: Mapped[str] = mapped_column(String(512), index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    event: Mapped[dict[str, Any]] = mapped_column(Json)


DEFAULT_ROLES = {
    "admin": "Full control of the platform, users and secrets",
    "engineer": "Build connections, ingestion jobs and pipelines",
    "steward": "Own data quality rules, glossary, classifications and ontologies",
    "analyst": "Query and consume published data",
    "viewer": "Read-only access to the console",
}


# ================================================================ Phase 1: ingestion & catalog


class Connection(Base):
    """A configured source. ``config`` holds only non-secret values and vault:// references."""

    __tablename__ = "connections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    type: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    # Secrets live under this service account's Vault path; jobs using the
    # connection run as it.
    service_account: Mapped[str | None] = mapped_column(String(64))
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_test_message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connections.id", ondelete="RESTRICT"), index=True)
    spec: Mapped[dict[str, Any]] = mapped_column(Json)
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("schedules.id", ondelete="SET NULL"))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IngestionJobVersion(Base):
    """Every saved JobSpec, so runs can say exactly which definition produced them."""

    __tablename__ = "ingestion_job_versions"

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    spec: Mapped[dict[str, Any]] = mapped_column(Json)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestionState(Base):
    __tablename__ = "ingestion_state"

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), primary_key=True)
    watermark: Mapped[Any] = mapped_column(Json, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IngestedFile(Base):
    __tablename__ = "ingested_files"
    __table_args__ = (Index("uq_ingested_files_job_path", "job_id", "path", unique=True),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_jobs.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String(1024))
    fingerprint: Mapped[str] = mapped_column(String(256))
    checksum: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    rows: Mapped[int] = mapped_column(BigInteger, default=0)
    batch_id: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (Index("ix_ingestion_runs_job_started", "job_id", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_jobs.id", ondelete="CASCADE"))
    job_version: Mapped[int] = mapped_column(Integer)
    queue_job_id: Mapped[int | None] = mapped_column(BigInteger)
    batch_id: Mapped[str] = mapped_column(String(64), index=True)
    lineage_run_id: Mapped[str | None] = mapped_column(String(64))
    trigger: Mapped[str] = mapped_column(String(32), default="manual")
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|succeeded|failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    rows_read: Mapped[int] = mapped_column(BigInteger, default=0)
    rows_written: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_read: Mapped[int] = mapped_column(BigInteger, default=0)
    files: Mapped[int] = mapped_column(Integer, default=0)
    table_version: Mapped[int | None] = mapped_column(BigInteger)
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    details: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (Index("uq_datasets_layer_name", "layer", "name", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    layer: Mapped[str] = mapped_column(String(16))  # bronze|silver|gold|vault
    name: Mapped[str] = mapped_column(String(256))
    uri: Mapped[str] = mapped_column(String(1024))
    format: Mapped[str] = mapped_column(String(32), default="delta")
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str | None] = mapped_column(String(128))
    source_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    row_count: Mapped[int | None] = mapped_column(BigInteger)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    table_version: Mapped[int | None] = mapped_column(BigInteger)
    last_loaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Freshness SLA: alert when the dataset hasn't been loaded for this long.
    freshness_sla_minutes: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DatasetColumn(Base):
    __tablename__ = "dataset_columns"

    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    data_type: Mapped[str] = mapped_column(String(128))
    nullable: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(Text, default="")
    is_audit: Mapped[bool] = mapped_column(Boolean, default=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SchemaChange(Base):
    __tablename__ = "schema_changes"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    changes: Mapped[list[dict[str, Any]]] = mapped_column(Json)


class DatasetProfile(Base):
    __tablename__ = "dataset_profiles"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    dataset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    row_count: Mapped[int] = mapped_column(BigInteger)
    columns: Mapped[list[dict[str, Any]]] = mapped_column(Json)


class PortalApp(Base):
    """A report, dashboard or business app listed in the App Portal."""

    __tablename__ = "portal_apps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    url: Mapped[str] = mapped_column(String(2048))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="report")  # report|dashboard|app|notebook
    owner: Mapped[str | None] = mapped_column(String(128))
    # Datasets the app reads (for report-level lineage; Phase 4 adds column level).
    datasets: Mapped[list[str]] = mapped_column(Json, default=list)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ================================================================ Phase 1b: streams, alerts


class StreamState(Base):
    """Runtime state of a continuous (CDC / event stream) ingestion job."""

    __tablename__ = "stream_state"

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), primary_key=True)
    # desired: what the operator asked for; status: what the stream worker reports.
    desired: Mapped[str] = mapped_column(String(16), default="running")  # running|paused
    status: Mapped[str] = mapped_column(String(16), default="starting")  # starting|running|paused|failed
    connector_name: Mapped[str | None] = mapped_column(String(255))
    topics: Mapped[list[str]] = mapped_column(Json, default=list)
    key_columns: Mapped[list[str] | None] = mapped_column(Json)
    worker: Mapped[str | None] = mapped_column(String(128))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_batch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    totals: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)


class StreamMetric(Base):
    """Per-minute stream metrics, for the Streams & Replication charts."""

    __tablename__ = "stream_metrics"

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), primary_key=True)
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    records: Mapped[int] = mapped_column(BigInteger, default=0)
    batches: Mapped[int] = mapped_column(Integer, default=0)
    dlq: Mapped[int] = mapped_column(Integer, default=0)
    latency_p50_ms: Mapped[int | None] = mapped_column(BigInteger)
    latency_p95_ms: Mapped[int | None] = mapped_column(BigInteger)
    lag: Mapped[int | None] = mapped_column(BigInteger)


class WebhookKey(Base):
    """API key for POST /ingest/events/{stream}; only its SHA-256 hash is stored."""

    __tablename__ = "webhook_keys"

    connection_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    hint: Mapped[str] = mapped_column(String(8))  # last characters, to tell keys apart
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_open", "kind", "target", "resolved_at"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))  # freshness|stream_failed|...
    severity: Mapped[str] = mapped_column(String(16), default="warning")  # warning|serious|critical
    target: Mapped[str] = mapped_column(String(512))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ================================================================ Phase 2: data vault & transform


class VaultObject(Base):
    """A Data Vault 2.0 object: hub, link, satellite, or a business-vault PIT/bridge."""

    __tablename__ = "vault_objects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(16))  # hub|link|sat|pit|bridge
    name: Mapped[str] = mapped_column(String(128), unique=True)
    definition: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class VaultMapping(Base):
    """Which source columns load a hub, link or satellite."""

    __tablename__ = "vault_mappings"
    __table_args__ = (
        Index("uq_vault_mappings_source_target", "source_layer", "source_dataset", "target_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    source_layer: Mapped[str] = mapped_column(String(16), default="bronze")
    source_dataset: Mapped[str] = mapped_column(String(256))
    target_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vault_objects.id", ondelete="CASCADE"), index=True)
    # hub: {business key: column}; link: {"<role>.<business key>": column};
    # sat: the parent's keys in the same form.
    keys: Mapped[dict[str, str]] = mapped_column(Json, default=dict)
    # sat only: {attribute: column}
    attributes: Mapped[dict[str, str]] = mapped_column(Json, default=dict)
    record_source: Mapped[str | None] = mapped_column(String(256))
    ingestion_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ingestion_jobs.id", ondelete="SET NULL"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VaultLoadState(Base):
    """High-water mark (source _load_ts) per mapping: rows at or below it are loaded."""

    __tablename__ = "vault_load_state"

    mapping_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vault_mappings.id", ondelete="CASCADE"), primary_key=True)
    high_water: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_loaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_rows: Mapped[int] = mapped_column(BigInteger, default=0)


class TransformModel(Base):
    """A silver/gold model: SQL, or a declarative SCD2 dimension, fact or date dimension."""

    __tablename__ = "transform_models"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    layer: Mapped[str] = mapped_column(String(16))  # silver|gold
    kind: Mapped[str] = mapped_column(String(32), default="sql")  # sql|scd2_dimension|fact|date_dimension
    sql: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    owner: Mapped[str | None] = mapped_column(String(128))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TransformModelVersion(Base):
    __tablename__ = "transform_model_versions"

    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transform_models.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    sql: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Pipeline(Base):
    """A set of models run in dependency order, on a schedule and/or when inputs change."""

    __tablename__ = "pipelines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    models: Mapped[list[str]] = mapped_column(Json, default=list)
    schedule: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    # Run whenever any of these datasets ("<layer>.<name>") gets new data.
    trigger_datasets: Mapped[list[str]] = mapped_column(Json, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TransformRun(Base):
    """One vault load, model build, pipeline run or serving sync."""

    __tablename__ = "transform_runs"
    __table_args__ = (Index("ix_transform_runs_target", "kind", "target", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(32))  # vault_load|vault_build|model|pipeline
    target: Mapped[str] = mapped_column(String(256))
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipelines.id", ondelete="SET NULL"), index=True)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    trigger: Mapped[str] = mapped_column(String(32), default="manual")
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|succeeded|failed|skipped
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    rows_written: Mapped[int] = mapped_column(BigInteger, default=0)
    table_version: Mapped[int | None] = mapped_column(BigInteger)
    lineage_run_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
