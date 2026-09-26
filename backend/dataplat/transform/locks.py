"""Cross-process locks for table writers (Postgres advisory locks on the metadata DB).

Insert-only vault loads and model builds read their target before appending to it;
two writers doing that at once could insert the same key twice. Every writer of a
table takes that table's lock first, so they run one after another across workers.
"""

from __future__ import annotations

import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text

from dataplat.core.context import PlatformContext

_local_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _key(name: str) -> int:
    return zlib.crc32(f"dataplat:table:{name}".encode()) - 2**31


@contextmanager
def table_lock(ctx: PlatformContext, name: str) -> Iterator[None]:
    engine = getattr(ctx.metadata, "engine", None)
    if engine is None or engine.dialect.name != "postgresql":
        with _guard:
            lock = _local_locks.setdefault(name, threading.Lock())
        with lock:
            yield
        return
    with engine.connect() as conn:
        conn.execute(text("select pg_advisory_lock(:k)"), {"k": _key(name)})
        try:
            yield
        finally:
            conn.execute(text("select pg_advisory_unlock(:k)"), {"k": _key(name)})
            conn.commit()
