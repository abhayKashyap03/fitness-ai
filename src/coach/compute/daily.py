"""Daily rollup — the "single circle" view (T3.1).

All numbers come from deterministic SQL aggregation here; the LLM never computes
(§2.2). Missing data is explicit: "not logged" is a distinct state from zero, and
absent measurements are ``None``, never invented (§2.2).

Workouts are counted ONCE per ``session_group_id`` (§5) — cross-source calorie
precedence within a group is deferred to T3.4 (see DECISIONS_NEEDED); today a
group's representative is chosen by a documented source rank.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# representative pick when a session_group has rows from multiple sources
_SOURCE_RANK = "CASE source WHEN 'whoop_api' THEN 1 WHEN 'whoop_ble' THEN 2 ELSE 9 END"


@dataclass(frozen=True)
class RecoverySummary:
    source: str
    score: float | None
    hrv_rmssd_ms: float | None
    resting_hr_bpm: float | None


@dataclass(frozen=True)
class WeightSummary:
    source: str
    weight_kg: float | None
    trend_kg: float | None


@dataclass(frozen=True)
class FoodSummary:
    logged: bool
    is_fast: bool
    is_complete: bool
    source: str | None
    kcal: float | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None


@dataclass(frozen=True)
class TrainingSummary:
    sessions: int  # distinct real sessions (grouped)
    kcal_active: float | None
    duration_s: int | None
    strain: float | None


@dataclass(frozen=True)
class DailyStatus:
    day_key: str
    user_id: int
    recovery: RecoverySummary | None
    weight: WeightSummary | None
    food: FoodSummary
    training: TrainingSummary
    notes: list[str] = field(default_factory=list)


def _recovery(conn: sqlite3.Connection, day: str, uid: int) -> RecoverySummary | None:
    r = conn.execute(
        "SELECT source, score, hrv_rmssd_ms, resting_hr_bpm FROM recovery_resolved "
        "WHERE user_id=? AND day_key=?",
        (uid, day),
    ).fetchone()
    if r is None:
        return None
    return RecoverySummary(r["source"], r["score"], r["hrv_rmssd_ms"], r["resting_hr_bpm"])


def _weight(conn: sqlite3.Connection, day: str, uid: int) -> WeightSummary | None:
    r = conn.execute(
        "SELECT source, weight_kg FROM weight_resolved_daily WHERE user_id=? AND day_key=?",
        (uid, day),
    ).fetchone()
    if r is None:
        return None
    trend = conn.execute(
        "SELECT trend_kg FROM weight_trend WHERE user_id=? AND day_key=?", (uid, day)
    ).fetchone()
    return WeightSummary(r["source"], r["weight_kg"], trend["trend_kg"] if trend else None)


def _food(conn: sqlite3.Connection, day: str, uid: int) -> FoodSummary:
    r = conn.execute(
        "SELECT source, kcal_total, protein_g_total, carbs_g_total, fat_g_total, "
        "is_fast, is_complete FROM food_daily WHERE user_id=? AND day_key=?",
        (uid, day),
    ).fetchone()
    if r is None:
        # absence == not logged; NEVER a misleading zero
        return FoodSummary(False, False, False, None, None, None, None, None)
    return FoodSummary(
        logged=True,
        is_fast=bool(r["is_fast"]),
        is_complete=bool(r["is_complete"]),
        source=r["source"],
        kcal=r["kcal_total"],
        protein_g=r["protein_g_total"],
        carbs_g=r["carbs_g_total"],
        fat_g=r["fat_g_total"],
    )


def _training(conn: sqlite3.Connection, day: str, uid: int) -> TrainingSummary:
    # one representative row per session group (count once), then aggregate
    rows = conn.execute(
        f"""
        WITH ranked AS (
          SELECT *,
                 COALESCE(session_group_id, id) AS grp,
                 ROW_NUMBER() OVER (
                   PARTITION BY COALESCE(session_group_id, id)
                   ORDER BY {_SOURCE_RANK}, id
                 ) AS rnk
          FROM workout WHERE user_id=? AND day_key=?
        )
        SELECT kcal_active, kcal_total, duration_s, strain FROM ranked WHERE rnk=1
        """,
        (uid, day),
    ).fetchall()
    if not rows:
        return TrainingSummary(0, None, None, None)

    def _sum(key: str) -> float | None:
        vals = [r[key] for r in rows if r[key] is not None]
        return sum(vals) if vals else None

    # prefer active kcal; fall back to total when active isn't separated (WHOOP)
    kcal = _sum("kcal_active")
    if kcal is None:
        kcal = _sum("kcal_total")
    dur = _sum("duration_s")
    return TrainingSummary(
        sessions=len(rows),
        kcal_active=kcal,
        duration_s=int(dur) if dur is not None else None,
        strain=_sum("strain"),
    )


def daily_status(conn: sqlite3.Connection, day_key: str, user_id: int = 1) -> DailyStatus:
    food = _food(conn, day_key, user_id)
    recovery = _recovery(conn, day_key, user_id)
    weight = _weight(conn, day_key, user_id)
    training = _training(conn, day_key, user_id)

    notes: list[str] = []
    if recovery is None:
        notes.append("no recovery data for this day")
    if weight is None:
        notes.append("no weight logged this day")
    if not food.logged:
        notes.append("food not logged (distinct from a zero-calorie day)")
    elif food.is_fast:
        notes.append("declared fast (known zero intake)")
    elif not food.is_complete:
        notes.append("food log incomplete (some entries missing kcal)")
    notes.append("basal/TDEE not shown here — see `coach tdee` (T3.3)")

    return DailyStatus(
        day_key=day_key,
        user_id=user_id,
        recovery=recovery,
        weight=weight,
        food=food,
        training=training,
        notes=notes,
    )
