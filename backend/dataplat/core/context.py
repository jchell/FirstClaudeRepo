"""Per-process wiring: config + adapter registry + platform services."""

from __future__ import annotations

from functools import cached_property
from typing import Any

from dataplat.core.config import PlatformConfig, get_config
from dataplat.core.ports import (
    EventBus,
    JobQueue,
    KnowledgeGraphStore,
    LineageSink,
    MetadataStore,
    ObjectStore,
    QueryEngine,
    SecretStore,
    TokenSigner,
)
from dataplat.core.registry import Registry


class PlatformContext:
    def __init__(self, config: PlatformConfig | None = None, overrides: dict[str, Any] | None = None) -> None:
        self.config = config or get_config()
        self.registry = Registry(self.config, overrides)

    @property
    def secrets(self) -> SecretStore:
        return self.registry.get("secret_store")

    @property
    def metadata(self) -> MetadataStore:
        return self.registry.get("metadata_store")

    @property
    def objects(self) -> ObjectStore:
        return self.registry.get("object_store")

    @property
    def tables(self):  # -> DeltaTableFormat (TableFormat port)
        return self.registry.get("table_format")

    @property
    def query_engine(self) -> QueryEngine:
        return self.registry.get("query_engine")

    @property
    def events(self) -> EventBus:
        return self.registry.get("event_bus")

    @property
    def jobs(self) -> JobQueue:
        return self.registry.get("job_queue")

    @property
    def change_capture(self):  # -> DebeziumChangeCapture (ChangeCapture port)
        return self.registry.get("change_capture")

    @property
    def serving(self):  # -> PostgresServingStore (ServingStore port)
        return self.registry.get("serving_store")

    @property
    def signer(self) -> TokenSigner:
        return self.registry.get("token_signer")

    @property
    def knowledge_graph(self) -> KnowledgeGraphStore:
        return self.registry.get("knowledge_graph")

    @property
    def lineage(self) -> LineageSink:
        return self.registry.get("lineage_sink")

    @cached_property
    def identity(self):  # -> JwtIdentityProvider
        from dataplat.adapters.jwt_identity import JwtIdentityProvider

        return JwtIdentityProvider(self.signer, self.config.auth)

    @cached_property
    def service_account_vault(self):  # -> ServiceAccountVault | None
        from dataplat.adapters.vault_secrets import shared_vault
        from dataplat.security.service_accounts import ServiceAccountVault

        if "_sa_vault" in self.registry._instances:  # test override
            return self.registry._instances["_sa_vault"]
        return ServiceAccountVault(shared_vault(self.registry), self.config.vault)

    def close(self) -> None:
        self.registry.close()
