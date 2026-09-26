"""`dataplat` command line: run services and one-off admin/bootstrap tasks."""

from __future__ import annotations

import argparse
import logging
import os
import sys


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "alembic"))
    command.upgrade(cfg, "head")


def _create_admin(username: str) -> None:
    from sqlalchemy import select

    from dataplat.core.context import PlatformContext
    from dataplat.db.models import User, UserRole
    from dataplat.security.passwords import generate_password, hash_password

    ctx = PlatformContext()
    password = generate_password()
    with ctx.metadata.session() as s:
        if s.scalars(select(User).where(User.username == username.lower())).first():
            raise SystemExit(f"user {username!r} already exists")
        user = User(username=username.lower(), display_name="Administrator", password_hash=hash_password(password))
        s.add(user)
        s.flush()
        s.add(UserRole(user_id=user.id, role="admin"))
    ctx.close()
    # Shown once, never stored in plain text anywhere.
    print(f"Created admin user {username!r}. Initial password (shown once, change it after first login):")
    print(password)


def main(argv: list[str] | None = None) -> None:
    from dataplat.core.logging import configure_logging

    parser = argparse.ArgumentParser(prog="dataplat")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("api")
    sub.add_parser("worker")
    sub.add_parser("scheduler")
    sub.add_parser("stream-worker")
    sub.add_parser("migrate")
    p = sub.add_parser("create-admin")
    p.add_argument("--username", default="admin")
    b = sub.add_parser("bootstrap")
    b.add_argument("step", choices=["prepare", "vault", "unseal"])
    b.add_argument("--stdin", action="store_true", help="read the vault init JSON from stdin")
    args = parser.parse_args(argv)

    if args.cmd == "api":
        from dataplat.api.app import main as run

        run()
    elif args.cmd == "worker":
        from dataplat.orchestration.worker import main as run

        run()
    elif args.cmd == "scheduler":
        from dataplat.orchestration.scheduler import main as run

        run()
    elif args.cmd == "stream-worker":
        from dataplat.orchestration.stream_worker import main as run

        run()
    elif args.cmd == "migrate":
        configure_logging(json_output=False)
        _migrate()
    elif args.cmd == "create-admin":
        configure_logging("WARNING", json_output=False)
        _create_admin(args.username)
    elif args.cmd == "bootstrap":
        configure_logging(json_output=False)
        logging.getLogger("dataplat").setLevel("INFO")
        from dataplat.bootstrap import vault_init

        if args.step == "prepare":
            vault_init.prepare()
        elif args.step == "vault":
            vault_init.run_vault(from_stdin=args.stdin)
        else:
            vault_init.run_unseal(from_stdin=args.stdin)


if __name__ == "__main__":
    main(sys.argv[1:])
