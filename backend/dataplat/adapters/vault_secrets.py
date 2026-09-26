"""SecretStore on Vault KV v2."""

from __future__ import annotations

from typing import Any

from hvac.exceptions import InvalidPath

from dataplat.adapters.vault_client import VaultClient
from dataplat.core.config import PlatformConfig
from dataplat.core.secrets import SecretNotFound, SecretRef

__all__ = ["SecretNotFound", "VaultSecretStore", "shared_vault"]


def shared_vault(registry: Any) -> VaultClient:
    """One authenticated Vault client per process, shared by all Vault adapters."""
    inst = registry._instances
    if "_vault" not in inst:
        inst["_vault"] = VaultClient.from_config(registry.config.vault)
    return inst["_vault"]


class VaultSecretStore:
    def __init__(self, vault: VaultClient, mount: str = "kv", prefix: str = "dataplat") -> None:
        self.vault = vault
        self.mount = mount
        self.prefix = prefix.strip("/")

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> VaultSecretStore:
        return cls(shared_vault(registry), config.vault.kv_mount, config.vault.kv_prefix)

    def _full(self, path: str) -> str:
        path = path.strip("/")
        if ".." in path.split("/"):
            raise ValueError("path traversal in secret path")
        # Paths are always under the platform prefix, e.g. dataplat/connections/42.
        return path if path.startswith(self.prefix + "/") else f"{self.prefix}/{path}"

    def write(self, path: str, data: dict[str, str]) -> int:
        resp = self.vault.call(
            lambda c: c.secrets.kv.v2.create_or_update_secret(
                path=self._full(path), secret=data, mount_point=self.mount
            )
        )
        return int(resp["data"]["version"])

    def read(self, path: str) -> dict[str, str]:
        try:
            resp = self.vault.call(
                lambda c: c.secrets.kv.v2.read_secret_version(
                    path=self._full(path), mount_point=self.mount, raise_on_deleted_version=True
                )
            )
        except InvalidPath as e:
            raise SecretNotFound(path) from e
        return resp["data"]["data"]

    def resolve(self, ref: SecretRef | str) -> str:
        ref = SecretRef.parse(ref) if isinstance(ref, str) else ref
        if ref.mount != self.mount:
            raise ValueError(f"reference points at mount {ref.mount!r}, store serves {self.mount!r}")
        data = self.read(ref.path)
        if ref.key not in data:
            raise SecretNotFound(str(ref))
        return data[ref.key]

    def delete(self, path: str) -> None:
        self.vault.call(
            lambda c: c.secrets.kv.v2.delete_metadata_and_all_versions(path=self._full(path), mount_point=self.mount)
        )

    def metadata(self, path: str) -> dict[str, Any]:
        try:
            resp = self.vault.call(
                lambda c: c.secrets.kv.v2.read_secret_metadata(path=self._full(path), mount_point=self.mount)
            )
        except InvalidPath as e:
            raise SecretNotFound(path) from e
        d = resp["data"]
        return {
            "path": self._full(path),
            "current_version": d["current_version"],
            "created_time": d["created_time"],
            "updated_time": d["updated_time"],
            "versions": len(d.get("versions", {})),
        }

    def list(self, prefix: str) -> list[str]:
        try:
            resp = self.vault.call(
                lambda c: c.secrets.kv.v2.list_secrets(path=self._full(prefix), mount_point=self.mount)
            )
        except InvalidPath:
            return []
        return resp["data"]["keys"]

    def ref(self, path: str, key: str) -> SecretRef:
        return SecretRef(self.mount, self._full(path), key)

    def health(self) -> bool:
        try:
            status = self.vault.health()
            return isinstance(status, dict) and not status.get("sealed", True)
        except Exception:
            return False
