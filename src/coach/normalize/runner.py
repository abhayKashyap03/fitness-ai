"""Normalize orchestrator: raw_events -> canonical recovery + workout.

This is the impure boundary (it does I/O). The actual raw->canonical mapping is
delegated to the pure functions in :mod:`coach.normalize.whoop`. ``--rebuild``
drops canonical and fully re-derives from raw (§2.1).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ..store.canonical import (
    upsert_food,
    upsert_recovery,
    upsert_sleep,
    upsert_weight,
    upsert_workout,
)
from .dedup import DEFAULT_TOLERANCE_S, WkSlot, assign_session_groups
from .healthkit import parse_body_record
from .myfitnesspal import parse_diary, parse_measurement
from .whoop import parse_recovery, parse_sleep, parse_workout


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
    # sibling raw versions of the same sleep (edited payloads) can coexist;
    # order by ingest time so the NEWEST ingested version wins deterministically
    for r in conn.execute(
        "SELECT payload FROM raw_events WHERE source='whoop_api' AND record_type='sleep' "
        "ORDER BY ingested_at, id"
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
        conn.execute("DELETE FROM food_entry")
        conn.execute("DELETE FROM sleep")

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

    n_slp = 0
    for r in conn.execute(
        "SELECT id, payload FROM raw_events WHERE source='whoop_api' AND record_type='sleep'"
    ).fetchall():
        srow = parse_sleep(json.loads(r["payload"]), user_id=user_id)
        if srow is None:
            continue  # unscored/no-id sleeps stay absent (§2.7)
        upsert_sleep(conn, srow, raw_ref=r["id"], derived_at=derived_at)
        n_slp += 1

    n_wt, n_wt_skipped = _normalize_healthkit_weight(conn, user_id, derived_at)
    n_food = _normalize_mfp_food(conn, user_id, derived_at)
    n_mfp_wt = _normalize_mfp_weight(conn, user_id, derived_at)

    n_groups = _regroup_workouts(conn, tolerance_s)
    conn.commit()
    return {
        "recovery": n_rec,
        "workout": n_wk,
        "sleep": n_slp,
        "weight": n_wt,
        "weight_skipped": n_wt_skipped,
        "mfp_weight": n_mfp_wt,
        "food": n_food,
        "workout_groups": n_groups,
    }


def _normalize_healthkit_weight(
    conn: sqlite3.Connection, user_id: int, derived_at: str
) -> tuple[int, int]:
    """Derive weight_measurement rows from raw HealthKit body records.

    One canonical row per raw body ``<Record>`` (1:1 raw_ref, §2.1). BMI,
    missing-value, and unknown-unit records parse to None and are skipped
    (§2.7) — the skip COUNT is returned and surfaced so canonical can never
    silently shrink on a rebuild without it showing in the totals.
    """
    n = skipped = 0
    for r in conn.execute(
        "SELECT id, external_id, payload FROM raw_events WHERE source='healthkit'"
    ).fetchall():
        payload = json.loads(r["payload"])
        partial = parse_body_record(payload, user_id=user_id)
        if partial is None:
            skipped += 1
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
    return n, skipped


def _normalize_mfp_food(conn: sqlite3.Connection, user_id: int, derived_at: str) -> int:
    """Derive food_entry rows from raw MyFitnessPal diary events.

    A day edited in MFP produces sibling raw rows sharing one external_id
    (``mfp:diary:<day>``); the NEWEST ingested version wins, so a re-log
    replaces yesterday's snapshot rather than double-counting (§2.3). Returns
    the number of food items written.
    """
    # A diary day is a COLLECTION whose membership changes on edit, so stale
    # items would orphan under INSERT-OR-REPLACE (unlike 1:1 weight/recovery
    # rows). The MFP slice is fully regenerable from raw, so clear + rebuild it
    # (no-op on a rebuild=True run — the table was already emptied). Scoped to
    # source='myfitnesspal' so HealthKit/other food siblings are untouched.
    conn.execute("DELETE FROM food_entry WHERE source='myfitnesspal'")

    # newest raw row per day (external_id). ingested_at is second-precision, so
    # two ingests in the same second tie — break on rowid, which is monotonic
    # with insertion order (a uuid id would tie randomly and could pick the
    # stale snapshot). Last writer in iteration order wins the dict slot.
    newest: dict[str, sqlite3.Row] = {}
    for r in conn.execute(
        "SELECT id, external_id, payload FROM raw_events "
        "WHERE source='myfitnesspal' AND record_type='diary' "
        "ORDER BY ingested_at, rowid"
    ).fetchall():
        newest[str(r["external_id"])] = r

    n = 0
    for r in newest.values():
        record = json.loads(r["payload"])
        for row in parse_diary(record, user_id=user_id):
            upsert_food(conn, row, raw_ref=r["id"], derived_at=derived_at)
            n += 1
    return n


def _normalize_mfp_weight(conn: sqlite3.Connection, user_id: int, derived_at: str) -> int:
    """Derive weight_measurement rows from raw MFP weight measurements.

    One weigh-in per day (external_id ``mfp:measurement:weight:<day>``); an
    edited day yields a sibling raw row, newest ingest wins (rowid tiebreak, as
    for food). Deterministic weight_id (1:1 raw_ref) means re-normalize
    overwrites in place — no stale rows to clear. source='myfitnesspal'.
    """
    newest: dict[str, sqlite3.Row] = {}
    for r in conn.execute(
        "SELECT id, external_id, payload FROM raw_events "
        "WHERE source='myfitnesspal' AND record_type='measurement' "
        "ORDER BY ingested_at, rowid"
    ).fetchall():
        newest[str(r["external_id"])] = r

    n = 0
    for r in newest.values():
        partial = parse_measurement(json.loads(r["payload"]), user_id=user_id)
        if partial is None:
            continue
        upsert_weight(
            conn,
            partial,
            source="myfitnesspal",
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
