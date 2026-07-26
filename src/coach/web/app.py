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
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..coach import tools
from ..config import ConfigError, Settings, load_settings
from ..store import db

TEMPLATES_DIR = Path(__file__).parent / "templates"


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

    @app.post("/api/ask")
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

    @app.post("/plan", response_class=HTMLResponse)
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

    return app


def _shift(day_key: str, days: int) -> str:
    from datetime import timedelta

    return (date.fromisoformat(day_key) + timedelta(days=days)).isoformat()
