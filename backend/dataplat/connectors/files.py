"""File-store connectors: local, SFTP, FTP, SMB and S3-compatible object stores.

All of them read through fsspec, so they share template matching, format parsing,
checksums and "new files only" handling; each subclass only builds its filesystem.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import io
import json
import posixpath
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import fsspec
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.json as pajson
import pyarrow.parquet as pq
from pydantic import BaseModel, Field

from dataplat.connectors.base import Batch, Connector, ConnectorError, FileRead, ReadContext, ReadRequest, SourceObject

# ---------------------------------------------------------------- file templates

_TOKEN = re.compile(r"\{([^{}]+)\}")
_OFFSET = re.compile(r"^(?P<fmt>.+?)(?:(?P<sign>[+-])(?P<n>\d+)(?P<unit>[mhdw]))?$")
_FMT_PARTS = [("yyyy", "%Y"), ("yy", "%y"), ("MM", "%m"), ("dd", "%d"), ("HH", "%H"), ("mm", "%M"), ("ss", "%S")]
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def render_template(template: str, run_date: datetime | None = None) -> str:
    """Expands date tokens: ``/in/orders_{yyyyMMdd}*.csv``, ``{yyyy}/{MM}``, ``{yyyyMMdd-1d}``.

    Glob characters (``*``, ``?``, ``[...]``) are left for matching.
    """
    now = run_date or datetime.now(UTC)

    def expand(m: re.Match[str]) -> str:
        parts = _OFFSET.match(m.group(1))
        assert parts is not None
        fmt, when = parts["fmt"], now
        if parts["n"]:
            delta = timedelta(**{_UNITS[parts["unit"]]: int(parts["n"])})
            when = now + delta if parts["sign"] == "+" else now - delta
        out, rest = "", fmt
        while rest:
            for token, code in _FMT_PARTS:
                if rest.startswith(token):
                    out += when.strftime(code)
                    rest = rest[len(token) :]
                    break
            else:
                if rest[0].isalpha():
                    raise ValueError(f"unknown date token in {{{fmt}}}")
                out += rest[0]
                rest = rest[1:]
        return out

    return _TOKEN.sub(expand, template)


# ---------------------------------------------------------------- parsing


def infer_format(path: str) -> str:
    lower = path.lower().removesuffix(".gz")
    for ext, fmt in (
        (".csv", "csv"),
        (".tsv", "csv"),
        (".jsonl", "jsonl"),
        (".ndjson", "jsonl"),
        (".json", "json"),
        (".parquet", "parquet"),
    ):
        if lower.endswith(ext):
            return fmt
    raise ConnectorError(f"cannot tell the format of {path!r}; set it explicitly")


def parse_file(data: bytes, fmt: str, options: dict[str, Any], path: str = "") -> pa.Table:
    if path.lower().endswith(".gz"):
        import gzip

        data = gzip.decompress(data)
    try:
        if fmt == "csv":
            delimiter = options.get("delimiter") or ("\t" if path.lower().endswith(".tsv") else ",")
            return pacsv.read_csv(
                io.BytesIO(data),
                read_options=pacsv.ReadOptions(
                    skip_rows=int(options.get("skip_rows", 0)),
                    autogenerate_column_names=not options.get("header", True),
                    encoding=options.get("encoding", "utf8"),
                ),
                parse_options=pacsv.ParseOptions(delimiter=delimiter, quote_char=options.get("quote_char", '"')),
                convert_options=pacsv.ConvertOptions(
                    null_values=options.get("null_values", ["", "NULL", "null"]), strings_can_be_null=True
                ),
            )
        if fmt == "jsonl":
            return pajson.read_json(io.BytesIO(data))
        if fmt == "json":
            doc = json.loads(data)
            records = doc.get(options["records_key"]) if isinstance(doc, dict) and options.get("records_key") else doc
            if isinstance(records, dict):
                records = [records]
            return pa.Table.from_pylist(normalize_records(records))
        if fmt == "parquet":
            return pq.read_table(io.BytesIO(data))
    except (pa.ArrowInvalid, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ConnectorError(f"cannot parse {path or 'file'} as {fmt}: {e}") from e
    raise ConnectorError(f"unsupported format {fmt!r}")


def normalize_records(records: list[Any]) -> list[dict[str, Any]]:
    """Keeps scalars; nested objects/arrays become JSON strings so schemas stay stable."""
    out = []
    for r in records:
        if not isinstance(r, dict):
            r = {"value": r}
        out.append(
            {
                str(k): json.dumps(v, default=str, sort_keys=True) if isinstance(v, dict | list) else v
                for k, v in r.items()
            }
        )
    return out


# ---------------------------------------------------------------- base


class FileConnector(Connector):
    category = "file"
    protocol: ClassVar[str]

    def __init__(self, config: dict[str, Any], secrets: Any = None) -> None:
        super().__init__(config, secrets)
        self._fs: fsspec.AbstractFileSystem | None = None

    # Subclasses: build the filesystem, and say where relative paths start.
    def build_fs(self) -> fsspec.AbstractFileSystem:
        raise NotImplementedError

    @property
    def root(self) -> str:
        return getattr(self.config, "base_path", "") or "/"

    @property
    def fs(self) -> fsspec.AbstractFileSystem:
        if self._fs is None:
            try:
                self._fs = self.build_fs()
            except ConnectorError:
                raise
            except Exception as e:
                raise ConnectorError(f"cannot connect: {type(e).__name__}: {e}") from e
        return self._fs

    def _abs(self, path: str) -> str:
        root = self.root.rstrip("/") or ""
        joined = posixpath.normpath(posixpath.join(root or "/", path.lstrip("/")))
        if root and not (joined == root or joined.startswith(root + "/")):
            raise ConnectorError(f"path {path!r} escapes the connection's base path")
        return joined

    def _rel(self, path: str) -> str:
        root = self.root.rstrip("/")
        path = "/" + path.lstrip("/")
        return path[len(root) :] if root and path.startswith(root + "/") else path

    def test(self) -> dict[str, Any]:
        try:
            entries = self.fs.ls(self._abs("/"), detail=False)
        except Exception as e:
            raise ConnectorError(f"cannot list {self.root}: {type(e).__name__}: {e}") from e
        return {"entries_in_base_path": len(entries)}

    def discover(self, pattern: str | None = None) -> list[SourceObject]:
        found = self.fs.find(self._abs("/"), detail=True)
        out = []
        for path, info in sorted(found.items()):
            rel = self._rel(path)
            if pattern and not fnmatch.fnmatch(rel, pattern):
                continue
            out.append(SourceObject(name=rel, kind="file", size=info.get("size"), modified=_mtime(info)))
        return out

    def match(self, template: str, run_date: datetime | None = None) -> list[str]:
        pattern = self._abs(render_template(template, run_date))
        paths = self.fs.glob(pattern) if any(c in pattern for c in "*?[") else [pattern]
        return sorted(p for p in paths if self.fs.isfile(p))

    def read(self, request: ReadRequest, ctx: ReadContext) -> Iterator[Batch]:
        if not request.path_template:
            raise ConnectorError("file sources need a path template")
        paths = self.match(request.path_template, request.run_date)
        for path in paths:
            info = self.fs.info(path)
            rel = self._rel(path)
            fingerprint = f"{info.get('size')}:{_mtime(info)}:{info.get('ETag') or info.get('etag') or ''}"
            if request.load_mode == "incremental" and request.seen_files.get(rel) == fingerprint:
                continue  # already ingested and unchanged
            with self.fs.open(path, "rb") as f:
                data = f.read()
            table = parse_file(data, request.format or infer_format(path), request.format_options, path)
            if request.max_records is not None:
                table = table.slice(0, request.max_records)
            ctx.files.append(
                FileRead(
                    path=rel,
                    size=len(data),
                    checksum=hashlib.sha256(data).hexdigest(),
                    fingerprint=fingerprint,
                    rows=table.num_rows,
                )
            )
            ctx.inputs.append(rel)
            ctx.rows += table.num_rows
            ctx.bytes += len(data)
            for batch in table.to_batches(max_chunksize=request.batch_size):
                yield Batch(batch, source=f"{ctx.namespace}{rel}", file=rel)
            if request.max_records is not None and ctx.rows >= request.max_records:
                return

    def close(self) -> None:
        close = getattr(self._fs, "close", None) if self._fs is not None else None
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _mtime(info: dict[str, Any]) -> datetime | None:
    for key in ("mtime", "LastModified", "last_modified", "modify", "created"):
        v = info.get(key)
        if v is None:
            continue
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=UTC)
        if isinstance(v, int | float):
            return datetime.fromtimestamp(v, UTC)
        if isinstance(v, str):
            for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%dT%H:%M:%S%z"):
                try:
                    return datetime.strptime(v, fmt).replace(tzinfo=UTC)
                except ValueError:
                    continue
    return None


# ---------------------------------------------------------------- implementations


class LocalFilesConfig(BaseModel):
    base_path: str = Field(description="Folder on the platform host (mounted into the workers)")


class LocalFilesConnector(FileConnector):
    type = "local_files"
    label = "Local folder"
    protocol = "file"
    Config = LocalFilesConfig

    def build_fs(self) -> fsspec.AbstractFileSystem:
        return fsspec.filesystem("file")

    def namespace(self) -> str:
        return "file://"


class SftpConfig(BaseModel):
    host: str
    port: int = 22
    username: str
    password: str | None = Field(default=None, description="vault:// reference")
    private_key: str | None = Field(default=None, description="vault:// reference to a PEM private key")
    base_path: str = "/"
    host_key_sha256: str | None = Field(
        default=None, description="Expected server host key fingerprint (SHA256:...); strongly recommended"
    )


class SftpConnector(FileConnector):
    type = "sftp"
    label = "SFTP"
    protocol = "sftp"
    Config = SftpConfig
    secret_fields = ("password", "private_key")

    def build_fs(self) -> fsspec.AbstractFileSystem:
        c = self.config
        kwargs: dict[str, Any] = {"port": c.port, "username": c.username, "timeout": 20}
        if pem := self.secret("private_key"):
            kwargs["pkey"] = _load_private_key(pem)
        if pw := self.secret("password"):
            kwargs["password"] = pw
        kwargs["look_for_keys"] = False
        kwargs["allow_agent"] = False
        fs = fsspec.filesystem("sftp", host=c.host, skip_instance_cache=True, **kwargs)
        if c.host_key_sha256:
            key = fs.client.get_transport().get_remote_server_key()
            actual = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
            if actual != c.host_key_sha256:
                fs.client.close()
                raise ConnectorError(f"SFTP host key mismatch: server presented {actual}")
        return fs

    def namespace(self) -> str:
        return f"sftp://{self.config.host}:{self.config.port}"


def _load_private_key(pem: str) -> Any:
    import paramiko

    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(pem))
        except paramiko.SSHException:
            continue
    raise ConnectorError("private key is not a supported (ed25519, ecdsa, rsa) PEM/OpenSSH key")


class FtpConfig(BaseModel):
    host: str
    port: int = 21
    username: str = "anonymous"
    password: str | None = Field(default=None, description="vault:// reference")
    tls: bool = Field(default=False, description="Explicit FTPS (AUTH TLS)")
    base_path: str = "/"


class FtpConnector(FileConnector):
    type = "ftp"
    label = "FTP / FTPS"
    protocol = "ftp"
    Config = FtpConfig
    secret_fields = ("password",)

    def build_fs(self) -> fsspec.AbstractFileSystem:
        c = self.config
        return fsspec.filesystem(
            "ftp",
            host=c.host,
            port=c.port,
            username=c.username,
            password=self.secret("password") or "",
            tls=c.tls,
            timeout=20,
            skip_instance_cache=True,
        )

    def namespace(self) -> str:
        return f"ftp://{self.config.host}:{self.config.port}"


class SmbConfig(BaseModel):
    host: str
    port: int = 445
    share: str
    username: str
    password: str | None = Field(default=None, description="vault:// reference")
    domain: str | None = None
    base_path: str = "/"


class SmbConnector(FileConnector):
    type = "smb"
    label = "Windows share (SMB)"
    protocol = "smb"
    Config = SmbConfig
    secret_fields = ("password",)

    @property
    def root(self) -> str:
        return posixpath.join(f"/{self.config.share}", self.config.base_path.lstrip("/")).rstrip("/") or "/"

    def build_fs(self) -> fsspec.AbstractFileSystem:
        c = self.config
        user = f"{c.domain}\\{c.username}" if c.domain else c.username
        return fsspec.filesystem(
            "smb",
            host=c.host,
            port=c.port,
            username=user,
            password=self.secret("password"),
            skip_instance_cache=True,
        )

    def namespace(self) -> str:
        return f"smb://{self.config.host}"


class S3Config(BaseModel):
    endpoint_url: str | None = Field(default=None, description="Leave empty for AWS S3")
    region: str = "us-east-1"
    bucket: str
    access_key_id: str = Field(description="vault:// reference")
    secret_access_key: str = Field(description="vault:// reference")
    base_path: str = "/"


class S3Connector(FileConnector):
    type = "s3"
    label = "S3 / MinIO bucket"
    protocol = "s3"
    Config = S3Config
    secret_fields = ("access_key_id", "secret_access_key")

    @property
    def root(self) -> str:
        return posixpath.join(f"/{self.config.bucket}", self.config.base_path.lstrip("/")).rstrip("/") or "/"

    def build_fs(self) -> fsspec.AbstractFileSystem:
        import s3fs

        c = self.config
        return s3fs.S3FileSystem(
            key=self.secret("access_key_id"),
            secret=self.secret("secret_access_key"),
            client_kwargs={"endpoint_url": c.endpoint_url, "region_name": c.region},
            use_listings_cache=False,
            skip_instance_cache=True,
        )

    def _abs(self, path: str) -> str:
        return super()._abs(path).lstrip("/")

    def namespace(self) -> str:
        return f"s3://{self.config.bucket}"
