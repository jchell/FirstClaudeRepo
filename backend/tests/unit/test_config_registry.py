from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dataplat.core.config import default_config_path, load_config
from dataplat.core.logging import redact
from dataplat.core.registry import AdapterNotFound, Registry, adapter_class


def test_platform_yaml_loads_and_interpolates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_ADDR", "http://localhost:8200")
    cfg = load_config()
    assert cfg.vault.addr == "http://localhost:8200"
    assert cfg.postgres.host == "postgres"  # default when POSTGRES_HOST is unset


def test_every_configured_adapter_resolves() -> None:
    cfg = load_config()
    for port, name in cfg.adapters.items():
        assert adapter_class(port, name) is not None


def test_unknown_adapter_is_a_clear_error() -> None:
    with pytest.raises(AdapterNotFound, match="available"):
        adapter_class("object_store", "nope")


def test_swapping_adapter_is_config_only(tmp_path: Path) -> None:
    raw = yaml.safe_load(default_config_path().read_text())
    raw["adapters"]["object_store"] = "memory"
    raw["adapters"]["event_bus"] = "memory"
    p = tmp_path / "platform.yaml"
    p.write_text(yaml.safe_dump(raw))
    reg = Registry(load_config(p))
    assert type(reg.get("object_store")).__name__ == "InMemoryObjectStore"
    assert type(reg.get("event_bus")).__name__ == "InMemoryEventBus"


def test_config_rejects_inline_secrets(tmp_path: Path) -> None:
    raw = yaml.safe_load(default_config_path().read_text())
    raw["object_store"]["secret"] = "plaintext-password"
    p = tmp_path / "platform.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        load_config(p)


def test_platform_yaml_contains_no_secrets() -> None:
    text = default_config_path().read_text()
    assert redact(text) == text
