"""Coach tool-calling contract (T4.1).

The tools the coach model MAY call. Each returns **JSON-serializable structured
data** with explicit provenance (a ``source`` field) and explicit nulls /
insufficient-data markers. Hard rules (§2.2):

  * **No tool returns prose.** Only structured data.
  * **No tool does math** the compute layer hasn't already done. Handlers are
    thin adapters over the tested Phase-3 compute (`compute.daily`,
    `compute.tdee`) and the canonical resolver views.
  * **Absence stays absent.** "Not logged" and "insufficient data" are explicit
    states, never a fabricated 0 or an interpolated number (§2.7).

This module has **no Anthropic dependency and makes no model call** — it is the
deterministic contract + handlers the model gets wired to in T4.2. Keeping it
pure-Python and DB-only means every tool is unit-testable without tokens (§8.7).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass

from ..compute.daily import daily_status, training_sessions
from ..compute.guardrails import Alert, TrendPoint, weight_loss_rate_alert
from ..compute.plan import plan_status, protein_status
from ..compute.tdee import build_window, estimate_tdee
from ..compute.trends import Insufficient
from ..store.notes import recent_notes
from ..store.plan import active_plan
from .llm import ToolSpec as LLMToolSpec

Handler = Callable[..., dict]


@dataclass(frozen=True)
class ToolSpec:
    """One callable tool: its API-facing schema + its deterministic handler."""

    name: str
    description: str
    input_schema: dict
    handler: Handler


def _insufficient(marker: Insufficient) -> dict:
    return {"have": marker.have, "needed": marker.needed}


def _alert(a: Alert) -> dict:
    return {"level": a.level, "code": a.code, "message": a.message, "evidence": a.evidence}


# ---- handlers --------------------------------------------------------------


def get_daily_status(conn: sqlite3.Connection, *, date: str, user_id: int = 1) -> dict:
    """Full daily rollup for one ``day_key``. Missing sections are null; food
    carries an explicit ``logged`` flag so "not logged" never reads as zero."""
    s = daily_status(conn, date, user_id=user_id)
    # asdict preserves None sections as null and keeps every explicit flag.
    return asdict(s)


def get_weight_trend(
    conn: sqlite3.Connection, *, end: str, window: int = 30, user_id: int = 1
) -> dict:
    """EWMA-smoothed weight series over ``window`` days ending ``end``.

    Values come straight from the ``weight_trend`` view (the compute layer's
    EWMA); provenance from ``weight_resolved_daily``. Empty window => explicit
    insufficient marker, not an invented number.
    """
    from datetime import date, timedelta

    end_d = date.fromisoformat(end)
    start = (end_d - timedelta(days=window - 1)).isoformat()
    rows = conn.execute(
        "SELECT t.day_key, t.weight_kg, t.trend_kg, r.source, r.source_app "
        "FROM weight_trend t "
        "LEFT JOIN weight_resolved_daily r "
        "  ON r.user_id = t.user_id AND r.day_key = t.day_key "
        "WHERE t.user_id = ? AND t.day_key BETWEEN ? AND ? "
        "ORDER BY t.day_key",
        (user_id, start, end),
    ).fetchall()
    series = [
        {
            "day_key": r["day_key"],
            "weight_kg": r["weight_kg"],
            "trend_kg": r["trend_kg"],
            "source": r["source"],
            "source_app": r["source_app"],
        }
        for r in rows
    ]
    return {
        "end": end,
        "window": window,
        "unit": "kg",
        "series": series,
        "latest_trend_kg": series[-1]["trend_kg"] if series else None,
        "insufficient": None if series else {"have": 0, "needed": 1},
    }


def get_recovery_history(
    conn: sqlite3.Connection, *, end: str, window: int = 14, user_id: int = 1
) -> dict:
    """Resolved recovery rows (objective measures + score) over a window.

    Objective measures (hrv, resting hr) are comparable across sources; the
    composite ``score`` is not (§2.3) — both are returned with their ``source``.
    """
    from datetime import date, timedelta

    end_d = date.fromisoformat(end)
    start = (end_d - timedelta(days=window - 1)).isoformat()
    rows = conn.execute(
        "SELECT day_key, source, score, hrv_rmssd_ms, resting_hr_bpm "
        "FROM recovery_resolved "
        "WHERE user_id = ? AND day_key BETWEEN ? AND ? ORDER BY day_key",
        (user_id, start, end),
    ).fetchall()
    series = [dict(r) for r in rows]
    return {
        "end": end,
        "window": window,
        "series": series,
        "insufficient": None if series else {"have": 0, "needed": 1},
    }


def get_sleep_history(
    conn: sqlite3.Connection, *, end: str, window: int = 14, user_id: int = 1
) -> dict:
    """Resolved night-sleep rows over a window (naps excluded by the resolver).

    Objective stage durations are cross-source comparable; the composite
    percentages are the source's own scoring (is_official flags it) and are
    NOT comparable across sources (§2.3).
    """
    from datetime import date, timedelta

    end_d = date.fromisoformat(end)
    start = (end_d - timedelta(days=window - 1)).isoformat()
    rows = conn.execute(
        "SELECT day_key, source, in_bed_min, awake_min, light_min, sws_min, rem_min, "
        "sleep_cycle_count, disturbance_count, respiratory_rate, performance_pct, "
        "efficiency_pct, is_official "
        "FROM sleep_resolved WHERE user_id = ? AND day_key BETWEEN ? AND ? "
        "ORDER BY day_key",
        (user_id, start, end),
    ).fetchall()
    series = [dict(r) for r in rows]
    return {
        "end": end,
        "window": window,
        "unit": "minutes",
        "series": series,
        "insufficient": None if series else {"have": 0, "needed": 1},
    }


def get_tdee_estimate(
    conn: sqlite3.Connection, *, end: str, window: int = 14, user_id: int = 1
) -> dict:
    """Adaptive TDEE over ``window`` days ending ``end``.

    Degrades honestly: too few logged-intake days => ``estimate`` is null and an
    ``insufficient`` marker says how many days are needed (§2.2). Never a
    confident-but-wrong number.
    """
    pts = build_window(conn, end, window, user_id)
    est = estimate_tdee(pts)
    if isinstance(est, Insufficient):
        return {
            "end": end,
            "window": window,
            "method": "adaptive_energy_balance",
            "estimate": None,
            "insufficient": _insufficient(est),
        }
    return {
        "end": end,
        "window": window,
        "method": "adaptive_energy_balance",
        "estimate": asdict(est),
        "insufficient": None,
    }


def get_safety_flags(
    conn: sqlite3.Connection, *, end: str, window: int = 30, user_id: int = 1
) -> dict:
    """Deterministic health-safety flags over a window (§8.6).

    Code-enforced hard limits, not model judgment: currently an unsafe
    weight-loss-rate check off the EWMA trend. Returns structured alerts the
    coach must surface plainly; an empty list means nothing tripped, and an
    ``insufficient`` marker means there isn't enough trend to judge (no false
    alarms).
    """
    from datetime import date, timedelta

    end_d = date.fromisoformat(end)
    start = (end_d - timedelta(days=window - 1)).isoformat()
    rows = conn.execute(
        "SELECT day_key, trend_kg FROM weight_trend "
        "WHERE user_id = ? AND day_key BETWEEN ? AND ? ORDER BY day_key",
        (user_id, start, end),
    ).fetchall()
    series = [TrendPoint(r["day_key"], r["trend_kg"]) for r in rows if r["trend_kg"] is not None]

    alerts: list[dict] = []
    insufficient: dict | None = None
    result = weight_loss_rate_alert(series)
    if isinstance(result, Insufficient):
        insufficient = _insufficient(result)
    elif isinstance(result, Alert):
        alerts.append(_alert(result))

    return {"end": end, "window": window, "alerts": alerts, "insufficient": insufficient}


def get_training_sessions(conn: sqlite3.Connection, *, date: str, user_id: int = 1) -> dict:
    """The day's individual training sessions, deduped across sources (§5).

    ``get_daily_status`` gives the day's totals; this gives what was actually
    done. Empty list = no session recorded, which is a true zero (unlike food,
    where not-logged and zero are different facts).
    """
    sessions = [asdict(s) for s in training_sessions(conn, date, user_id=user_id)]
    return {"date": date, "sessions": sessions, "count": len(sessions)}


def get_coach_notes(conn: sqlite3.Connection, *, limit: int = 20, user_id: int = 1) -> dict:
    """Past coaching decisions and observations, newest first (ADR-0016).

    This is MEMORY, not measurement: it records that a decision was made and why,
    so guidance stays consistent between sessions. It never carries a current
    number — anything measurable must be re-read from the other tools, because a
    note is a historical statement and may be out of date.

    An empty list means nothing has been recorded yet; say so rather than
    inferring history (§2.7).
    """
    notes = [
        {
            "created_at": n.created_at,
            "day_key": n.day_key,
            "author": n.author,
            "kind": n.kind,
            "text": n.text,
        }
        for n in recent_notes(conn, limit=limit, user_id=user_id)
    ]
    return {"notes": notes, "count": len(notes)}


def get_plan_status(
    conn: sqlite3.Connection, *, end: str, window: int = 14, user_id: int = 1
) -> dict:
    """The active cut/bulk plan vs measured state (Phase 7, ADR-0013).

    Returns the daily calorie goal, target rate, timeline projection, and any
    fired §8.6 safety alert — all computed by ``compute.plan`` (no math here).
    ``plan`` is null when none is set; ``status`` is null with an ``insufficient``
    marker when TDEE or the weight trend can't be measured (§2.2). The daily goal
    is always past the calorie-floor clamp — the floor wins.
    """
    plan = active_plan(conn, user_id=user_id)
    if plan is None:
        return {"end": end, "plan": None, "status": None, "protein": None, "insufficient": None}

    plan_dict = {
        "direction": plan.direction,
        "target_rate_pct_per_week": plan.target_rate_pct_per_week,
        "goal_weight_kg": plan.goal_weight_kg,
        "start_day_key": plan.start_day_key,
        "start_weight_kg": plan.start_weight_kg,
        "protein_g_per_kg": plan.protein_g_per_kg,
    }

    est = estimate_tdee(build_window(conn, end, window, user_id))
    # Pass the marker through unflattened so the caller learns WHAT is missing.
    tdee_kcal = est if isinstance(est, Insufficient) else est.tdee_kcal
    row = conn.execute(
        "SELECT trend_kg FROM weight_trend WHERE user_id = ? AND day_key <= ? "
        "AND trend_kg IS NOT NULL ORDER BY day_key DESC LIMIT 1",
        (user_id, end),
    ).fetchone()
    current_trend = row["trend_kg"] if row else None

    # The day's logged protein, so a plan that carries a g/kg figure can be
    # judged against what was actually eaten. Read straight from the daily
    # rollup: NULL here means not logged, which is not zero (§2.7), and the
    # compute layer keeps that distinction rather than scoring a missed target.
    food = conn.execute(
        "SELECT protein_g_total FROM food_daily WHERE user_id = ? AND day_key = ?",
        (user_id, end),
    ).fetchone()
    logged_protein = food["protein_g_total"] if food else None

    st = plan_status(
        direction=plan.direction,
        target_rate_pct_per_week=plan.target_rate_pct_per_week,
        goal_weight_kg=plan.goal_weight_kg,
        tdee_kcal=tdee_kcal,
        current_trend_kg=current_trend,
        end_day=end,
        start_day_key=plan.start_day_key,
        start_weight_kg=plan.start_weight_kg,
        protein_g_per_kg=plan.protein_g_per_kg,
        logged_protein_g=logged_protein,
    )
    # Protein needs only a g/kg figure and the trend weight, so it is reported
    # ALONGSIDE status rather than inside it — a target the user just set must not
    # be invisible for the ten days it takes TDEE to become measurable.
    prot = protein_status(
        protein_g_per_kg=plan.protein_g_per_kg,
        current_trend_kg=current_trend,
        logged_protein_g=logged_protein,
    )
    protein_dict = asdict(prot) if prot is not None else None

    if isinstance(st, Insufficient):
        return {
            "end": end,
            "plan": plan_dict,
            "status": None,
            "protein": protein_dict,
            "insufficient": _insufficient(st),
        }
    # asdict recurses PlanStatus.alerts (Alert dataclasses) into the same dict
    # shape as _alert — no prose, structured only.
    return {
        "end": end,
        "plan": plan_dict,
        "status": asdict(st),
        "protein": protein_dict,
        "insufficient": None,
    }


# ---- registry --------------------------------------------------------------

_DAY = {
    "type": "string",
    "description": "day_key in YYYY-MM-DD; OMIT for today (the server fills the real current date)",
}
_WINDOW = {"type": "integer", "description": "number of days", "minimum": 1}

TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="get_daily_status",
        description=(
            "Full daily rollup (recovery, weight, food, training) for one day. "
            "Missing sections are null; food.logged=false means NOT LOGGED, "
            "which is different from zero intake."
        ),
        input_schema={
            "type": "object",
            "properties": {"date": _DAY},
        },
        handler=get_daily_status,
    ),
    ToolSpec(
        name="get_weight_trend",
        description=(
            "EWMA-smoothed body-weight trend series over a window of days. Use "
            "the trend, not raw daily weight, to judge a cut/bulk direction."
        ),
        input_schema={
            "type": "object",
            "properties": {"end": _DAY, "window": _WINDOW},
        },
        handler=get_weight_trend,
    ),
    ToolSpec(
        name="get_recovery_history",
        description=(
            "Resolved recovery rows over a window: objective measures (HRV, "
            "resting HR) plus the source's composite score."
        ),
        input_schema={
            "type": "object",
            "properties": {"end": _DAY, "window": _WINDOW},
        },
        handler=get_recovery_history,
    ),
    ToolSpec(
        name="get_sleep_history",
        description=(
            "Resolved night-sleep rows over a window: stage minutes (in-bed, "
            "light, slow-wave, REM, awake), disturbances, respiratory rate, "
            "and the source's composite percentages. Naps excluded."
        ),
        input_schema={
            "type": "object",
            "properties": {"end": _DAY, "window": _WINDOW},
        },
        handler=get_sleep_history,
    ),
    ToolSpec(
        name="get_tdee_estimate",
        description=(
            "Adaptive TDEE (kcal/day) from logged intake + weight trend over a "
            "window. Returns estimate=null with an insufficient marker when "
            "intake logging is too sparse."
        ),
        input_schema={
            "type": "object",
            "properties": {"end": _DAY, "window": _WINDOW},
        },
        handler=get_tdee_estimate,
    ),
    ToolSpec(
        name="get_safety_flags",
        description=(
            "Deterministic health-safety flags (§8.6) over a window — e.g. an "
            "unsafe rate of weight loss. Code-enforced hard limits, not model "
            "judgment. Surface any returned alert plainly to the user."
        ),
        input_schema={
            "type": "object",
            "properties": {"end": _DAY, "window": _WINDOW},
        },
        handler=get_safety_flags,
    ),
    ToolSpec(
        name="get_training_sessions",
        description=(
            "The individual training sessions recorded on a day (sport, duration, "
            "calories, source), already deduped so one real workout seen by two "
            "sources is listed once. An empty list means no session was recorded."
        ),
        input_schema={"type": "object", "properties": {"date": _DAY}},
        handler=get_training_sessions,
    ),
    ToolSpec(
        name="get_coach_notes",
        description=(
            "Past coaching decisions and observations (newest first) so guidance "
            "stays consistent with what was already advised. These are HISTORICAL "
            "statements, not current measurements — re-read any number from the "
            "other tools rather than quoting it from a note. An empty list means "
            "nothing has been recorded yet."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "how many notes", "minimum": 1}
            },
        },
        handler=get_coach_notes,
    ),
    ToolSpec(
        name="get_plan_status",
        description=(
            "The user's active cut/bulk plan vs measured state: today's calorie "
            "goal, target rate, projected timeline to goal weight, and any fired "
            "§8.6 safety alert. All numbers are code-computed and floor-clamped. "
            "plan=null means no plan is set; status=null with an insufficient "
            "marker means TDEE or the weight trend can't be measured yet."
        ),
        input_schema={
            "type": "object",
            "properties": {"end": _DAY, "window": _WINDOW},
        },
        handler=get_plan_status,
    ),
]

_BY_NAME = {t.name: t for t in TOOLS}

# per-tool name of its day anchor (filled with the server-side "today" when the
# model omits it — the model must never have to guess the current date, §2.2)
# Per-tool day anchor, filled server-side when the model omits it (the model has
# no clock, §2.2). get_coach_notes has no day argument at all — None means
# "inject nothing", so a date never gets smuggled into a tool that has no window.
_DAY_ARG: dict[str, str | None] = {}
for _t in TOOLS:
    if _t.name in {"get_daily_status", "get_training_sessions"}:
        _DAY_ARG[_t.name] = "date"
    elif _t.name == "get_coach_notes":
        _DAY_ARG[_t.name] = None
    else:
        _DAY_ARG[_t.name] = "end"


def tool_specs() -> list[LLMToolSpec]:
    """Provider-neutral tool definitions (no handlers leaked).

    Each LLM provider translates these into its own schema at its own boundary
    (§2.5) — this contract stays vendor-free.
    """
    return [
        LLMToolSpec(name=t.name, description=t.description, input_schema=t.input_schema)
        for t in TOOLS
    ]


def dispatch(
    conn: sqlite3.Connection,
    name: str,
    args: dict,
    *,
    user_id: int = 1,
    today: str | None = None,
) -> dict:
    """Run a tool by name with model-supplied ``args``. Raises on unknown tool.

    When ``today`` is given and the model omitted the tool's day anchor
    (``end``/``date``), the REAL current day_key is filled in server-side — the
    model has no reliable clock and must never guess dates (§2.2).
    """
    spec = _BY_NAME.get(name)
    if spec is None:
        raise KeyError(f"unknown tool: {name!r}")
    day_arg = _DAY_ARG[name]
    if day_arg is not None and today is not None and not args.get(day_arg):
        args = {**args, day_arg: today}
    return spec.handler(conn, user_id=user_id, **args)
