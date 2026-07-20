"""``coach`` command-line entry point.

Thin dispatch layer. Subcommands are added per phase; each delegates to a
handler so the CLI stays a boundary, not a place where logic accumulates.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

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


def _cmd_db_backup(settings: Settings, args: argparse.Namespace) -> int:
    from ..store.maintenance import backup_db

    if not settings.db_path.exists():
        print(f"Database: {settings.db_path} (does not exist — nothing to back up)")
        return 2
    conn = db.connect(settings.db_path)
    try:
        dest = backup_db(conn, settings.db_path, Path(args.to) if args.to else None)
    finally:
        conn.close()
    size_kb = dest.stat().st_size / 1024
    print(f"Backup written: {dest} ({size_kb:.0f} KiB)")
    return 0


def _cmd_db_verify(settings: Settings, _args: argparse.Namespace) -> int:
    from ..store.maintenance import verify_db

    if not settings.db_path.exists():
        print(f"Database: {settings.db_path} (does not exist)")
        return 2
    conn = db.connect(settings.db_path)
    try:
        report = verify_db(conn)
    finally:
        conn.close()
    print(f"integrity:       {report.integrity}")
    print(f"fk violations:   {report.fk_violations}")
    for table, n in report.row_counts.items():
        print(f"  {table:20} {'(missing)' if n < 0 else n}")
    fp = report.canonical_fingerprint
    print(f"canonical fingerprint: {fp[:16] + '…' if len(fp) == 64 else fp}")
    if not report.ok:
        print("PROBLEMS FOUND — restore from a backup")
        return 1
    if any(n < 0 for n in report.row_counts.values()):
        print("NOT INITIALIZED — run `coach db init`")
        return 1
    print("OK")
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


def _ensure_migrated(conn) -> None:
    """Apply pending migrations before a write-path command (idempotent).

    Keeps `coach sync`/`ingest` from crashing on a fresh or half-initialized
    DB file; read-only commands (doctor, db verify) diagnose instead of mutate.
    """
    applied = db.migrate(conn)
    if applied:
        print(f"  (applied {len(applied)} pending migration(s))")


def _whoop_client(settings: Settings) -> WhoopClient:
    oauth = WhoopOAuth(
        settings.whoop_client_id,
        settings.whoop_client_secret,
        settings.whoop_redirect_uri,
    )
    store = TokenStore(whoop_token_path())
    return WhoopClient(lambda: oauth.valid_access_token(store))


def _cmd_ingest_whoop(settings: Settings, args: argparse.Namespace) -> int:
    from ..adapters.whoop.ingest import auto_since

    try:
        settings.require_whoop()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    conn = db.connect(settings.db_path)
    try:
        _ensure_migrated(conn)
        since = args.since or auto_since(conn)
        if since is None:
            print(
                "No WHOOP data ingested yet — pass an explicit --since for the "
                "first backfill (e.g. --since 2025-10-01).",
                file=sys.stderr,
            )
            return 2
        if not args.since:
            print(f"  (incremental since {since})")
        client = _whoop_client(settings)
        result = ingest_whoop(
            conn, client, since=since, until=args.until, user_id=settings.user_id
        )
    except ReauthRequired as exc:
        print(f"WHOOP auth needed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    for rtype, counts in result.items():
        print(f"  {rtype:18} inserted={counts['inserted']:4d} skipped={counts['skipped']:4d}")
    return 0


def _cmd_ingest_healthkit(settings: Settings, args: argparse.Namespace) -> int:
    from pathlib import Path

    from ..adapters.healthkit.ingest import ingest_healthkit

    path = Path(args.file)
    if not path.exists():
        print(f"Export not found: {path}", file=sys.stderr)
        return 2
    conn = db.connect(settings.db_path)
    try:
        _ensure_migrated(conn)
        result = ingest_healthkit(conn, path, user_id=settings.user_id)
    finally:
        conn.close()
    print(f"  healthkit (body)   inserted={result['inserted']:4d} skipped={result['skipped']:4d}")
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


def _today(settings: Settings) -> str:
    """Today's day_key in the configured home timezone (never host-local, §2.6)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(settings.home_tz)).date().isoformat()


