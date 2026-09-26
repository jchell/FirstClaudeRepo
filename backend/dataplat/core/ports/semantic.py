from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class KnowledgeGraphStore(Protocol):
    """RDF triple store. Local: Oxigraph. Cloud: Neptune, GraphDB, Stardog, Fuseki."""

    def query(self, sparql: str) -> dict[str, Any]: ...
    def update(self, sparql_update: str) -> None: ...
    def load(self, data: bytes, content_type: str = "text/turtle", graph: str | None = None) -> None: ...
    def health(self) -> bool: ...


@runtime_checkable
class EntityResolver(Protocol):
    """Probabilistic record linkage. Local: Splink on DuckDB. Cloud: Splink on Spark, Senzing."""

    def train(self, settings: dict[str, Any], table: str) -> dict[str, Any]: ...
    def predict(self, model: dict[str, Any], table: str, threshold: float) -> Any: ...
