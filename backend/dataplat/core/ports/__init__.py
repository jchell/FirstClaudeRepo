"""Abstract interfaces for every swappable piece of infrastructure.

Each port has a local adapter in ``dataplat.adapters`` and room for cloud adapters,
selected per port in ``config/platform.yaml``.
"""

from dataplat.core.ports.events import ChangeCapture, Event, EventBus, StreamProcessor
from dataplat.core.ports.identity import IdentityProvider, Principal
from dataplat.core.ports.metadata import LineageSink, MetadataStore
from dataplat.core.ports.orchestration import Job, JobQueue, Orchestrator
from dataplat.core.ports.secrets import SecretStore, TokenSigner
from dataplat.core.ports.semantic import EntityResolver, KnowledgeGraphStore
from dataplat.core.ports.storage import ObjectStore, QueryEngine, ServingStore, TableFormat

__all__ = [
    "ChangeCapture",
    "EntityResolver",
    "Event",
    "EventBus",
    "IdentityProvider",
    "Job",
    "JobQueue",
    "KnowledgeGraphStore",
    "LineageSink",
    "MetadataStore",
    "ObjectStore",
    "Orchestrator",
    "Principal",
    "QueryEngine",
    "SecretStore",
    "ServingStore",
    "StreamProcessor",
    "TableFormat",
    "TokenSigner",
]