def _cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    from ..compute.daily import daily_status

    date = args.date or _today(settings)
    conn = db.connect(settings.db_path)
    try:
        if args.json:
            import json as _json

            from ..coach.tools import get_daily_status

            print(_json.dumps(get_daily_status(conn, date=date, user_id=settings.user_id)))
            return 0
        s = daily_status(conn, date, user_id=settings.user_id)
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

    end = args.end or _today(settings)
    args.end = end
    conn = db.connect(settings.db_path)
    try:
        if args.json:
            import json as _json

            from ..coach.tools import get_tdee_estimate

            print(
                _json.dumps(
                    get_tdee_estimate(conn, end=end, window=args.window, user_id=settings.user_id)
                )
            )
            return 0
        window = build_window(conn, end, args.window, settings.user_id)
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


# ---- doctor / sync ---------------------------------------------------------


def _cmd_doctor(settings: Settings, _args: argparse.Namespace) -> int:
    """Environment/config/data sanity in one shot. Prints no secret values."""
    problems = 0

    print("── coach doctor ──")
    print(f"  db path:        {settings.db_path}")
    if settings.db_path.exists():
        conn = db.connect(settings.db_path)
        try:
            # read-only diagnosis: don't let current_version() create the
            # schema_version table as a side effect on an unmigrated file
            has_schema = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
                ).fetchone()
                is not None
            )
            if has_schema:
                version = db.current_version(conn)
                pending = db.pending_migrations(conn)
            else:
                version = 0
                pending = db.discover_migrations()
            print(f"  schema:         v{version}" + (f"  (PENDING: {len(pending)})" if pending else "  (up to date)"))
            if pending:
                problems += 1
                print("                  -> run `coach db init`")
            else:
                for source in ("whoop_api", "healthkit"):
                    row = conn.execute(
                        "SELECT COUNT(*) AS n, MAX(ingested_at) AS last FROM raw_events WHERE source=?",
                        (source,),
                    ).fetchone()
                    last = row["last"] or "never"
                    print(f"  raw[{source:9}] {row['n']:6d} rows   last ingest: {last}")
        finally:
            conn.close()
    else:
        problems += 1
        print("  schema:         DB MISSING -> run `coach db init`")

    try:
        settings.require_whoop()
        print("  whoop creds:    configured")
        store = TokenStore(whoop_token_path())
        tokens = store.load() if store.exists() else None
        if tokens is None:
            problems += 1
            print("  whoop token:    MISSING -> run `coach auth whoop`")
        elif tokens.is_expired():
            print(f"  whoop token:    expired {tokens.expires_at.isoformat()} (auto-refresh on use)")
        else:
            print(f"  whoop token:    valid until {tokens.expires_at.isoformat()}")
    except ConfigError:
        problems += 1
        print("  whoop creds:    NOT CONFIGURED (WHOOP_CLIENT_ID/SECRET in .env)")

    export = Path("apple_health_export/export.xml")
    print(f"  hk export:      {'present' if export.exists() else 'not found (optional)'}")

    print("OK" if problems == 0 else f"{problems} problem(s) found")
    return 0 if problems == 0 else 1


