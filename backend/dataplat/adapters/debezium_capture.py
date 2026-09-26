"""ChangeCapture on Debezium, managed through the Kafka Connect REST API."""

from __future__ import annotations

import time
from typing import Any

import httpx

from dataplat.core.config import PlatformConfig


class ChangeCaptureError(Exception):
    pass


class DebeziumChangeCapture:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self._http = httpx.Client(base_url=self.url, timeout=30)

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> DebeziumChangeCapture:
        return cls(config.change_capture.url)

    def _check(self, r: httpx.Response, what: str) -> httpx.Response:
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", "")
            except ValueError:
                msg = r.text[:300]
            # Kafka Connect error messages can echo the offending config; keep only the start.
            raise ChangeCaptureError(f"{what}: HTTP {r.status_code} {msg[:300]}")
        return r

    def create_connector(self, name: str, config: dict[str, Any]) -> None:
        """Creates or updates (PUT is idempotent)."""
        self._check(self._http.put(f"/connectors/{name}/config", json=config), f"configuring {name}")

    def delete_connector(self, name: str) -> None:
        r = self._http.delete(f"/connectors/{name}")
        if r.status_code != 404:
            self._check(r, f"deleting {name}")

    def exists(self, name: str) -> bool:
        return self._http.get(f"/connectors/{name}").status_code == 200

    def status(self, name: str) -> dict[str, Any]:
        r = self._http.get(f"/connectors/{name}/status")
        if r.status_code == 404:
            return {"state": "MISSING", "tasks": []}
        body = self._check(r, f"status of {name}").json()
        tasks = [
            {"id": t["id"], "state": t["state"], "trace": (t.get("trace") or "").splitlines()[:1]}
            for t in body.get("tasks", [])
        ]
        return {"state": body["connector"]["state"], "tasks": tasks}

    def pause(self, name: str) -> None:
        self._check(self._http.put(f"/connectors/{name}/pause"), f"pausing {name}")

    def resume(self, name: str) -> None:
        self._check(self._http.put(f"/connectors/{name}/resume"), f"resuming {name}")

    def restart(self, name: str) -> None:
        self._check(
            self._http.post(f"/connectors/{name}/restart", params={"includeTasks": "true"}), f"restarting {name}"
        )

    def reset_offsets(self, name: str, timeout: float = 60) -> None:
        """Stops the connector and clears its source offsets, so it snapshots again."""
        self._check(self._http.put(f"/connectors/{name}/stop"), f"stopping {name}")
        deadline = time.monotonic() + timeout
        while self.status(name)["state"] != "STOPPED":
            if time.monotonic() > deadline:
                raise ChangeCaptureError(f"{name} did not stop")
            time.sleep(1)
        self._check(self._http.delete(f"/connectors/{name}/offsets"), f"resetting offsets of {name}")
        self.resume(name)

    def health(self) -> bool:
        try:
            return self._http.get("/").status_code == 200
        except httpx.HTTPError:
            return False

    def close(self) -> None:
        self._http.close()
