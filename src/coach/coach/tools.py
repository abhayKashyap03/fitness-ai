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

from ..compute.daily import daily_status
from ..compute.guardrails import Alert, TrendPoint, weight_loss_rate_alert
from ..compute.tdee import build_window, estimate_tdee
from ..compute.trends import Insufficient

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


# ---- registry --------------------------------------------------------------

_DAY = {"type": "string", "description": "day_key in YYYY-MM-DD"}
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
            "required": ["date"],
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
            "required": ["end"],
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
            "required": ["end"],
        },
        handler=get_recovery_history,
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
            "required": ["end"],
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
            "required": ["end"],
        },
        handler=get_safety_flags,
    ),
]

_BY_NAME = {t.name: t for t in TOOLS}


def anthropic_tool_defs() -> list[dict]:
    """The tool definitions to pass to the Messages API (no handlers leaked)."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in TOOLS
    ]


def dispatch(
    conn: sqlite3.Connection, name: str, args: dict, *, user_id: int = 1
) -> dict:
    """Run a tool by name with model-supplied ``args``. Raises on unknown tool."""
    spec = _BY_NAME.get(name)
    if spec is None:
        raise KeyError(f"unknown tool: {name!r}")
    return spec.handler(conn, user_id=user_id, **args)
