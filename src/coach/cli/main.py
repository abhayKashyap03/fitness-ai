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
    store = TokenStore(whoop_token_path(settings.user_id))
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
    # Wiring lives in the service layer so the web UI shares it (services.clients).
    from ..services.clients import whoop_client

    return whoop_client(settings)


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
        result = ingest_whoop(conn, client, since=since, until=args.until, user_id=settings.user_id)
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


def _mfp_client(settings: Settings):
    from ..services.clients import mfp_client

    return mfp_client(settings)


def _cmd_ingest_mfp(settings: Settings, args: argparse.Namespace) -> int:
    from datetime import date

    from ..adapters.myfitnesspal.auth import MfpAuthError
    from ..adapters.myfitnesspal.ingest import auto_since, ingest_mfp

    try:
        settings.require_mfp()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    conn = db.connect(settings.db_path)
    try:
        _ensure_migrated(conn)
        since = args.since or auto_since(conn)
        if since is None:
            print(
                "No MFP data ingested yet — pass an explicit --since for the "
                "first backfill (e.g. --since 2026-06-01).",
                file=sys.stderr,
            )
            return 2
        if not args.since:
            print(f"  (incremental since {since})")
        until = args.until or date.fromisoformat(_today(settings)).isoformat()
        client = _mfp_client(settings)
        result = ingest_mfp(conn, client, since=since, until=until, user_id=settings.user_id)
    except MfpAuthError as exc:
        print(f"MFP auth needed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    d, w = result["diary"], result["weight"]
    print(f"  myfitnesspal  days={result['days']:4d}")
    print(f"    diary   inserted={d['inserted']:4d} skipped={d['skipped']:4d}")
    print(f"    weight  inserted={w['inserted']:4d} skipped={w['skipped']:4d}")
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
    from ..compute.daily import daily_status, training_sessions

    date = args.date or _today(settings)
    conn = db.connect(settings.db_path)
    try:
        if args.json:
            import json as _json

            from ..coach.tools import get_daily_status

            print(_json.dumps(get_daily_status(conn, date=date, user_id=settings.user_id)))
            return 0
        s = daily_status(conn, date, user_id=settings.user_id)
        sessions = training_sessions(conn, date, user_id=settings.user_id)
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
    if s.sleep:
        sl = s.sleep
        print(
            f"  sleep [{sl.source}]: {_fmt(sl.in_bed_min, 'min')} in bed  "
            f"sws={_fmt(sl.sws_min, 'min')} rem={_fmt(sl.rem_min, 'min')} "
            f"eff={_fmt(sl.efficiency_pct, '%')}"
        )
    else:
        print("  sleep: — (none)")
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
    for sess in sessions:
        mins = f"{sess.duration_s / 60:.0f}min" if sess.duration_s else "—"
        label = sess.description or sess.sport_type
        print(
            f"    · {sess.sport_type:8} {mins:>7}  {_fmt(sess.kcal_active, ' kcal')}"
            f"  [{sess.source}] {label}"
        )
    _print_plan_line(settings, date)
    for n in s.notes:
        print(f"    · {n}")
    return 0


def _print_plan_line(settings: Settings, date: str) -> None:
    """Compact plan status on the daily view, only when a plan is set."""
    from ..coach.tools import get_plan_status

    conn = db.connect(settings.db_path)
    try:
        p = get_plan_status(conn, end=date, user_id=settings.user_id)
    finally:
        conn.close()
    if p["plan"] is None:
        return
    pl = p["plan"]
    if p["status"] is None:
        print(
            f"  plan [{pl['direction']} {pl['target_rate_pct_per_week']:+.2f}%/wk]: goal — (need TDEE + trend)"
        )
        return
    st = p["status"]
    goal = f" · goal {_fmt(st['goal_weight_kg'], 'kg')}" if st["goal_weight_kg"] is not None else ""
    eta = f" · ETA {st['projected_goal_day']}" if st["projected_goal_day"] else ""
    adh = f" · {st['adherence'].upper()}" if st["adherence"] is not None else ""
    clamp = "  [floor-clamped]" if st["floor_clamped"] else ""
    print(
        f"  plan [{st['direction']} {st['target_rate_pct_per_week']:+.2f}%/wk]: "
        f"{st['calorie_goal_kcal']:.0f} kcal/day{goal}{eta}{adh}{clamp}"
    )
    for a in st["alerts"]:
        print(f"    ⚠ {a['message']}")


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


# ---- ask / eval ------------------------------------------------------------


def _build_provider(settings: Settings):
    """Construct the configured LLM provider (raises ConfigError if unusable)."""
    from ..coach.llm import build_provider

    settings.require_llm()
    return build_provider(settings.llm_provider, settings.llm_api_key, model=settings.coach_model)


def _cmd_ask(settings: Settings, args: argparse.Namespace) -> int:
    from ..coach.agent import ask
    from ..coach.llm import ApiError

    try:
        provider = _build_provider(settings)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    conn = db.connect(settings.db_path)
    try:
        _ensure_migrated(conn)
        result = ask(
            conn, provider, args.question, user_id=settings.user_id, today=_today(settings)
        )
    except ApiError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if args.show_tools:
        for c in result.tool_calls:
            flag = "" if c.ok else "  [ERROR]"
            print(f"  → {c.name}({c.args}){flag}", file=sys.stderr)
    print(result.text)
    u = result.usage
    print(
        f"\n[{provider.name}/{provider.model} · {result.rounds} round(s) · "
        f"in={u.input_tokens} out={u.output_tokens} cached={u.cached_input_tokens}]",
        file=sys.stderr,
    )
    _record_spend(settings, provider, result, command="ask")
    return 0


def _record_spend(settings: Settings, provider: object, result: object, *, command: str) -> None:
    """Persist this call's spend (§8.7). Never fails the user's command.

    Accounting is a side effect of doing the work, not the work itself: if the
    ledger write breaks, the answer the user already received still stands. The
    failure is reported on stderr rather than swallowed, so a silently broken
    ledger can't masquerade as a zero-cost month.
    """
    from ..services.llm_usage import record_agent_result

    try:
        conn = db.connect(settings.db_path)
        try:
            record_agent_result(
                conn,
                provider,
                result,
                command=command,
                prices=settings.llm_prices,
                home_tz=settings.home_tz,
                user_id=settings.user_id,
            )
        finally:
            conn.close()
    except Exception as exc:  # accounting must never break the coach
        print(f"[warning] could not record token usage: {exc}", file=sys.stderr)


def _cmd_eval_grounding(settings: Settings, args: argparse.Namespace) -> int:
    """Live faithfulness eval (T4.2). Burns tokens — run deliberately, never in CI."""
    from ..coach.grounding import SCENARIOS, run_live_grounding, select_scenarios

    try:
        provider = _build_provider(settings)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    only = getattr(args, "only", None)
    limit = getattr(args, "limit", None)
    picked = select_scenarios(only=only, limit=limit)
    if not picked:
        print(f"No scenario name matches {only!r} — nothing to run.", file=sys.stderr)
        return 2
    # One live agent loop per scenario: say the size before spending it (§8.7).
    print(f"Running {len(picked)}/{len(SCENARIOS)} grounding scenarios (one model run each)...")
    results = run_live_grounding(provider, only=only, limit=limit)
    failed = 0
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        failed += int(not r["passed"])
        print(f"  [{status}] {r['scenario']}  tools={r['tool_calls']} rounds={r['rounds']}")
        if not r["passed"]:
            print(
                f"         admits_absence={r['admits_absence']} "
                f"fabricated={r['fabricated_numbers']} omitted={r['omitted_numbers']}"
            )
            print(f"         answer: {r['answer'][:300]}")
    print(f"{len(results) - failed}/{len(results)} scenarios passed (target: zero fabrications)")

    # One agent loop per scenario, so this is the project's largest single spend.
    # Recorded as ONE row for the whole run: the unit of spend a human decides
    # about is "should I run the eval", not "should I run scenario 34".
    from ..coach.llm.base import Usage

    total = Usage()
    for r in results:
        total = total + r.get("usage", Usage())
    print(
        f"[tokens: in={total.input_tokens} out={total.output_tokens} "
        f"cached={total.cached_input_tokens}]",
        file=sys.stderr,
    )
    _record_spend(
        settings,
        provider,
        type("_R", (), {"usage": total, "rounds": sum(r["rounds"] for r in results)})(),
        command="eval_grounding",
    )
    return 0 if failed == 0 else 1


def _fmt_agreement(name: str, got: object) -> str:
    """One metric's agreement line, or an honest insufficient marker."""
    from ..compute.calibration import Agreement
    from ..compute.trends import Insufficient

    if isinstance(got, Insufficient):
        return f"  {name:16} insufficient overlap (shared days {got.have}, need {got.needed})"
    assert isinstance(got, Agreement)
    return (
        f"  {name:16} n={got.n:4}  bias={got.mean_bias:+.3f}  "
        f"MAE={got.mae:.3f}  {_fmt_corr(got.correlation)}"
    )


def _cmd_eval_calibration(settings: Settings, args: argparse.Namespace) -> int:
    """Cross-source agreement (ADR-0012). Deterministic; no tokens, no network.

    Built for the post-membership question — does our recomputed WHOOP metric
    track the official one? — but deliberately runnable TODAY on weight, where
    two writers already overlap. The machinery should not be first exercised on
    the day the membership lapses.
    """
    from ..compute.calibration import (
        SourceSpec,
        calibration_report,
        weight_calibration_report,
    )

    conn = db.connect(settings.db_path)
    try:
        if args.domain in ("weight", "all"):
            a = SourceSpec.parse(args.a or "healthkit:okok")
            b = SourceSpec.parse(args.b or "myfitnesspal")
            print(f"── Weight calibration · {b} vs {a} (reference) ──")
            report = weight_calibration_report(conn, spec_a=a, spec_b=b, user_id=settings.user_id)
            for metric, got in report.items():
                print(_fmt_agreement(metric, got))
            print()
        if args.domain in ("recovery", "all"):
            ra = args.a or "whoop_api"
            rb = args.b or "whoop_ble"
            if args.domain == "all":
                ra, rb = "whoop_api", "whoop_ble"
            print(f"── Recovery calibration · {rb} vs {ra} (reference) ──")
            rec = calibration_report(conn, source_a=ra, source_b=rb, user_id=settings.user_id)
            for metric, got in rec.items():
                print(_fmt_agreement(metric, got))
            print(
                "  (whoop_ble rows appear once the BLE adapter lands — ADR-0012. "
                "Insufficient here is expected, not a fault.)"
            )
    finally:
        conn.close()
    return 0


def _fmt_corr(c: object) -> str:
    from ..compute.hrv_validation import Correlation
    from ..compute.trends import Insufficient

    if isinstance(c, Correlation):
        return f"r={c.r:+.3f} (n={c.n})"
    if isinstance(c, Insufficient):
        return f"insufficient data (have {c.have}, need {c.needed})"
    return str(c)


def _cmd_eval_hrv(settings: Settings, args: argparse.Namespace) -> int:
    """HRV-differentiator validation report (risk #6). Deterministic, no tokens."""
    from ..compute.hrv_validation import hrv_validation_report
    from ..compute.trends import Insufficient

    end = args.end or _today(settings)
    conn = db.connect(settings.db_path)
    try:
        rep = hrv_validation_report(conn, user_id=settings.user_id, end=end, window=args.window)
    finally:
        conn.close()

    cv = (
        f"{rep.hrv_cv:.1%}"
        if not isinstance(rep.hrv_cv, Insufficient)
        else f"insufficient data (have {rep.hrv_cv.have}, need {rep.hrv_cv.needed})"
    )
    print(f"HRV validation over the last {rep.window_days} days (ending {end})")
    print(f"  hrv days observed:        {rep.hrv_days}")
    print(f"  day-to-day noise (CV):    {cv}")
    print(f"  lag-1 autocorrelation:    {_fmt_corr(rep.hrv_lag1_autocorr)}")
    print(f"  dev% -> next-day score:   {_fmt_corr(rep.dev_vs_next_score)}")
    print(f"  dev% -> next-day strain:  {_fmt_corr(rep.dev_vs_next_strain)}")
    print(f"  dev% -> next-day train:   {_fmt_corr(rep.dev_vs_next_train_min)}")
    # data-driven verdict, not a static legend (§2.2: thresholds are code)
    label = {"signal": "SIGNAL", "noise": "NOISE", "insufficient": "INSUFFICIENT"}
    print(f"\n  verdict: {label.get(rep.verdict, rep.verdict.upper())} — {rep.rationale}")
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
            print(
                f"  schema:         v{version}"
                + (f"  (PENDING: {len(pending)})" if pending else "  (up to date)")
            )
            if pending:
                problems += 1
                print("                  -> run `coach db init`")
            else:
                for source in ("whoop_api", "healthkit", "myfitnesspal"):
                    row = conn.execute(
                        "SELECT COUNT(*) AS n, MAX(ingested_at) AS last FROM raw_events WHERE source=?",
                        (source,),
                    ).fetchone()
                    last = row["last"] or "never"
                    print(f"  raw[{source:12}] {row['n']:6d} rows   last ingest: {last}")
        finally:
            conn.close()
    else:
        problems += 1
        print("  schema:         DB MISSING -> run `coach db init`")

    try:
        settings.require_whoop()
        print("  whoop creds:    configured")
        store = TokenStore(whoop_token_path(settings.user_id))
        tokens = store.load() if store.exists() else None
        if tokens is None:
            problems += 1
            print("  whoop token:    MISSING -> run `coach auth whoop`")
        elif tokens.is_expired():
            print(
                f"  whoop token:    expired {tokens.expires_at.isoformat()} (auto-refresh on use)"
            )
        else:
            print(f"  whoop token:    valid until {tokens.expires_at.isoformat()}")
    except ConfigError:
        problems += 1
        print("  whoop creds:    NOT CONFIGURED (WHOOP_CLIENT_ID/SECRET in .env)")

    export = Path("apple_health_export/export.xml")
    print(f"  hk export:      {'present' if export.exists() else 'not found (optional)'}")

    # LLM provider (never print the key itself, §8.4)
    try:
        provider = _build_provider(settings)
        print(f"  coach llm:      {provider.name}/{provider.model} (key configured)")
    except ConfigError as exc:
        problems += 1
        print(f"  coach llm:      NOT CONFIGURED — {exc}")

    print("OK" if problems == 0 else f"{problems} problem(s) found")
    return 0 if problems == 0 else 1


def _cmd_sync(settings: Settings, args: argparse.Namespace) -> int:
    """One-shot daily driver: incremental WHOOP + MFP (food+weight) + normalize.

    Lowest-friction path to current data (risk #8: logging/sync friction kills
    the tool). Skips sources that aren't configured instead of failing.
    HealthKit is NOT part of the daily path anymore (MFP supplies daily food and
    weight); an Apple Health export is an occasional backfill, run only when
    ``--hk-file`` is passed explicitly.
    """
    from ..services.sync import run_sync

    conn = db.connect(settings.db_path)
    try:
        _ensure_migrated(conn)
        result = run_sync(
            conn,
            settings,
            whoop_client=_whoop_client,
            mfp_client=_mfp_client,
            today=_today(settings),
            hk_file=Path(args.hk_file) if args.hk_file else None,
        )
        for src in result.sources:
            if src.skipped:
                print(f"  {src.name}: {src.skipped} — skipped")
                continue
            since = f" (incremental since {src.since})" if src.since else ""
            print(f"  {src.name}:{since}")
            for key, c in src.counts.items():
                if isinstance(c, dict):  # nested per-record-type counts
                    print(f"    {key:18} inserted={c['inserted']:4d} skipped={c['skipped']:4d}")
                else:
                    print(f"    {key:18} {c}")
        print("  normalize:", "  ".join(f"{k}={v}" for k, v in result.normalized.items()))
    finally:
        conn.close()
    return 0


def _cmd_plan_set(settings: Settings, args: argparse.Namespace) -> int:
    """Set the active plan. All resolution/clamping lives in the service seam so
    the CLI and the web form cannot drift (services/plan.py)."""
    from ..services.plan import PlanInputError, set_active_plan

    today = _today(settings)
    conn = db.connect(settings.db_path)
    try:
        _ensure_migrated(conn)
        result = set_active_plan(
            conn,
            today=today,
            rate=args.rate,
            goal_weight=args.goal_weight,
            by=args.by,
            maintain=args.maintain,
            protein=args.protein,
            start_date=args.start_date,
            start_weight=args.start_weight,
            user_id=settings.user_id,
        )
        conn.commit()
    except PlanInputError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        conn.close()

    row = result.row
    anchor = (
        f" (start {row.start_weight_kg:.2f}kg @ {row.start_day_key})"
        if row.start_weight_kg is not None
        else ""
    )
    goal = f", goal {row.goal_weight_kg}kg" if row.goal_weight_kg is not None else ""
    print(f"Plan set: {row.direction} at {row.target_rate_pct_per_week:+.2f}%/week{anchor}{goal}")
    if result.clamped:
        print(f"  ⚠ {result.note}")
    print("Run `coach plan status` for the daily calorie goal.")
    return 0


def _print_protein(p: dict) -> None:
    """The plan's protein target line, if it has one.

    Reads ``p["protein"]``, which the tool reports ALONGSIDE status rather than
    inside it: a protein target needs only a g/kg figure and the trend weight, so
    it must not vanish while TDEE is still insufficient.
    """
    prot = p.get("protein")
    if prot is None:
        return
    line = f"  protein target:   {prot['target_g_per_day']:.0f} g/day"
    line += f" ({prot['g_per_kg']:.2f} g/kg of trend weight)"
    if prot["logged_g"] is None:
        # Not logged is not a missed target — say which one it is (§2.7).
        line += "  · today NOT LOGGED"
    else:
        mark = "met" if prot["met"] else "short"
        line += f"  · logged {prot['logged_g']:.0f} g ({prot['gap_g']:+.0f} g, {mark})"
    print(line)


def _cmd_plan_status(settings: Settings, args: argparse.Namespace) -> int:
    from ..coach.tools import get_plan_status

    end = args.end or _today(settings)
    conn = db.connect(settings.db_path)
    try:
        p = get_plan_status(conn, end=end, window=args.window, user_id=settings.user_id)
    finally:
        conn.close()

    if args.json:
        import json as _json

        print(_json.dumps(p))
        return 0

    if p["plan"] is None:
        print("No active plan. Set one with `coach plan set` (see --help).")
        return 0
    pl = p["plan"]
    print(f"── Plan · {pl['direction']} {pl['target_rate_pct_per_week']:+.2f}%/week ──")
    if pl["goal_weight_kg"] is not None:
        print(f"  goal weight:      {pl['goal_weight_kg']} kg")
    if pl["start_weight_kg"] is not None:
        print(f"  start weight:     {pl['start_weight_kg']:.2f} kg ({pl['start_day_key']})")
    if p["status"] is None:
        need = p["insufficient"]
        print(
            f"  daily goal:       — (insufficient data: need {need['needed']}, have {need['have']})"
        )
        # A protein target needs only the trend weight, so it survives here —
        # otherwise a target the user just set is invisible for the ten days it
        # takes TDEE to become measurable.
        _print_protein(p)
        return 0
    st = p["status"]
    print(f"  measured TDEE:    {st['tdee_kcal']:.0f} kcal/day")
    print(f"  current trend:    {st['current_trend_kg']:.2f} kg")
    print(f"  target rate:      {st['target_rate_kg_per_week']:+.3f} kg/week")
    print(
        f"  daily goal:       {st['calorie_goal_kcal']:.0f} kcal/day "
        f"(TDEE {st['effective_daily_kcal_delta']:+.0f})"
        + ("  [floor-clamped]" if st["floor_clamped"] else "")
    )
    _print_protein(p)
    if st["weeks_to_goal"] is not None:
        print(f"  projection:       ~{st['weeks_to_goal']:.1f} weeks → {st['projected_goal_day']}")
    if st["adherence"] is not None:
        print(
            f"  progress:         {st['kg_changed_so_far']:+.2f} kg over {st['elapsed_days']}d "
            f"({st['actual_rate_kg_per_week']:+.3f} kg/wk actual)  →  {st['adherence'].upper()}"
        )
    for a in st["alerts"]:
        print(f"  ⚠ {a['message']}")
    return 0


def _cmd_web(settings: Settings, args: argparse.Namespace) -> int:
    """Serve the local dashboard (ADR-0014).

    Binds localhost by default: this serves personal health data with no
    authentication, so exposing it on a network must be a deliberate act.
    """
    try:
        import uvicorn

        from ..web.app import create_app
    except ImportError as exc:
        print(
            f"The web UI needs the optional [web] extra ({exc.name} missing).\n"
            '  pip install -e ".[web]"',
            file=sys.stderr,
        )
        return 2

    conn = db.connect(settings.db_path)
    try:
        _ensure_migrated(conn)
    finally:
        conn.close()

    if args.host not in {"127.0.0.1", "localhost"}:
        print(
            f"WARNING: binding {args.host} exposes your health data on the network "
            "with NO authentication. Use only on a network you trust.",
            file=sys.stderr,
        )
    print(f"Dashboard: http://{args.host}:{args.port}  (Ctrl-C to stop)")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning")
    return 0


def _cmd_note_add(settings: Settings, args: argparse.Namespace) -> int:
    """Record a coaching decision or observation (ADR-0016)."""
    from ..store.notes import USER, add_note

    conn = db.connect(settings.db_path)
    try:
        _ensure_migrated(conn)
        row = add_note(
            conn,
            day_key=args.date or _today(settings),
            text=args.text,
            kind=args.kind,
            author=USER,
            user_id=settings.user_id,
        )
        conn.commit()
    except ValueError as exc:
        print(f"Cannot record that: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(f"Noted [{row.kind}] {row.day_key}: {row.text}")
    return 0


def _cmd_note_list(settings: Settings, args: argparse.Namespace) -> int:
    from ..coach.tools import get_coach_notes

    conn = db.connect(settings.db_path)
    try:
        out = get_coach_notes(conn, limit=args.limit, user_id=settings.user_id)
    finally:
        conn.close()

    if args.json:
        import json as _json

        print(_json.dumps(out))
        return 0
    if not out["notes"]:
        print("No coaching notes recorded yet.")
        return 0
    print(f"── Coaching memory · {out['count']} most recent ──")
    for n in out["notes"]:
        print(f"  {n['day_key']}  [{n['kind']}/{n['author']}]  {n['text']}")
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
    p_backup.add_argument(
        "--to", default=None, help="destination path (default: <db dir>/backups/)"
    )
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
        "healthkit", help="occasional backfill: Apple Health body/weight from an export (.xml/.zip)"
    )
    p_ih.add_argument("--file", required=True, help="path to export.xml or export.zip")
    p_ih.set_defaults(func=_cmd_ingest_healthkit)

    p_im = ingest_sub.add_parser(
        "mfp", help="ingest MyFitnessPal food diary via its v2 API (session cookie)"
    )
    p_im.add_argument(
        "--since",
        default=None,
        help="ISO date start of window (default: incremental from last ingest)",
    )
    p_im.add_argument(
        "--until", default=None, help="ISO date end of window (default: today in COACH_HOME_TZ)"
    )
    p_im.set_defaults(func=_cmd_ingest_mfp)

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

    p_ask = sub.add_parser("ask", help="ask the coach a question (grounded in your data)")
    p_ask.add_argument("question", help="the question, quoted")
    p_ask.add_argument(
        "--show-tools", action="store_true", help="print the tool calls made (to stderr)"
    )
    p_ask.set_defaults(func=_cmd_ask)

    p_eval = sub.add_parser("eval", help="model evaluation harnesses (burns tokens)")
    eval_sub = p_eval.add_subparsers(dest="eval_command", required=True)
    p_eg = eval_sub.add_parser("grounding", help="live zero-fabrication eval (T4.2)")
    p_eg.add_argument(
        "--only",
        help="run only scenarios whose name contains this substring (cost control, §8.7)",
    )
    p_eg.add_argument(
        "--limit", type=int, help="stop after this many scenarios (cost control, §8.7)"
    )
    p_eg.set_defaults(func=_cmd_eval_grounding)
    p_eh = eval_sub.add_parser(
        "hrv", help="HRV-differentiator validation report (deterministic, no tokens)"
    )
    p_eh.add_argument("--end", default=None, help="end day_key (default: today in COACH_HOME_TZ)")
    p_eh.add_argument("--window", type=int, default=90, help="lookback window in days (default 90)")
    p_eh.set_defaults(func=_cmd_eval_hrv)

    p_ecal = eval_sub.add_parser(
        "calibration", help="cross-source agreement: bias/MAE/correlation (ADR-0012)"
    )
    p_ecal.add_argument(
        "--domain",
        choices=("weight", "recovery", "all"),
        default="all",
        help="which metric family to compare (default: all)",
    )
    p_ecal.add_argument("--a", help="reference source, 'source[:source_app]'")
    p_ecal.add_argument("--b", help="source under test, 'source[:source_app]'")
    p_ecal.set_defaults(func=_cmd_eval_calibration)

    p_doctor = sub.add_parser("doctor", help="config/db/token/data sanity report")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_sync = sub.add_parser(
        "sync", help="one-shot daily driver: incremental WHOOP + MFP (food+weight) + normalize"
    )
    p_sync.add_argument(
        "--hk-file",
        default=None,
        help="ALSO ingest an Apple Health export (occasional weight backfill; not part of the daily path)",
    )
    p_sync.set_defaults(func=_cmd_sync)

    p_plan = sub.add_parser("plan", help="set / show the cut/bulk plan (ADR-0013)")
    plan_sub = p_plan.add_subparsers(dest="plan_command", required=True)
    p_ps = plan_sub.add_parser(
        "set", help="set the active plan (--rate | --goal-weight --by | --maintain)"
    )
    p_ps.add_argument(
        "--rate",
        type=float,
        default=None,
        help="signed %%/week target (e.g. -0.5 to cut, 0.25 to bulk)",
    )
    p_ps.add_argument("--goal-weight", type=float, default=None, help="goal weight in kg")
    p_ps.add_argument(
        "--by", default=None, help="deadline YYYY-MM-DD (with --goal-weight; rate is clamped)"
    )
    p_ps.add_argument("--protein", type=float, default=None, help="protein floor g/kg (optional)")
    p_ps.add_argument(
        "--start-date",
        default=None,
        help="backdate the plan start YYYY-MM-DD (already mid-cut); anchors progress",
    )
    p_ps.add_argument(
        "--start-weight",
        type=float,
        default=None,
        help="start weight in kg (defaults to the trend at --start-date, else today's trend)",
    )
    p_ps.add_argument("--maintain", action="store_true", help="maintenance plan (rate 0)")
    p_ps.set_defaults(func=_cmd_plan_set)
    p_pst = plan_sub.add_parser(
        "status", help="daily calorie goal + projection for the active plan"
    )
    p_pst.add_argument("--end", default=None, help="day_key YYYY-MM-DD (default: today)")
    p_pst.add_argument("--window", type=int, default=14, help="TDEE window in days")
    p_pst.add_argument("--json", action="store_true", help="machine-readable output")
    p_pst.set_defaults(func=_cmd_plan_status)

    p_note = sub.add_parser("note", help="coaching memory: past decisions and observations")
    note_sub = p_note.add_subparsers(dest="note_command", required=True)
    p_na = note_sub.add_parser("add", help="record a decision or observation")
    p_na.add_argument("text", help="the note, quoted")
    p_na.add_argument(
        "--kind",
        default="note",
        help="plan | advice | observation | note (default: note)",
    )
    p_na.add_argument("--date", default=None, help="day the note is about (default: today)")
    p_na.set_defaults(func=_cmd_note_add)
    p_nl = note_sub.add_parser("list", help="show recent notes")
    p_nl.add_argument("--limit", type=int, default=20, help="how many (default 20)")
    p_nl.add_argument("--json", action="store_true", help="machine-readable output")
    p_nl.set_defaults(func=_cmd_note_list)

    p_web = sub.add_parser("web", help="serve the local dashboard (needs the [web] extra)")
    p_web.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default localhost; anything else exposes health data un-authed)",
    )
    p_web.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    p_web.set_defaults(func=_cmd_web)

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
