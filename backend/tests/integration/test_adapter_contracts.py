"""The shared contract suites against the real local adapters."""

from __future__ import annotations

import io
import subprocess
import time
import uuid

import hvac
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dataplat.adapters.duckdb_engine import DuckDBQueryEngine
from dataplat.adapters.jwt_identity import JwtIdentityProvider, VaultTransitSigner
from dataplat.adapters.minio_store import MinioObjectStore
from dataplat.adapters.oxigraph_store import OxigraphStore
from dataplat.adapters.redpanda_bus import RedpandaEventBus
from dataplat.adapters.vault_client import VaultClient
from dataplat.adapters.vault_secrets import VaultSecretStore
from dataplat.core.config import AuthConfig
from dataplat.core.ports.identity import Principal
from tests.contracts.suites import event_bus_contract, object_store_contract, secret_store_contract
from tests.integration.conftest import HOME

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def dev_vault():
    """A throwaway Vault dev server, so the contract suite never touches platform secrets."""
    name = f"dataplat-contract-vault-{uuid.uuid4().hex[:6]}"
    token = uuid.uuid4().hex
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            "127.0.0.1::8200",
            "-e",
            f"VAULT_DEV_ROOT_TOKEN_ID={token}",
            "hashicorp/vault:1.17",
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = subprocess.run(["docker", "port", name, "8200"], check=True, capture_output=True, text=True).stdout
        addr = "http://" + port.strip().splitlines()[0]
        client = hvac.Client(url=addr, token=token)
        for _ in range(60):
            try:
                if client.sys.is_initialized():
                    break
            except Exception:
                time.sleep(0.5)
        client.sys.enable_secrets_engine("transit", path="transit")
        client.secrets.transit.create_key(name="dataplat-jwt", key_type="ed25519")
        yield VaultClient(addr, token=token), client
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_vault_secret_store_contract(dev_vault) -> None:
    vc, _ = dev_vault
    secret_store_contract(VaultSecretStore(vc, mount="secret"))  # dev server mounts KV v2 at secret/


def test_vault_transit_jwt_roundtrip_and_rotation(dev_vault) -> None:
    vc, raw = dev_vault
    signer = VaultTransitSigner(vc, "dataplat-jwt")
    idp = JwtIdentityProvider(signer, AuthConfig())
    p = Principal(id="u", name="ann", kind="user", roles=frozenset({"admin"}))
    old, _ = idp.issue_access_token(p)
    assert idp.verify_access_token(old) == p
    raw.secrets.transit.rotate_key(name="dataplat-jwt")
    signer.refresh_keys()
    new, _ = idp.issue_access_token(p)
    assert '"kid":"dataplat-jwt-v2"' in __import__("base64").urlsafe_b64decode(new.split(".")[0] + "==").decode()
    # Tokens signed with the previous key version still verify until they expire.
    assert idp.verify_access_token(old) == p and idp.verify_access_token(new) == p


@pytest.fixture(scope="module")
def minio() -> MinioObjectStore:
    return MinioObjectStore(
        "http://127.0.0.1:9000",
        (HOME / "minio" / "root_user").read_text().strip(),
        (HOME / "minio" / "root_password").read_text().strip(),
    )


def test_minio_object_store_contract(minio) -> None:
    object_store_contract(minio)


def test_duckdb_reads_parquet_from_minio(minio) -> None:
    bucket = f"contract-duck-{uuid.uuid4().hex[:6]}"
    minio.ensure_bucket(bucket)
    buf = io.BytesIO()
    pq.write_table(pa.table({"id": [1, 2, 3], "name": ["a", "b", "c"]}), buf)
    minio.put_bytes(bucket, "t/part-0.parquet", buf.getvalue())
    engine = DuckDBQueryEngine(minio)
    t = engine.query(f"select count(*) as n, max(name) as m from read_parquet('s3://{bucket}/t/*.parquet')")
    assert t.to_pylist() == [{"n": 3, "m": "c"}]


def test_redpanda_event_bus_contract() -> None:
    event_bus_contract(RedpandaEventBus("127.0.0.1:19092"))


def test_oxigraph_knowledge_graph() -> None:
    kg = OxigraphStore("http://127.0.0.1:7878")
    g = f"urn:dataplat:contract:{uuid.uuid4().hex[:6]}"
    kg.load(b'<urn:c1> a <https://schema.org/Person> ; <https://schema.org/name> "Ann" .', graph=g)
    res = kg.query(f"SELECT ?n WHERE {{ GRAPH <{g}> {{ ?s <https://schema.org/name> ?n }} }}")
    assert [b["n"]["value"] for b in res["results"]["bindings"]] == ["Ann"]
    kg.update(f"DROP GRAPH <{g}>")
    assert kg.health()
