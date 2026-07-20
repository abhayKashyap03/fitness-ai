"""Normalize orchestrator: raw_events -> canonical recovery + workout.

This is the impure boundary (it does I/O). The actual raw->canonical mapping is
delegated to the pure functions in :mod:`coach.normalize.whoop`. ``--rebuild``
drops canonical and fully re-derives from raw (§2.1).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ..store.canonical import upsert_recovery, upsert_weight, upsert_workout
from .dedup import DEFAULT_TOLERANCE_S, WkSlot, assign_session_groups
from .healthkit import parse_body_record
from .whoop import parse_recovery, parse_workout


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _cycle_offsets(conn: sqlite3.Connection) -> dict[int, str]:
    """cycle_id -> timezone_offset, from raw WHOOP cycle events.

    Recovery records carry no offset; their local day comes from the cycle.
    """
    out: dict[int, str] = {}
    for r in conn.execute(
        "SELECT payload FROM raw_events WHERE source='whoop_api' AND record_type='cycle'"
    ):
        p = json.loads(r["payload"])
        cid, off = p.get("id"), p.get("timezone_offset")
        if cid is not None and off is not None:
            out[int(cid)] = off
    return out


def _sleep_resp_rates(conn: sqlite3.Connection) -> dict[str, float]:
    """sleep_id -> respiratory_rate, from raw WHOOP sleep events.

    WHOOP reports respiratory rate on the sleep record; recovery links to it via
    ``sleep_id``. Unscored sleeps (no score) are simply absent (§2.7).
    """
    out: dict[str, float] = {}
    for r in conn.execute(
        "SELECT payload FROM raw_events WHERE source='whoop_api' AND record_type='sleep'"
    ):
        p = json.loads(r["payload"])
        sid = p.get("id")
        rate = (p.get("score") or {}).get("respiratory_rate")
        if sid is not None and rate is not None:
            out[str(sid)] = float(rate)
    return out


def normalize_all(
    conn: sqlite3.Connection,
    *,
    user_id: int = 1,
    rebuild: bool = False,
    tolerance_s: int = DEFAULT_TOLERANCE_S,
) -> dict[str, int]:
    derived_at = _utcnow_iso()
    if rebuild:
        conn.execute("DELETE FROM recovery")
        conn.execute("DELETE FROM workout")
        conn.execute("DELETE FROM weight_measurement")

    offsets = _cycle_offsets(conn)
    resp_rates = _sleep_resp_rates(conn)

    n_rec = 0
    for r in conn.execute(
        "SELECT id, payload FROM raw_events WHERE source='whoop_api' AND record_type='recovery'"
    ).fetchall():
        payload = json.loads(r["payload"])
        offset = offsets.get(payload.get("cycle_id"))
        sleep_id = payload.get("sleep_id")
        rate = resp_rates.get(str(sleep_id)) if sleep_id is not None else None
        row = parse_recovery(payload, tz_offset=offset, resp_rate=rate, user_id=user_id)
        if row is None:
            continue
        upsert_recovery(conn, row, raw_ref=r["id"], derived_at=derived_at)
        n_rec += 1

    n_wk = 0
    for r in conn.execute(
        "SELECT id, payload FROM raw_events WHERE source='whoop_api' AND record_type='workout'"
    ).fetchall():
        payload = json.loads(r["payload"])
        wrow = parse_workout(payload, user_id=user_id)
        upsert_workout(conn, wrow, raw_ref=r["id"], derived_at=derived_at)
        n_wk += 1

    n_wt = _normalize_healthkit_weight(conn, user_id, derived_at)

    n_groups = _regroup_workouts(conn, tolerance_s)
    conn.commit()
    return {
        "recovery": n_rec,
        "workout": n_wk,
        "weight": n_wt,
        "workout_groups": n_groups,
    }


def _normalize_healthkit_weight(
    conn: sqlite3.Connection, user_id: int, derived_at: str
) -> int:
    """Derive weight_measurement rows from raw HealthKit body records.

    One canonical row per raw body ``<Record>`` (1:1 raw_ref, §2.1). BMI and
    missing-value records parse to None and are skipped (§2.7).
    """
    n = 0
    for r in conn.execute(
        "SELECT id, external_id, payload FROM raw_events WHERE source='healthkit'"
    ).fetchall():
        payload = json.loads(r["payload"])
        partial = parse_body_record(payload, user_id=user_id)
        if partial is None:
            continue
        upsert_weight(
            conn,
            partial,
            source="healthkit",
            raw_ref=r["id"],
            external_id=r["external_id"],
            derived_at=derived_at,
        )
        n += 1
    return n


def _regroup_workouts(conn: sqlite3.Connection, tolerance_s: int) -> int:
    slots = [
        WkSlot(
            id=r["id"],
            user_id=r["user_id"],
            sport_type=r["sport_type"],
            start_at=r["start_at"],
            end_at=r["end_at"],
        )
        for r in conn.execute("SELECT id, user_id, sport_type, start_at, end_at FROM workout")
    ]
    mapping = assign_session_groups(slots, tolerance_s)
    for wid, gid in mapping.items():
        conn.execute(
            "UPDATE workout SET session_group_id=?, dedupe_hash=? WHERE id=?",
            (gid, gid, wid),
        )
    return len(set(mapping.values()))
