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
from .exercise import parse_exercise_log
from .foods import parse_food_log
from .healthkit import parse_body_record
from .myfitnesspal import parse_diary, parse_exercise, parse_measurement
from .whoop import parse_recovery, parse_sleep, parse_workout
from .whoop_ble import parse_ble_session


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

    # Adapter B sibling rows (ADR-0012). Same `recovery` table, different
    # source — the read-time resolver decides which one wins, and the objective
    # measurements from both stay comparable for calibration (§5).
    n_ble = 0
    for r in conn.execute(
        "SELECT id, payload FROM raw_events WHERE source='whoop_ble' AND record_type='hr_session'"
    ).fetchall():
        brow = parse_ble_session(json.loads(r["payload"]), user_id=user_id)
        if brow is None:
            continue  # a session that measured nothing stays absent (§2.7)
        upsert_recovery(conn, brow, raw_ref=r["id"], derived_at=derived_at)
        n_ble += 1

    # Hand-logged exercise (P12). Same rule as own-logged food: `workout` is a
    # raw-derived table the rebuild owns, so the raw event has to be sufficient
    # to reproduce the row rather than merely record that it happened.
    n_own_ex = 0
    for r in conn.execute(
        "SELECT id, payload FROM raw_events WHERE record_type='exercise_log'"
    ).fetchall():
        exrow = parse_exercise_log(json.loads(r["payload"]), user_id=user_id)
        if exrow is None:
            continue
        upsert_workout(conn, exrow, raw_ref=r["id"], derived_at=derived_at)
        n_own_ex += 1

    # Own-logged food (P12). These are USER-AUTHORED but still raw-derived: the
    # raw event holds the product AND the portion, so a rebuild reproduces the
    # canonical row exactly rather than losing the meal (§2.1).
    n_own_food = 0
    for r in conn.execute(
        "SELECT id, payload FROM raw_events WHERE record_type='food_log'"
    ).fetchall():
        frow = parse_food_log(json.loads(r["payload"]))
        if frow is None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO food_entry (id, user_id, day_key, source, entry_type, "
            "consumed_at, tz_name, description, quantity, unit, kcal, protein_g, carbs_g, "
            "fat_g, fiber_g, alcohol_g, raw_ref, derived_at) "
            "VALUES (?,?,?,?,'item',?,?,?,?,'g',?,?,?,?,?,NULL,?,?)",
            (
                frow.entry_id,
                user_id,
                frow.day_key,
                frow.source,
                frow.consumed_at,
                frow.tz_name,
                frow.description,
                frow.grams,
                frow.kcal,
                frow.protein_g,
                frow.carbs_g,
                frow.fat_g,
                frow.fiber_g,
                r["id"],
                derived_at,
            ),
        )
        n_own_food += 1

    n_wt, n_wt_skipped = _normalize_healthkit_weight(conn, user_id, derived_at)
    n_food = _normalize_mfp_food(conn, user_id, derived_at)
    n_mfp_wt = _normalize_mfp_weight(conn, user_id, derived_at)
    n_mfp_wk = _normalize_mfp_workout(conn, user_id, derived_at)

    n_groups = _regroup_workouts(conn, tolerance_s)
    conn.commit()
    return {
        "recovery": n_rec,
        "recovery_ble": n_ble,
        "food_own": n_own_food,
        "exercise_own": n_own_ex,
        "workout": n_wk,
        "sleep": n_slp,
        "weight": n_wt,
        "weight_skipped": n_wt_skipped,
        "mfp_weight": n_mfp_wt,
        "mfp_workout": n_mfp_wk,
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


def _normalize_mfp_workout(conn: sqlite3.Connection, user_id: int, derived_at: str) -> int:
    """Derive workout rows from `exercise_entry` items in raw MFP diaries.

    MFP is a real training source, not just food: a walk logged there must reach
    `workout` or it is invisible to status, the dashboard and the coach.

    Same newest-version-wins rule as food (a re-logged day supersedes its
    earlier snapshot), and the MFP slice is cleared first so an exercise deleted
    in the app doesn't linger. Only source='myfitnesspal' rows are touched —
    WHOOP siblings are left alone (§2.3); `_regroup_workouts` then groups a
    session seen by both sources so compute counts it once (T2.6).
    """
    conn.execute("DELETE FROM workout WHERE source='myfitnesspal'")

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
        for row in parse_exercise(record, user_id=user_id):
            upsert_workout(conn, row, raw_ref=r["id"], derived_at=derived_at)
            n += 1
    return n


def regroup_workouts(conn: sqlite3.Connection, tolerance_s: int = DEFAULT_TOLERANCE_S) -> int:
    """Public alias — the write paths need this too, not just the rebuild.

    A hand-logged session that leaves session_group_id unset produces different
    canonical state from the same raw than a rebuild does, and until the next
    normalize a workout the strap also caught would be counted twice (§5).
    """
    return _regroup_workouts(conn, tolerance_s)


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
    mapping = _absorb_placeholder_time_sources(conn, mapping)
    for wid, gid in mapping.items():
        conn.execute(
            "UPDATE workout SET session_group_id=?, dedupe_hash=? WHERE id=?",
            (gid, gid, wid),
        )
    return len(set(mapping.values()))


# Sources whose exercise timestamps are entered by hand (or defaulted by the
# app) rather than measured. MyFitnessPal repeats the same clock time across
# months — e.g. every logged walk at 08:30 — so a start-time tolerance window
# can NEVER match one against the strap's measured instant.
_PLACEHOLDER_TIME_SOURCES = ("myfitnesspal",)


def _absorb_placeholder_time_sources(
    conn: sqlite3.Connection, mapping: dict[str, str]
) -> dict[str, str]:
    """Fold hand-logged sessions into a measured session of the same day+sport.

    Time-window dedup (T2.6) assumes both sides carry a real instant. They
    don't: the same session logged in MFP and detected by WHOOP lands hours
    apart, so both survive as separate groups and compute counts the workout —
    and its calories — TWICE. That is the exact cross-source double-count
    CLAUDE.md §5 calls an expected bug class.

    So for each (user, day, sport) where a strap-detected session exists, the
    hand-logged rows join that group — grouping only, which source's numbers get
    reported is decided at READ time by the resolver's ranking (§2.3), where
    MyFitnessPal outranks WHOOP for training.

    Deliberate trade-off: two genuinely distinct same-sport sessions on one day —
    one strap-detected, one hand-logged — collapse into one. Under-counting one
    session is the safer error; double-counting inflates burn and would flatter
    the cut.
    """
    rows = list(conn.execute("SELECT id, user_id, day_key, sport_type, source FROM workout"))
    measured_group: dict[tuple[int, str, str], str] = {}
    for r in rows:
        if r["source"] in _PLACEHOLDER_TIME_SOURCES:
            continue
        key = (r["user_id"], r["day_key"], r["sport_type"])
        gid = mapping.get(r["id"])
        if gid is not None:
            measured_group.setdefault(key, gid)

    if not measured_group:
        return mapping

    merged = dict(mapping)
    for r in rows:
        if r["source"] not in _PLACEHOLDER_TIME_SOURCES:
            continue
        gid = measured_group.get((r["user_id"], r["day_key"], r["sport_type"]))
        if gid is not None:
            merged[r["id"]] = gid
    return merged
