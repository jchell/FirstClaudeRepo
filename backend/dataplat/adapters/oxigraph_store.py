"""KnowledgeGraphStore on an Oxigraph server (SPARQL 1.1 Protocol + Graph Store Protocol)."""

from __future__ import annotations

from typing import Any

import httpx

from dataplat.core.config import PlatformConfig


class OxigraphStore:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self._http = httpx.Client(timeout=60)

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> OxigraphStore:
        return cls(config.knowledge_graph.url)

    def query(self, sparql: str) -> dict[str, Any]:
        r = self._http.post(
            f"{self.url}/query",
            content=sparql,
            headers={"Content-Type": "application/sparql-query", "Accept": "application/sparql-results+json"},
        )
        r.raise_for_status()
        return r.json()

    def update(self, sparql_update: str) -> None:
        r = self._http.post(
            f"{self.url}/update", content=sparql_update, headers={"Content-Type": "application/sparql-update"}
        )
        r.raise_for_status()

    def load(self, data: bytes, content_type: str = "text/turtle", graph: str | None = None) -> None:
        params = {"graph": graph} if graph else {"default": ""}
        r = self._http.post(f"{self.url}/store", params=params, content=data, headers={"Content-Type": content_type})
        r.raise_for_status()

    def health(self) -> bool:
        try:
            self.query("ASK { }")
            return True
        except Exception:
            return False

    def close(self) -> None:
        self._http.close()
