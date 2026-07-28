"""FastAPI app for the local dashboard (ADR-0014).

**This module is a presentation boundary, exactly like the CLI.** Every number it
shows comes from ``coach.coach.tools`` handlers — the same deterministic compute
the CLI and the LLM use (§2.2). There is no arithmetic here and there must never
be: if the UI needs a value that doesn't exist, it gets added to the compute
layer with tests, not computed in a route or a template.

Absence stays absence (§2.7): "not logged" and "insufficient data" are rendered
as themselves, never as a zero.

Serves personal health data with **no authentication**, so it binds to localhost
by default (see ``cli.main._cmd_web``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..coach import tools
from ..config import ConfigError, Settings, load_settings
from ..store import db
from .jobs import JobBusy, JobRunner

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _same_origin(request: Request) -> None:
    """Refuse cross-origin state changes.

    The server is unauthenticated by design, so any page in the user's browser
    could otherwise POST to it (classic CSRF against a localhost service). A
    browser always sends ``Origin`` on these requests; a missing header means a
    non-browser client (curl, tests), which is allowed.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return
    if urlparse(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(status_code=403, detail="cross-origin request refused")


def _today(settings: Settings) -> str:
    """Today's day_key in the configured home timezone (never host-local, §2.6)."""
    return datetime.now(ZoneInfo(settings.home_tz)).date().isoformat()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. ``settings`` is injectable so tests never touch a real DB."""
    cfg = settings or load_settings()
    app = FastAPI(
        title="Coach",
        description="Local dashboard over the deterministic compute layer (ADR-0014).",
        version="0.1.0",
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    runner = JobRunner()
    app.state.jobs = runner

    @contextmanager
    def _conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _day(value: str | None) -> str:
        return value or _today(cfg)

    # ---- JSON API ---------------------------------------------------------
    # The contract a future iOS app (P13) consumes. Each route is a thin pass
    # through to the tool handler of the same name — no reshaping, so the CLI,
    # the LLM and the UI all see identical numbers.

    @app.get("/api/status")
    def api_status(date: str | None = None) -> dict:
        with _conn() as conn:
            return tools.get_daily_status(conn, date=_day(date), user_id=cfg.user_id)

    @app.get("/api/tdee")
    def api_tdee(end: str | None = None, window: int = Query(14, ge=1)) -> dict:
        with _conn() as conn:
            return tools.get_tdee_estimate(conn, end=_day(end), window=window, user_id=cfg.user_id)

    @app.get("/api/weight-trend")
    def api_weight_trend(end: str | None = None, window: int = Query(30, ge=1)) -> dict:
        with _conn() as conn:
            return tools.get_weight_trend(conn, end=_day(end), window=window, user_id=cfg.user_id)

    @app.get("/api/recovery")
    def api_recovery(end: str | None = None, window: int = Query(14, ge=1)) -> dict:
        with _conn() as conn:
            return tools.get_recovery_history(
                conn, end=_day(end), window=window, user_id=cfg.user_id
            )

    @app.get("/api/sleep")
    def api_sleep(end: str | None = None, window: int = Query(14, ge=1)) -> dict:
        with _conn() as conn:
            return tools.get_sleep_history(conn, end=_day(end), window=window, user_id=cfg.user_id)

    @app.get("/api/plan")
    def api_plan(end: str | None = None, window: int = Query(14, ge=1)) -> dict:
        with _conn() as conn:
            return tools.get_plan_status(conn, end=_day(end), window=window, user_id=cfg.user_id)

    @app.get("/api/safety")
    def api_safety(end: str | None = None, window: int = Query(30, ge=1)) -> dict:
        with _conn() as conn:
            return tools.get_safety_flags(conn, end=_day(end), window=window, user_id=cfg.user_id)

    @app.post("/api/ask", dependencies=[Depends(_same_origin)])
    def api_ask(payload: dict) -> dict:
        """Ask the coach. Grounded in tool results exactly as the CLI is."""
        question = (payload.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=422, detail="question is required")

        from ..coach.agent import ask
        from ..coach.llm import ApiError, build_provider

        try:
            cfg.require_llm()
            provider = build_provider(cfg.llm_provider, cfg.llm_api_key, model=cfg.coach_model)
        except ConfigError as exc:
            # Configuration problem, not a user error — say so plainly.
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        with _conn() as conn:
            try:
                result = ask(conn, provider, question, user_id=cfg.user_id, today=_today(cfg))
            except ApiError as exc:
                raise HTTPException(status_code=502, detail=f"LLM API error: {exc}") from exc

        return {
            "answer": result.text,
            "tool_calls": [{"name": c.name, "args": c.args} for c in result.tool_calls],
        }

    # ---- pages ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def page_dashboard(request: Request, date: str | None = None) -> Any:
        day = _day(date)
        with _conn() as conn:
            status = tools.get_daily_status(conn, date=day, user_id=cfg.user_id)
            plan = tools.get_plan_status(conn, end=day, user_id=cfg.user_id)
            tdee = tools.get_tdee_estimate(conn, end=day, user_id=cfg.user_id)
            weight = tools.get_weight_trend(conn, end=day, window=30, user_id=cfg.user_id)
            safety = tools.get_safety_flags(conn, end=day, user_id=cfg.user_id)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "day": day,
                "prev_day": _shift(day, -1),
                "next_day": _shift(day, 1),
                "status": status,
                "plan": plan,
                "tdee": tdee,
                "weight": weight,
                "safety": safety,
            },
        )

    @app.get("/plan", response_class=HTMLResponse)
    def page_plan(request: Request) -> Any:
        day = _today(cfg)
        with _conn() as conn:
            plan = tools.get_plan_status(conn, end=day, user_id=cfg.user_id)
        return templates.TemplateResponse(
            request=request,
            name="plan.html",
            context={"day": day, "plan": plan, "error": None},
        )

    @app.post("/plan", response_class=HTMLResponse, dependencies=[Depends(_same_origin)])
    def submit_plan(
        request: Request,
        mode: str = Form(...),
        rate: float | None = Form(None),
        goal_weight: float | None = Form(None),
        by: str | None = Form(None),
        start_date: str | None = Form(None),
    ) -> Any:
        """Set the active plan. The §8.6 clamp is applied by compute, not here."""
        from datetime import UTC

        from ..compute.plan import rate_from_deadline, resolve_target_rate
        from ..store.plan import PlanRow, insert_plan, plan_id

        day = _today(cfg)
        error: str | None = None
        with _conn() as conn:
            trend_row = conn.execute(
                "SELECT trend_kg FROM weight_trend WHERE user_id = ? AND day_key <= ? "
                "AND trend_kg IS NOT NULL ORDER BY day_key DESC LIMIT 1",
                (cfg.user_id, day),
            ).fetchone()
            current_trend = trend_row["trend_kg"] if trend_row else None

            requested: float | None = None
            note_extra: str | None = None
            if mode == "maintain":
                requested = 0.0
            elif mode == "rate" and rate is not None:
                requested = rate
            elif mode == "deadline" and goal_weight is not None and by:
                if current_trend is None:
                    error = (
                        "Deadline entry needs a current weight trend, and there is none yet. "
                        "Log some weight first, or set a rate directly."
                    )
                else:
                    weeks = (date.fromisoformat(by) - date.fromisoformat(day)).days / 7
                    if weeks <= 0:
                        error = f"Deadline must be a future date (got {by})."
                    else:
                        requested = rate_from_deadline(
                            current_weight_kg=current_trend,
                            goal_weight_kg=goal_weight,
                            weeks=weeks,
                        )
                        note_extra = f"deadline entry {goal_weight}kg by {by}"
            else:
                error = "Specify a rate, a goal weight + deadline, or maintain."

            if error is None and requested is not None:
                target = resolve_target_rate(requested)
                note = target.note
                if note_extra:
                    note = f"{note_extra}; {note}" if note else note_extra

                start_day = start_date or day
                start_weight = current_trend
                if start_date:
                    srow = conn.execute(
                        "SELECT trend_kg FROM weight_trend WHERE user_id = ? AND day_key <= ? "
                        "AND trend_kg IS NOT NULL ORDER BY day_key DESC LIMIT 1",
                        (cfg.user_id, start_date),
                    ).fetchone()
                    start_weight = srow["trend_kg"] if srow else None

                created_at = datetime.now(UTC).isoformat()
                insert_plan(
                    conn,
                    PlanRow(
                        id=plan_id(cfg.user_id, created_at),
                        user_id=cfg.user_id,
                        created_at=created_at,
                        start_day_key=start_day,
                        direction=target.direction,
                        target_rate_pct_per_week=target.rate_pct_per_week,
                        start_weight_kg=start_weight,
                        goal_weight_kg=goal_weight,
                        note=note,
                    ),
                )
                conn.commit()

            if error is not None:
                plan = tools.get_plan_status(conn, end=day, user_id=cfg.user_id)
                return templates.TemplateResponse(
                    request=request,
                    name="plan.html",
                    context={"day": day, "plan": plan, "error": error},
                    status_code=400,
                )
        return RedirectResponse(url="/plan", status_code=303)

    @app.get("/coach", response_class=HTMLResponse)
    def page_coach(request: Request) -> Any:
        return templates.TemplateResponse(
            request=request, name="coach.html", context={"provider": cfg.llm_provider}
        )

    # ---- operations -------------------------------------------------------
    # Long-running work (sync, ingest, normalize, live eval) runs as a background
    # job so a request never blocks on the network. Instant, read-only checks
    # answer inline.

    def _run_in_job(fn) -> Any:
        """Wrap a body needing its own connection (jobs run on other threads)."""

        def body(emit) -> None:
            conn = db.connect(cfg.db_path)
            try:
                fn(conn, emit)
            finally:
                conn.close()

        return body

    def _start(name: str, fn) -> dict:
        try:
            job = runner.submit(name, _run_in_job(fn))
        except JobBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job.as_dict()

    @app.get("/api/jobs")
    def api_jobs() -> dict:
        return {"jobs": [j.as_dict() for j in runner.recent()]}

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str) -> dict:
        job = runner.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return job.as_dict()

    @app.post("/api/jobs/sync", dependencies=[Depends(_same_origin)])
    def api_job_sync() -> dict:
        from ..services.clients import mfp_client, whoop_client
        from ..services.sync import run_sync

        def fn(conn, emit) -> None:
            result = run_sync(
                conn,
                cfg,
                whoop_client=whoop_client,
                mfp_client=mfp_client,
                today=_today(cfg),
            )
            for src in result.sources:
                if src.skipped:
                    emit(f"{src.name}: {src.skipped} — skipped")
                    continue
                since = f" (incremental since {src.since})" if src.since else ""
                emit(f"{src.name}:{since}")
                for key, c in src.counts.items():
                    if isinstance(c, dict):
                        emit(f"  {key}: inserted={c['inserted']} skipped={c['skipped']}")
                    else:
                        emit(f"  {key}: {c}")
            emit("normalize: " + "  ".join(f"{k}={v}" for k, v in result.normalized.items()))

        return _start("sync", fn)

    @app.post("/api/jobs/ingest/{source}", dependencies=[Depends(_same_origin)])
    def api_job_ingest(source: str, payload: dict | None = None) -> dict:
        """Backfill a window from one source (the daily path is `sync`)."""
        payload = payload or {}
        since = (payload.get("since") or "").strip() or None
        until = (payload.get("until") or "").strip() or None
        if source not in {"whoop", "mfp"}:
            raise HTTPException(status_code=404, detail=f"unknown source {source!r}")

        def fn(conn, emit) -> None:
            if source == "whoop":
                from ..adapters.whoop.ingest import auto_since, ingest_whoop
                from ..services.clients import whoop_client

                cfg.require_whoop()
                start = since or auto_since(conn)
                if start is None:
                    raise RuntimeError("no --since given and nothing ingested yet to resume from")
                emit(f"whoop ingest since {start}" + (f" until {until}" if until else ""))
                counts = ingest_whoop(
                    conn, whoop_client(cfg), since=start, until=until, user_id=cfg.user_id
                )
                for rtype, c in counts.items():
                    emit(f"  {rtype}: inserted={c['inserted']} skipped={c['skipped']}")
            else:
                from ..adapters.myfitnesspal.ingest import ingest_mfp
                from ..services.clients import mfp_client

                cfg.require_mfp()
                start = since or _shift(_today(cfg), -7)
                end = until or _today(cfg)
                emit(f"mfp ingest {start} → {end}")
                result = ingest_mfp(
                    conn, mfp_client(cfg), since=start, until=end, user_id=cfg.user_id
                )
                emit(f"  days={result['days']}")
                emit(
                    f"  diary:  inserted={result['diary']['inserted']} skipped={result['diary']['skipped']}"
                )
                emit(
                    f"  weight: inserted={result['weight']['inserted']} skipped={result['weight']['skipped']}"
                )

        return _start(f"ingest-{source}", fn)

    @app.post("/api/jobs/normalize", dependencies=[Depends(_same_origin)])
    def api_job_normalize(payload: dict | None = None) -> dict:
        rebuild = bool((payload or {}).get("rebuild"))

        def fn(conn, emit) -> None:
            from ..normalize.runner import normalize_all

            emit("rebuild: re-deriving ALL canonical rows from raw" if rebuild else "incremental")
            counts = normalize_all(conn, user_id=cfg.user_id, rebuild=rebuild)
            for k, v in counts.items():
                emit(f"  {k}: {v}")

        return _start("normalize", fn)

    @app.post("/api/jobs/eval-grounding", dependencies=[Depends(_same_origin)])
    def api_job_eval_grounding() -> dict:
        """Live zero-fabrication eval. Burns API tokens (§8.7)."""

        def fn(conn, emit) -> None:
            from ..coach.grounding import run_live_grounding
            from ..coach.llm import build_provider

            cfg.require_llm()
            provider = build_provider(cfg.llm_provider, cfg.llm_api_key, model=cfg.coach_model)
            results = run_live_grounding(provider)
            failed = 0
            for r in results:
                bad = r["fabricated_numbers"]
                if bad:
                    failed += 1
                emit(f"[{'FAIL' if bad else 'PASS'}] {r['scenario']}  tools={r['tool_calls']}")
                if bad:
                    emit(f"    fabricated: {bad}")
            emit(f"{len(results) - failed}/{len(results)} passed (target: zero fabrications)")
            if failed:
                raise RuntimeError(f"{failed} scenario(s) fabricated numbers")

        return _start("eval-grounding", fn)

    # ---- instant checks ---------------------------------------------------

    @app.get("/api/doctor")
    def api_doctor() -> dict:
        from ..services.diagnostics import doctor_report

        rep = doctor_report(cfg)
        return {
            "db_path": rep.db_path,
            "ok": rep.ok,
            "problems": rep.problems,
            "checks": [c.__dict__ for c in rep.checks],
            "raw_counts": [c.__dict__ for c in rep.raw_counts],
        }

    @app.get("/api/db/verify")
    def api_db_verify() -> dict:
        from ..store.maintenance import verify_db

        with _conn() as conn:
            rep = verify_db(conn)
        return {
            "integrity": rep.integrity,
            "fk_violations": rep.fk_violations,
            "counts": rep.row_counts,
            "fingerprint": rep.canonical_fingerprint,
            "ok": rep.ok,
        }

    @app.post("/api/db/backup", dependencies=[Depends(_same_origin)])
    def api_db_backup() -> dict:
        from ..store.maintenance import backup_db

        with _conn() as conn:
            dest = backup_db(conn, cfg.db_path)
        return {"path": str(dest), "bytes": dest.stat().st_size}

    @app.get("/api/eval/hrv")
    def api_eval_hrv(end: str | None = None, window: int = Query(90, ge=1)) -> dict:
        """Deterministic — no tokens (risk #6 harness)."""
        from ..compute.hrv_validation import hrv_validation_report
        from ..compute.trends import Insufficient

        with _conn() as conn:
            rep = hrv_validation_report(conn, user_id=cfg.user_id, end=_day(end), window=window)

        def corr(c) -> dict | None:
            if isinstance(c, Insufficient):
                return {"insufficient": {"have": c.have, "needed": c.needed}}
            return {"r": round(c.r, 4), "n": c.n}

        cv = rep.hrv_cv
        return {
            "window_days": rep.window_days,
            "hrv_days": rep.hrv_days,
            "cv": None if isinstance(cv, Insufficient) else round(cv, 4),
            "lag1_autocorr": corr(rep.hrv_lag1_autocorr),
            "dev_vs_next_score": corr(rep.dev_vs_next_score),
            "dev_vs_next_strain": corr(rep.dev_vs_next_strain),
            "dev_vs_next_train_min": corr(rep.dev_vs_next_train_min),
            "verdict": rep.verdict,
            "rationale": rep.rationale,
        }

    @app.get("/doctor", response_class=HTMLResponse)
    def page_doctor(request: Request) -> Any:
        from ..services.diagnostics import doctor_report

        return templates.TemplateResponse(
            request=request, name="doctor.html", context={"rep": doctor_report(cfg)}
        )

    @app.get("/ops", response_class=HTMLResponse)
    def page_ops(request: Request) -> Any:
        return templates.TemplateResponse(
            request=request, name="ops.html", context={"today": _today(cfg)}
        )

    return app


def _shift(day_key: str, days: int) -> str:
    from datetime import timedelta

    return (date.fromisoformat(day_key) + timedelta(days=days)).isoformat()
