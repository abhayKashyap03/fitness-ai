"""Canonical row persistence for recovery + workout.

Ids are DETERMINISTIC functions of the row's identity, so re-normalizing the
same raw yields the same primary keys — the precondition for §2.1's regenerable
canonical and for byte-identical ``--rebuild``. ``derived_at`` (a wall-clock) is
the only non-deterministic column and is excluded from the rebuild fingerprint.
"""

from __future__ import annotations

import sqlite3

from ..normalize.whoop import RecoveryRow, WorkoutRow


def recovery_id(row: RecoveryRow) -> str:
    return f"rec:{row.user_id}:{row.source}:{row.day_key}:{row.score_method}"


def workout_id(row: WorkoutRow) -> str:
    if row.external_id:
        return f"wk:{row.user_id}:{row.source}:{row.external_id}"
    # fallback for sources without a stable id: identity from start+sport
    return f"wk:{row.user_id}:{row.source}:{row.start_at}:{row.sport_type}"


def upsert_recovery(
    conn: sqlite3.Connection, row: RecoveryRow, *, raw_ref: str, derived_at: str
) -> str:
    rid = recovery_id(row)
    conn.execute(
        "INSERT OR REPLACE INTO recovery (id, user_id, day_key, source, measured_at, "
        "tz_name, hrv_rmssd_ms, resting_hr_bpm, spo2_pct, skin_temp_c, resp_rate_bpm, "
        "score, score_scale, score_method, is_official, raw_ref, derived_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rid,
            row.user_id,
            row.day_key,
            row.source,
            row.measured_at,
            row.tz_name,
            row.hrv_rmssd_ms,
            row.resting_hr_bpm,
            row.spo2_pct,
            row.skin_temp_c,
            row.resp_rate_bpm,
            row.score,
            row.score_scale,
            row.score_method,
            row.is_official,
            raw_ref,
            derived_at,
        ),
    )
    return rid


def upsert_workout(
    conn: sqlite3.Connection, row: WorkoutRow, *, raw_ref: str, derived_at: str
) -> str:
    wid = workout_id(row)
    conn.execute(
        "INSERT OR REPLACE INTO workout (id, user_id, source, external_id, sport_type, "
        "source_sport_raw, start_at, end_at, tz_name, day_key, duration_s, kcal_active, "
        "kcal_total, avg_hr_bpm, max_hr_bpm, strain, distance_m, hr_zones_json, "
        "raw_ref, derived_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            wid,
            row.user_id,
            row.source,
            row.external_id,
            row.sport_type,
            row.source_sport_raw,
            row.start_at,
            row.end_at,
            row.tz_name,
            row.day_key,
            row.duration_s,
            row.kcal_active,
            row.kcal_total,
            row.avg_hr_bpm,
            row.max_hr_bpm,
            row.strain,
            row.distance_m,
            row.hr_zones_json,
            raw_ref,
            derived_at,
        ),
    )
    return wid


def canonical_fingerprint(conn: sqlite3.Connection) -> str:
    """Stable digest of all canonical rows EXCLUDING volatile ``derived_at``.

    Used to prove ``--rebuild`` reproduces identical canonical output.
    """
    import hashlib

    parts: list[str] = []
    rec_cols = (
        "id,user_id,day_key,source,measured_at,tz_name,hrv_rmssd_ms,resting_hr_bpm,"
        "spo2_pct,skin_temp_c,resp_rate_bpm,score,score_scale,score_method,is_official,raw_ref"
    )
    for r in conn.execute(f"SELECT {rec_cols} FROM recovery ORDER BY id"):
        parts.append("|".join("" if v is None else str(v) for v in r))
    wk_cols = (
        "id,user_id,source,external_id,sport_type,source_sport_raw,start_at,end_at,"
        "tz_name,day_key,duration_s,kcal_active,kcal_total,avg_hr_bpm,max_hr_bpm,"
        "strain,distance_m,hr_zones_json,session_group_id,dedupe_hash,raw_ref"
    )
    for r in conn.execute(f"SELECT {wk_cols} FROM workout ORDER BY id"):
        parts.append("|".join("" if v is None else str(v) for v in r))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()
