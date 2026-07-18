"""Normalize orchestrator: raw_events -> canonical recovery + workout.

This is the impure boundary (it does I/O). The actual raw->canonical mapping is
delegated to the pure functions in :mod:`coach.normalize.whoop`. ``--rebuild``
drops canonical and fully re-derives from raw (§2.1).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ..store.canonical import upsert_recovery, upsert_workout
from .dedup import DEFAULT_TOLERANCE_S, WkSlot, assign_session_groups
from .whoop import parse_recovery, parse_workout


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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

    offsets = _cycle_offsets(conn)

    n_rec = 0
    for r in conn.execute(
        "SELECT id, payload FROM raw_events WHERE source='whoop_api' AND record_type='recovery'"
    ).fetchall():
        payload = json.loads(r["payload"])
        offset = offsets.get(payload.get("cycle_id"))
        row = parse_recovery(payload, tz_offset=offset, user_id=user_id)
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

    n_groups = _regroup_workouts(conn, tolerance_s)
    conn.commit()
    return {"recovery": n_rec, "workout": n_wk, "workout_groups": n_groups}


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
