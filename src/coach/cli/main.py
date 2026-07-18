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
from ..adapters.whoop.client import WhoopClient
from ..adapters.whoop.ingest import ingest_whoop
from ..config import ConfigError, Settings, load_settings
from ..normalize.runner import normalize_all
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


# ---- ingest / normalize ----------------------------------------------------


def _whoop_client(settings: Settings) -> WhoopClient:
    oauth = WhoopOAuth(
        settings.whoop_client_id,
        settings.whoop_client_secret,
        settings.whoop_redirect_uri,
    )
    store = TokenStore(whoop_token_path())
    return WhoopClient(lambda: oauth.valid_access_token(store))


def _cmd_ingest_whoop(settings: Settings, args: argparse.Namespace) -> int:
    try:
        settings.require_whoop()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    conn = db.connect(settings.db_path)
    try:
        client = _whoop_client(settings)
        result = ingest_whoop(
            conn, client, since=args.since, until=args.until, user_id=settings.user_id
        )
    except ReauthRequired as exc:
        print(f"WHOOP auth needed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    for rtype, counts in result.items():
        print(f"  {rtype:18} inserted={counts['inserted']:4d} skipped={counts['skipped']:4d}")
    return 0


def _cmd_normalize(settings: Settings, args: argparse.Namespace) -> int:
    conn = db.connect(settings.db_path)
    try:
        counts = normalize_all(
            conn,
            user_id=settings.user_id,
            rebuild=args.rebuild,
            tolerance_s=args.tolerance,
        )
    finally:
        conn.close()
    mode = "rebuild" if args.rebuild else "incremental"
    print(f"Normalized ({mode}):")
    for k, v in counts.items():
        print(f"  {k:16} {v}")
    return 0


# ---- status ----------------------------------------------------------------


def _fmt(v: object, unit: str = "") -> str:
    return "—" if v is None else f"{v}{unit}"


def _cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    from ..compute.daily import daily_status

    conn = db.connect(settings.db_path)
    try:
        s = daily_status(conn, args.date, user_id=settings.user_id)
    finally:
        conn.close()

    print(f"── Daily status · {s.day_key} ──")
    if s.recovery:
        r = s.recovery
        print(
            f"  recovery [{r.source}]: score={_fmt(r.score)} "
            f"hrv={_fmt(r.hrv_rmssd_ms, 'ms')} rhr={_fmt(r.resting_hr_bpm, 'bpm')}"
        )
    else:
        print("  recovery: — (none)")
    if s.weight:
        w = s.weight
        print(f"  weight [{w.source}]: {_fmt(w.weight_kg, 'kg')} (trend {_fmt(w.trend_kg, 'kg')})")
    else:
        print("  weight: — (none)")
    f = s.food
    if not f.logged:
        print("  food: NOT LOGGED (not the same as zero)")
    elif f.is_fast:
        print("  food: FAST — 0 kcal (declared)")
    else:
        flag = "" if f.is_complete else "  [incomplete]"
        print(
            f"  food [{f.source}]: {_fmt(f.kcal, ' kcal')}  "
            f"P{_fmt(f.protein_g)} C{_fmt(f.carbs_g)} F{_fmt(f.fat_g)}{flag}"
        )
    t = s.training
    print(
        f"  training: {t.sessions} session(s) "
        f"kcal={_fmt(t.kcal_active)} dur={_fmt(t.duration_s, 's')} strain={_fmt(t.strain)}"
    )
    for n in s.notes:
        print(f"    · {n}")
    return 0


def _cmd_tdee(settings: Settings, args: argparse.Namespace) -> int:
    from ..compute.tdee import build_window, estimate_tdee
    from ..compute.trends import Insufficient

    conn = db.connect(settings.db_path)
    try:
        window = build_window(conn, args.end, args.window, settings.user_id)
    finally:
        conn.close()
    est = estimate_tdee(window)
    if isinstance(est, Insufficient):
        print(
            f"Insufficient data for TDEE: have {est.have} logged-intake day(s), "
            f"need {est.needed}. Log more consistently."
        )
        return 0
    print(f"── Adaptive TDEE · {args.window}d ending {args.end} ──")
    print(f"  TDEE estimate:   {est.tdee_kcal:.0f} kcal/day")
    print(f"  mean intake:     {est.mean_intake_kcal:.0f} kcal/day")
    print(f"  trend Δweight:   {est.trend_delta_kg:+.3f} kg over {est.span_days}d")
    print(f"  logged-intake days: {est.intake_days}")
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

    p_ingest = sub.add_parser("ingest", help="fetch a source into raw_events")
    ingest_sub = p_ingest.add_subparsers(dest="ingest_command", required=True)
    p_iw = ingest_sub.add_parser("whoop", help="ingest WHOOP data (verbatim, idempotent)")
    p_iw.add_argument("--since", required=True, help="ISO date/datetime start of window")
    p_iw.add_argument("--until", default=None, help="ISO date/datetime end (optional)")
    p_iw.set_defaults(func=_cmd_ingest_whoop)

    p_norm = sub.add_parser("normalize", help="derive canonical tables from raw")
    p_norm.add_argument("--rebuild", action="store_true", help="drop + re-derive all")
    p_norm.add_argument(
        "--tolerance", type=int, default=300, help="workout dedup window in seconds"
    )
    p_norm.set_defaults(func=_cmd_normalize)

    p_status = sub.add_parser("status", help="daily rollup for a date")
    p_status.add_argument("--date", required=True, help="day_key (YYYY-MM-DD)")
    p_status.set_defaults(func=_cmd_status)

    p_tdee = sub.add_parser("tdee", help="adaptive TDEE estimate over a window")
    p_tdee.add_argument("--end", required=True, help="window end day_key (YYYY-MM-DD)")
    p_tdee.add_argument("--window", type=int, default=14, help="window length in days")
    p_tdee.set_defaults(func=_cmd_tdee)

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
