"""FastAPI app for the local dashboard (ADR-0014).

**This module is a presentation boundary, exactly like the CLI.** Every number it
shows comes from ``coach.coach.tools`` handlers — the same deterministic compute
the CLI and the LLM use (§2.2). There is no arithmetic here and there must never
be: if the UI needs a value that doesn't exist, it gets added to the compute
layer with tests, not computed in a route or a template.

Absence stays absence (§2.7): "not logged" and "insufficient data" are rendered
as themselves, never as a zero.

Requests are authenticated per user (ADR-0018). The one exception is the
single-user laptop case — loopback bind with no account yet claimed — which keeps
the original workflow working. Binding a non-loopback address with no claimed
account is a startup error, not a warning (see :mod:`coach.web.auth`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..coach import tools
from ..config import ConfigError, Settings, load_settings
from ..store import db
from ..store import users as U
from .auth import (
    AuthPolicy,
    clear_session_cookie,
    current_user,
    request_is_secure,
    resolve_policy,
    set_session_cookie,
)
from .jobs import JobBusy, JobRunner

TEMPLATES_DIR = Path(__file__).parent / "templates"

# The authenticated user for the request in flight.
#
# Carried in a ContextVar rather than threaded through every route signature:
# there are two dozen handlers and none of them are about authentication. The
# middleware below sets it once per request; Starlette copies the context into
# the worker thread that runs a sync endpoint, so this is safe for both.
#
# The default is deliberately absent rather than 1 — a route that somehow ran
# without the middleware must fail loudly, not quietly serve the owner's data.
_REQUEST_USER: ContextVar[U.User | None] = ContextVar("coach_request_user", default=None)


def _me() -> U.User:
    """The current request's user. Raises if authentication never ran."""
    user = _REQUEST_USER.get()
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def _uid() -> int:
    """The current request's user id."""
    return _me().id


# Paths reachable without a session: the login form itself, invite redemption,
# and the health check. Everything else is closed by default, so a new route is
# protected the moment it is added rather than the moment someone remembers.
_PUBLIC_PREFIXES = ("/login", "/invite", "/healthz", "/static")


