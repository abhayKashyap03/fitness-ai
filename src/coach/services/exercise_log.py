"""Log a training session by hand (ROADMAP P12, own logging).

The exercise counterpart to `services/food_log.py`, and written the same way
round: the raw event holds everything the canonical row is derived from, so
``normalize --rebuild`` regenerates hand-logged sessions rather than deleting
them (§2.1 — a lesson learned the hard way by the food path).

One seam for every surface, so the CLI, the web form and a future app cannot
drift the way `plan set` once did.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from ..normalize.exercise import parse_exercise_log
from ..store.canonical import upsert_workout
from ..store.raw import insert_raw_event


@dataclass(frozen=True)
class LoggedExercise:
    workout_id: str
    raw_ref: str
    day_key: str
    sport_type: str
    duration_s: int
    kcal: float | None


def log_exercise(
    conn: sqlite3.Connection,
    *,
    sport: str,
    duration_s: int,
    started_at: str,
    utc_offset: str | None = None,
    tz_name: str | None = None,
    kcal: float | None = None,
    distance_m: float | None = None,
    avg_hr_bpm: float | None = None,
    user_id: int = 1,
) -> LoggedExercise:
    """Record one hand-logged session. Raises ValueError if it describes nothing."""
    now = datetime.now(UTC).isoformat()
    envelope = {
        "sport": sport,
        "duration_s": duration_s,
        "started_at": started_at,
        "utc_offset": utc_offset,
        "tz_name": tz_name,
        "kcal": kcal,
        "distance_m": distance_m,
        "avg_hr_bpm": avg_hr_bpm,
        "logged_at": now,
    }
    row = parse_exercise_log(envelope, user_id=user_id)
    if row is None:
        raise ValueError("a session needs a start time and a positive duration")

    raw_ref, _ = insert_raw_event(
        conn,
        source="manual",
        record_type="exercise_log",
        payload=envelope,
        # Scoped so the same session logged twice dedupes, while a second
        # genuinely different session that day is its own event.
        external_id=f"{started_at}:{row.sport_type}:{duration_s}",
        recorded_at=started_at,
        user_id=user_id,
    )
    workout_id = upsert_workout(conn, row, raw_ref=raw_ref, derived_at=now)

    # Re-run session grouping, exactly as `normalize` does.
    #
    # Without this the write path leaves session_group_id unset while a rebuild
    # assigns it, so the two produce DIFFERENT canonical state from the same raw
    # — caught by a byte-identical-rebuild test. It also matters on its own
    # terms: a session hand-logged for a workout the strap also caught must join
    # that group immediately, or compute counts the workout twice (§5's expected
    # bug class) until the next normalize.
    from ..normalize.runner import regroup_workouts

    regroup_workouts(conn)

    return LoggedExercise(
        workout_id=workout_id,
        raw_ref=raw_ref,
        day_key=row.day_key,
        sport_type=row.sport_type,
        duration_s=row.duration_s or duration_s,
        kcal=row.kcal_active,
    )
