"""ObjectStore on MinIO (S3 API) via fsspec/s3fs. Any S3-compatible endpoint works."""

from __future__ import annotations

from typing import Any

import s3fs

from dataplat.core.config import PlatformConfig
from dataplat.core.ports.secrets import SecretStore


class MinioObjectStore:
    def __init__(self, endpoint: str, access_key: str, secret_key: str) -> None:
        self.endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
        self.fs = s3fs.S3FileSystem(
            key=access_key,
            secret=secret_key,
            client_kwargs={"endpoint_url": endpoint, "region_name": "us-east-1"},
            use_listings_cache=False,
        )

    @classmethod
    def from_config(cls, config: PlatformConfig, registry: Any) -> MinioObjectStore:
        secrets: SecretStore = registry.get("secret_store")
        oc = config.object_store
        return cls(oc.endpoint, secrets.resolve(oc.credentials), secrets.resolve(oc.secret))

    def ensure_bucket(self, bucket: str) -> None:
        if not self.fs.exists(bucket):
            self.fs.mkdir(bucket)

    def put_bytes(self, bucket: str, key: str, data: bytes) -> None:
        self.fs.pipe_file(f"{bucket}/{key}", data)

    def get_bytes(self, bucket: str, key: str) -> bytes:
        return self.fs.cat_file(f"{bucket}/{key}")

    def exists(self, bucket: str, key: str) -> bool:
        return self.fs.exists(f"{bucket}/{key}")

    def list(self, bucket: str, prefix: str = "") -> list[str]:
        base = f"{bucket}/"
        paths = self.fs.find(f"{bucket}/{prefix}") if prefix else self.fs.find(bucket)
        return sorted(p[len(base) :] for p in paths)

    def delete(self, bucket: str, key: str) -> None:
        self.fs.rm(f"{bucket}/{key}", recursive=True)

    def uri(self, bucket: str, key: str = "") -> str:
        return f"s3://{bucket}/{key}"

    def storage_options(self) -> dict[str, str]:
        return {
            "AWS_ENDPOINT_URL": self.endpoint,
            "AWS_ACCESS_KEY_ID": self._access_key,
            "AWS_SECRET_ACCESS_KEY": self._secret_key,
            "AWS_REGION": "us-east-1",
            "AWS_ALLOW_HTTP": "true" if self.endpoint.startswith("http://") else "false",
            "AWS_S3_ALLOW_UNSAFE_RENAME": "true",
        }

    def health(self) -> bool:
        try:
            self.fs.ls("")
            return True
        except Exception:
            return False

    def __repr__(self) -> str:  # keep credentials out of reprs/logs
        return f"MinioObjectStore(endpoint={self.endpoint!r})"