def _same_origin(request: Request) -> None:
    """Refuse cross-origin state changes.

    A session cookie rides on every state change, so any page in the user's
    browser could otherwise POST to it (classic CSRF). A
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


def create_app(settings: Settings | None = None, *, bind_host: str = "127.0.0.1") -> FastAPI:
    """Build the app. ``settings`` is injectable so tests never touch a real DB.

    ``bind_host`` decides the authentication posture (see :mod:`coach.web.auth`):
    on loopback with no account claimed the existing single-user workflow keeps
    working; on any other address an unclaimed owner is a startup error, because
    serving health data to a network with no credentials must be impossible
    rather than merely discouraged.
    """
    cfg = settings or load_settings()
    app = FastAPI(
        title="Coach",
        description="Local dashboard over the deterministic compute layer (ADR-0014).",
        version="0.1.0",
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    runner = JobRunner()
    app.state.jobs = runner

    _boot = db.connect(cfg.db_path)
    try:
        db.migrate(_boot)
        policy: AuthPolicy = resolve_policy(_boot, bind_host)
    finally:
        _boot.close()
    app.state.auth_policy = policy

    @contextmanager
    def _conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(cfg.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _day(value: str | None) -> str:
        return value or _today(cfg)

    @app.middleware("http")
    async def _authenticate(request: Request, call_next):
        """Resolve the caller once per request; refuse everything unrecognised.

        Closed by default: a route added tomorrow is protected without anyone
        remembering to protect it. Only the prefixes in ``_PUBLIC_PREFIXES`` are
        reachable without a session.
        """
        path = request.url.path
        if path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)

        with _conn() as conn:
            user = current_user(conn, request, policy)

        if user is None:
            # A JSON caller cannot follow a redirect usefully; a browser cannot
            # do anything with a 401 body. Answer each in its own language.
            if path.startswith("/api/"):
                return JSONResponse({"detail": "authentication required"}, status_code=401)
            return RedirectResponse("/login", status_code=303)

        token = _REQUEST_USER.set(user)
        request.state.user = user  # templates read this for the nav / sign-out
        try:
            return await call_next(request)
        finally:
            _REQUEST_USER.reset(token)

    # ---- authentication ---------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def page_login(request: Request) -> Any:
        with _conn() as conn:
            if current_user(conn, request, policy) is not None:
                return RedirectResponse("/", status_code=303)  # already signed in
        return templates.TemplateResponse(
            request=request, name="login.html", context={"error": None}
        )

    @app.post("/login", response_class=HTMLResponse, dependencies=[Depends(_same_origin)])
    def post_login(request: Request, email: str = Form(...), password: str = Form(...)) -> Any:
        with _conn() as conn:
            try:
                _user, token = U.login(
                    conn,
                    email=email,
                    password=password,
                    user_agent=request.headers.get("user-agent"),
                )
                conn.commit()
            except ValueError as exc:
                # One generic message for every failure mode, so the form can't
                # be used to discover which addresses have accounts here.
                return templates.TemplateResponse(
                    request=request,
                    name="login.html",
                    context={"error": str(exc)},
                    status_code=401,
                )
        response = RedirectResponse("/", status_code=303)
        set_session_cookie(response, token, secure=request_is_secure(request))
        return response

    @app.post("/logout", dependencies=[Depends(_same_origin)])
    def post_logout(request: Request) -> Any:
        token = request.cookies.get("coach_session")
        if token:
            with _conn() as conn:
                U.revoke_session(conn, token)
                conn.commit()
        response = RedirectResponse("/login", status_code=303)
        clear_session_cookie(response)
        return response

    @app.get("/invite/{token}", response_class=HTMLResponse)
    def page_invite(request: Request, token: str) -> Any:
        """The invite form. Validity is checked on POST, not here.

        Rendering the form for any token on purpose: reporting "no such invite"
        at this point would let someone probe which invite links are live.
        """
        return templates.TemplateResponse(
            request=request,
            name="invite.html",
            context={"token": token, "error": None, "min_password_len": U.MIN_PASSWORD_LEN},
        )

    @app.post("/invite/{token}", response_class=HTMLResponse, dependencies=[Depends(_same_origin)])
    def post_invite(request: Request, token: str, password: str = Form(...)) -> Any:
        with _conn() as conn:
            try:
                user = U.accept_invite(conn, token=token, password=password)
                session = U.open_session(
                    conn, user_id=user.id, user_agent=request.headers.get("user-agent")
                )
                conn.commit()
            except ValueError as exc:
                return templates.TemplateResponse(
                    request=request,
                    name="invite.html",
                    context={
                        "token": token,
                        "error": str(exc),
                        "min_password_len": U.MIN_PASSWORD_LEN,
                    },
                    status_code=400,
                )
        response = RedirectResponse("/", status_code=303)
        set_session_cookie(response, session, secure=request_is_secure(request))
        return response

    @app.get("/healthz")
    def healthz() -> dict:
        """Liveness only. Deliberately says nothing about users or data."""
        return {"ok": True}

    # ---- JSON API ---------------------------------------------------------
    # The contract a future iOS app (P13) consumes. Each route is a thin pass
    # through to the tool handler of the same name — no reshaping, so the CLI,
    # the LLM and the UI all see identical numbers.

    @app.get("/api/status")
    def api_status(date: str | None = None) -> dict:
        with _conn() as conn:
            return tools.get_daily_status(conn, date=_day(date), user_id=_uid())

    @app.get("/api/tdee")
    def api_tdee(end: str | None = None, window: int = Query(14, ge=1)) -> dict:
        with _conn() as conn:
            return tools.get_tdee_estimate(conn, end=_day(end), window=window, user_id=_uid())

    @app.get("/api/weight-trend")
    def api_weight_trend(end: str | None = None, window: int = Query(30, ge=1)) -> dict:
        with _conn() as conn:
            return tools.get_weight_trend(conn, end=_day(end), window=window, user_id=_uid())

    @app.get("/api/recovery")
    def api_recovery(end: str | None = None, window: int = Query(14, ge=1)) -> dict:
        with _conn() as conn:
            return tools.get_recovery_history(conn, end=_day(end), window=window, user_id=_uid())

    @app.get("/api/sleep")
    def api_sleep(end: str | None = None, window: int = Query(14, ge=1)) -> dict:
        with _conn() as conn:
            return tools.get_sleep_history(conn, end=_day(end), window=window, user_id=_uid())

    @app.get("/api/plan")
    def api_plan(end: str | None = None, window: int = Query(14, ge=1)) -> dict:
        with _conn() as conn:
            return tools.get_plan_status(conn, end=_day(end), window=window, user_id=_uid())

    @app.get("/api/training")
    def api_training(date: str | None = None) -> dict:
        with _conn() as conn:
            return tools.get_training_sessions(conn, date=_day(date), user_id=_uid())

    @app.get("/api/safety")
    def api_safety(end: str | None = None, window: int = Query(30, ge=1)) -> dict:
        with _conn() as conn:
            return tools.get_safety_flags(conn, end=_day(end), window=window, user_id=_uid())

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

        from ..services.llm_usage import record_agent_result

        with _conn() as conn:
            try:
                result = ask(conn, provider, question, user_id=_uid(), today=_today(cfg))
            except ApiError as exc:
                raise HTTPException(status_code=502, detail=f"LLM API error: {exc}") from exc
            # Same ledger the CLI writes, via the same seam (§8.7). A browser
            # question costs exactly as much as a terminal one, so spend that
            # only counted from the CLI would understate the month.
            record_agent_result(
                conn,
                provider,
                result,
                command="web_ask",
                prices=cfg.llm_prices,
                home_tz=cfg.home_tz,
                user_id=_uid(),
            )

        return {
            "answer": result.text,
            "tool_calls": [{"name": c.name, "args": c.args} for c in result.tool_calls],
        }

    @app.get("/account", response_class=HTMLResponse)
    def page_account(request: Request) -> Any:
        with _conn() as conn:
            users = U.list_users(conn) if _me().role == "owner" else []
            sessions = conn.execute(
                "SELECT COUNT(*) AS n FROM user_session WHERE user_id = ? AND "
                "revoked_at IS NULL AND expires_at > ?",
                (_uid(), datetime.now(UTC).isoformat()),
            ).fetchone()["n"]
        return templates.TemplateResponse(
            request=request,
            name="account.html",
            context={
                "me": _me(),
                "users": users,
                "sessions": sessions,
                "min_password_len": U.MIN_PASSWORD_LEN,
                "invite_link": None,
                "error": None,
                "notice": None,
            },
        )

    def _account_page(request: Request, **over: Any) -> Any:
        """Re-render /account after a POST, without duplicating its context."""
        with _conn() as conn:
            users = U.list_users(conn) if _me().role == "owner" else []
            sessions = conn.execute(
                "SELECT COUNT(*) AS n FROM user_session WHERE user_id = ? AND "
                "revoked_at IS NULL AND expires_at > ?",
                (_uid(), datetime.now(UTC).isoformat()),
            ).fetchone()["n"]
        ctx: dict[str, Any] = {
            "me": _me(),
            "users": users,
            "sessions": sessions,
            "min_password_len": U.MIN_PASSWORD_LEN,
            "invite_link": None,
            "error": None,
            "notice": None,
        }
        ctx.update(over)
        return templates.TemplateResponse(
            request=request, name="account.html", context=ctx, status_code=over.pop("code", 200)
        )

    @app.post(
        "/account/password", response_class=HTMLResponse, dependencies=[Depends(_same_origin)]
    )
    def post_change_password(
        request: Request, current: str = Form(...), new: str = Form(...)
    ) -> Any:
        with _conn() as conn:
            try:
                U.change_password(conn, user_id=_uid(), current=current, new=new)
                conn.commit()
            except ValueError as exc:
                return _account_page(request, error=str(exc))
        # change_password revokes every session, including this one, so the
        # browser must sign in again — which is the point of changing it.
        response = RedirectResponse("/login", status_code=303)
        clear_session_cookie(response)
        return response

    @app.post("/account/invite", response_class=HTMLResponse, dependencies=[Depends(_same_origin)])
    def post_create_invite(request: Request, email: str = Form(...)) -> Any:
        me = _me()
        if me.role != "owner":
            raise HTTPException(status_code=403, detail="owner only")
        with _conn() as conn:
            try:
                token = U.create_invite(conn, email=email, invited_by=me.id)
                conn.commit()
            except ValueError as exc:
                return _account_page(request, error=str(exc))
        base = str(request.base_url).rstrip("/")
        return _account_page(request, invite_link=f"{base}/invite/{token}")

    @app.post("/account/sessions/revoke", dependencies=[Depends(_same_origin)])
    def post_revoke_sessions(request: Request) -> Any:
        with _conn() as conn:
            U.revoke_all_sessions(conn, user_id=_uid())
            conn.commit()
        response = RedirectResponse("/login", status_code=303)
        clear_session_cookie(response)
        return response

    # ---- pages ------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def page_dashboard(request: Request, date: str | None = None) -> Any:
        day = _day(date)
        with _conn() as conn:
            status = tools.get_daily_status(conn, date=day, user_id=_uid())
            plan = tools.get_plan_status(conn, end=day, user_id=_uid())
            tdee = tools.get_tdee_estimate(conn, end=day, user_id=_uid())
            weight = tools.get_weight_trend(conn, end=day, window=30, user_id=_uid())
            safety = tools.get_safety_flags(conn, end=day, user_id=_uid())
            sessions = tools.get_training_sessions(conn, date=day, user_id=_uid())
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
                "sessions": sessions,
            },
        )

    @app.get("/plan", response_class=HTMLResponse)
    def page_plan(request: Request) -> Any:
        day = _today(cfg)
        with _conn() as conn:
            plan = tools.get_plan_status(conn, end=day, user_id=_uid())
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
        """Set the active plan through the SAME service the CLI uses.

        This handler used to carry its own copy of the resolution/clamp logic and
        silently skipped the coaching note the CLI wrote — so a plan changed here
        left no trace in memory. One seam, no drift (services/plan.py).
        """
        from ..services.plan import PlanInputError, set_active_plan

        day = _today(cfg)
        with _conn() as conn:
            try:
                set_active_plan(
                    conn,
                    today=day,
                    rate=rate if mode == "rate" else None,
                    goal_weight=goal_weight if mode == "deadline" else None,
                    by=by if mode == "deadline" else None,
                    maintain=(mode == "maintain"),
                    start_date=start_date or None,
                    user_id=_uid(),
                )
                conn.commit()
            except PlanInputError as exc:
                plan = tools.get_plan_status(conn, end=day, user_id=_uid())
                return templates.TemplateResponse(
                    request=request,
                    name="plan.html",
                    context={"day": day, "plan": plan, "error": str(exc)},
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

    def _run_in_job(fn, user: U.User) -> Any:
        """Wrap a body needing its own connection (jobs run on other threads).

        The submitting user is captured at submit time and re-established inside
        the worker thread. A job outlives the request that started it, so the
        request-scoped ContextVar is long gone by the time the body runs — and
        without this, a job would ingest or normalize with no user at all.
        Carrying the user explicitly also means a job can never act as somebody
        else just because it happened to run later.
        """

        def body(emit) -> None:
            token = _REQUEST_USER.set(user)
            conn = db.connect(cfg.db_path)
            try:
                fn(conn, emit)
            finally:
                conn.close()
                _REQUEST_USER.reset(token)

        return body

    def _start(name: str, fn) -> dict:
        user = _REQUEST_USER.get()
        if user is None:  # middleware guarantees this, but never guess an owner
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            job = runner.submit(name, _run_in_job(fn, user))
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
                    conn, whoop_client(cfg), since=start, until=until, user_id=_uid()
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
                result = ingest_mfp(conn, mfp_client(cfg), since=start, until=end, user_id=_uid())
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
            counts = normalize_all(conn, user_id=_uid(), rebuild=rebuild)
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
            rep = hrv_validation_report(conn, user_id=_uid(), end=_day(end), window=window)

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
