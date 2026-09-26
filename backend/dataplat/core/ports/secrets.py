from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from dataplat.core.secrets import SecretRef


@runtime_checkable
class SecretStore(Protocol):
    """Holds every secret on the platform.

    Local: HashiCorp Vault (KV v2 + database + transit engines).
    Cloud: HCP Vault, AWS Secrets Manager, Azure Key Vault, GCP Secret Manager.
    """

    def write(self, path: str, data: dict[str, str]) -> int:
        """Stores a new version of the secret at ``path``. Returns the version number."""
        ...

    def read(self, path: str) -> dict[str, str]: ...
    def resolve(self, ref: SecretRef | str) -> str:
        """Returns the single value a ``vault://`` reference points to."""
        ...

    def delete(self, path: str) -> None: ...
    def metadata(self, path: str) -> dict[str, Any]:
        """Version/timestamps only — never values."""
        ...

    def list(self, prefix: str) -> list[str]: ...
    def ref(self, path: str, key: str) -> SecretRef: ...
    def health(self) -> bool: ...


@runtime_checkable
class TokenSigner(Protocol):
    """Signs platform JWTs without the platform ever holding the private key.

    Local: Vault transit (ed25519). Cloud: AWS KMS, Azure Key Vault keys, GCP KMS.
    """

    algorithm: str

    def sign(self, message: bytes) -> tuple[bytes, str]:
        """Returns (signature, key id)."""
        ...

    def public_keys(self) -> dict[str, bytes]:
        """Key id -> raw public key, for local verification."""
        ...
