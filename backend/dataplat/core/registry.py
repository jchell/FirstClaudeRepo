"""Resolves the adapter configured for each port through Python entry points.

``config/platform.yaml`` names an adapter per port (e.g. ``object_store: minio``);
the name maps to a class registered under the ``dataplat.adapters.<port>`` entry-point
group. Third-party packages can register cloud adapters the same way.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

from dataplat.core.config import PlatformConfig


class AdapterNotFound(LookupError):
    pass


def adapter_class(port: str, name: str) -> type:
    group = f"dataplat.adapters.{port}"
    matches = [ep for ep in entry_points(group=group) if ep.name == name]
    if not matches:
        available = sorted(ep.name for ep in entry_points(group=group))
        raise AdapterNotFound(f"no adapter {name!r} for port {port!r}; available: {available}")
    return matches[0].load()


class Registry:
    """Builds and caches one adapter instance per port for a process."""

    def __init__(self, config: PlatformConfig, overrides: dict[str, Any] | None = None) -> None:
        self.config = config
        self._instances: dict[str, Any] = dict(overrides or {})

    def get(self, port: str) -> Any:
        if port not in self._instances:
            name = self.config.adapters.get(port)
            if name is None:
                raise AdapterNotFound(f"port {port!r} has no adapter configured")
            cls = adapter_class(port, name)
            self._instances[port] = cls.from_config(self.config, self)
        return self._instances[port]

    def close(self) -> None:
        for instance in self._instances.values():
            close = getattr(instance, "close", None)
            if callable(close):
                close()
        self._instances.clear()