def _cmd_sync(settings: Settings, args: argparse.Namespace) -> int:
    """One-shot: incremental WHOOP ingest + HealthKit (if export present) + normalize.

    Lowest-friction path to current data (risk #8: logging/sync friction kills
    the tool). Skips sources that aren't configured instead of failing.
    """
    from ..adapters.healthkit.ingest import ingest_healthkit
    from ..adapters.whoop.ingest import auto_since
    from ..normalize.runner import normalize_all as _normalize

    conn = db.connect(settings.db_path)
    try:
        _ensure_migrated(conn)
        # WHOOP (skip cleanly when unconfigured)
        try:
            settings.require_whoop()
            since = auto_since(conn)
            if since is None:
                print("  whoop: no prior ingest — run `coach ingest whoop --since <date>` once first")
            else:
                print(f"  whoop: incremental since {since}")
                result = ingest_whoop(
                    conn, _whoop_client(settings), since=since, user_id=settings.user_id
                )
                for rtype, c in result.items():
                    print(f"    {rtype:18} inserted={c['inserted']:4d} skipped={c['skipped']:4d}")
        except ConfigError:
            print("  whoop: not configured — skipped")
        except ReauthRequired as exc:
            print(f"  whoop: auth needed ({exc}) — skipped", file=sys.stderr)

        # HealthKit (only if an export file is present)
        export = Path(args.hk_file) if args.hk_file else Path("apple_health_export/export.xml")
        if export.exists():
            res = ingest_healthkit(conn, export, user_id=settings.user_id)
            print(f"  healthkit: inserted={res['inserted']} skipped={res['skipped']}")
        else:
            print("  healthkit: no export file — skipped")

        counts = _normalize(conn, user_id=settings.user_id)
        print("  normalize:", "  ".join(f"{k}={v}" for k, v in counts.items()))
    finally:
        conn.close()
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
    p_backup = db_sub.add_parser("backup", help="consistent online snapshot of the DB")
    p_backup.add_argument("--to", default=None, help="destination path (default: <db dir>/backups/)")
    p_backup.set_defaults(func=_cmd_db_backup)
    p_verify = db_sub.add_parser("verify", help="integrity check + row counts + fingerprint")
    p_verify.set_defaults(func=_cmd_db_verify)

    p_auth = sub.add_parser("auth", help="authorize a data source")
    auth_sub = p_auth.add_subparsers(dest="auth_command", required=True)
    p_whoop = auth_sub.add_parser("whoop", help="run the WHOOP OAuth login")
    p_whoop.set_defaults(func=_cmd_auth_whoop)

    p_ingest = sub.add_parser("ingest", help="fetch a source into raw_events")
    ingest_sub = p_ingest.add_subparsers(dest="ingest_command", required=True)
    p_iw = ingest_sub.add_parser("whoop", help="ingest WHOOP data (verbatim, idempotent)")
    p_iw.add_argument(
        "--since",
        default=None,
        help="ISO date/datetime start of window (default: incremental from last ingest)",
    )
    p_iw.add_argument("--until", default=None, help="ISO date/datetime end (optional)")
    p_iw.set_defaults(func=_cmd_ingest_whoop)

    p_ih = ingest_sub.add_parser(
        "healthkit", help="ingest Apple Health body/weight from an export (.xml/.zip)"
    )
    p_ih.add_argument("--file", required=True, help="path to export.xml or export.zip")
    p_ih.set_defaults(func=_cmd_ingest_healthkit)

    p_norm = sub.add_parser("normalize", help="derive canonical tables from raw")
    p_norm.add_argument("--rebuild", action="store_true", help="drop + re-derive all")
    p_norm.add_argument(
        "--tolerance", type=int, default=300, help="workout dedup window in seconds"
    )
    p_norm.set_defaults(func=_cmd_normalize)

    p_status = sub.add_parser("status", help="daily rollup for a date")
    p_status.add_argument(
        "--date", default=None, help="day_key YYYY-MM-DD (default: today in COACH_HOME_TZ)"
    )
    p_status.add_argument("--json", action="store_true", help="machine-readable output")
    p_status.set_defaults(func=_cmd_status)

    p_tdee = sub.add_parser("tdee", help="adaptive TDEE estimate over a window")
    p_tdee.add_argument(
        "--end", default=None, help="window end day_key (default: today in COACH_HOME_TZ)"
    )
    p_tdee.add_argument("--window", type=int, default=14, help="window length in days")
    p_tdee.add_argument("--json", action="store_true", help="machine-readable output")
    p_tdee.set_defaults(func=_cmd_tdee)

    p_doctor = sub.add_parser("doctor", help="config/db/token/data sanity report")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_sync = sub.add_parser(
        "sync", help="one-shot: incremental WHOOP + HealthKit (if present) + normalize"
    )
    p_sync.add_argument(
        "--hk-file", default=None, help="Apple Health export path (default: apple_health_export/export.xml)"
    )
    p_sync.set_defaults(func=_cmd_sync)

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
