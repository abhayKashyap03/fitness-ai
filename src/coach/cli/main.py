"""``coach`` command-line entry point.

Thin dispatch layer. Subcommands are added per phase; each delegates to a
handler so the CLI stays a boundary, not a place where logic accumulates.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from ..adapters.whoop.auth import ReauthRequired, TokenStore, WhoopOAuth
from ..config import ConfigError, Settings, load_settings
from ..paths import whoop_token_path
from ..store import db


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ---- db subcommands --------------------------------------------------------


def _cmd_db_init(settings: Settings, _args: argparse.Namespace) -> int:
    conn = db.connect(settings.db_path)
    try:
        before = db.current_version(conn)
        applied = db.migrate(conn)
        after = db.current_version(conn)
    finally:
        conn.close()
    if applied:
        names = ", ".join(m.name for m in applied)
        print(f"Applied {len(applied)} migration(s): {names}")
    else:
        print("No pending migrations.")
    print(f"Database: {settings.db_path}")
    print(f"Schema version: {before} -> {after}")
    return 0


def _cmd_db_status(settings: Settings, _args: argparse.Namespace) -> int:
    if not settings.db_path.exists():
        print(f"Database: {settings.db_path} (does not exist — run `coach db init`)")
        return 0
    conn = db.connect(settings.db_path)
    try:
        version = db.current_version(conn)
        pending = db.pending_migrations(conn)
    finally:
        conn.close()
    print(f"Database: {settings.db_path}")
    print(f"Schema version: {version}")
    if pending:
        print(f"Pending migrations: {', '.join(m.name for m in pending)}")
    else:
        print("Pending migrations: none (up to date)")
    return 0


# ---- auth subcommands ------------------------------------------------------


def _cmd_auth_whoop(settings: Settings, _args: argparse.Namespace) -> int:
    from ..adapters.whoop.flow import run_login  # local: touches browser/socket

    try:
        settings.require_whoop()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    oauth = WhoopOAuth(
        settings.whoop_client_id,
        settings.whoop_client_secret,
        settings.whoop_redirect_uri,
    )
    store = TokenStore(whoop_token_path())
    try:
        tokens = run_login(oauth, store, settings.whoop_redirect_uri)
    except (ReauthRequired, RuntimeError) as exc:
        print(f"WHOOP authorization failed: {exc}", file=sys.stderr)
        return 1
    # never print the token itself
    print("WHOOP authorized. Token stored at", store.path)
    print(f"  scopes: {tokens.scope or '(none reported)'}")
    print(f"  expires: {tokens.expires_at.isoformat()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coach", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_db = sub.add_parser("db", help="database bootstrap & status")
    db_sub = p_db.add_subparsers(dest="db_command", required=True)
    p_init = db_sub.add_parser("init", help="create/upgrade the DB from migrations")
    p_init.set_defaults(func=_cmd_db_init)
    p_status = db_sub.add_parser("status", help="report current schema version")
    p_status.set_defaults(func=_cmd_db_status)

    p_auth = sub.add_parser("auth", help="authorize a data source")
    auth_sub = p_auth.add_subparsers(dest="auth_command", required=True)
    p_whoop = auth_sub.add_parser("whoop", help="run the WHOOP OAuth login")
    p_whoop.set_defaults(func=_cmd_auth_whoop)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    _configure_logging(settings.log_level)
    return int(args.func(settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
