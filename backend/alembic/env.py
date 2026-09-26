from __future__ import annotations

from alembic import context

from dataplat.core.context import PlatformContext
from dataplat.db.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    ctx = PlatformContext()
    try:
        with ctx.metadata.engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        ctx.close()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported")
run_migrations_online()
