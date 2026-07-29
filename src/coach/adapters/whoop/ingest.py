"""Fetch WHOOP data and write it verbatim to ``raw_events`` (T2.3).

We ingest more than we currently normalize (cycles, sleep, body): raw is
sacred and cheap, cycles supply recovery's timezone offset, and sleep/body are
future slices. Idempotent — re-running a window inserts no duplicates.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping

from ...store.raw import insert_raw_event
from .client import WhoopClient


def _ingest_records(
    conn: sqlite3.Connection,
    records: Iterable[dict],
    *,
    record_type: str,
    id_key: str,
    time_key: str,
    user_id: int,
) -> tuple[int, int]:
    inserted = skipped = 0
    for rec in records:
        ext = rec.get(id_key)
        _, was_new = insert_raw_event(
            conn,
            source="whoop_api",
            record_type=record_type,
            payload=rec,
            external_id=str(ext) if ext is not None else None,
            recorded_at=rec.get(time_key),
            user_id=user_id,
        )
        inserted += int(was_new)
        skipped += int(not was_new)
    return inserted, skipped


def auto_since(conn: sqlite3.Connection, *, overlap_days: int = 2) -> str | None:
    """Derive an incremental ``since`` from already-ingested WHOOP records.

    Watermark = the MIN across record types of each type's newest
    ``recorded_at`` — a single global MAX could silently skip a type that an
    interrupted ingest never reached (recovery lands first; a crash before
    workouts would strand them past the watermark forever). Backs off
    ``overlap_days`` on top; overlap is free because ingest dedups on
    payload_hash. Returns None when no WHOOP data exists yet (caller must
    demand an explicit ``--since`` rather than silently guessing a backfill
    window). Emitted in RFC3339 ``Z`` form, matching WHOOP's documented format.
    """
    from datetime import timedelta

    from ...timeutil import parse_instant, to_utc_iso

    row = conn.execute(
        "SELECT MIN(m) AS watermark FROM ("
        "  SELECT MAX(recorded_at) AS m FROM raw_events"
        "  WHERE source='whoop_api' AND recorded_at IS NOT NULL"
        "  GROUP BY record_type"
        ")"
    ).fetchone()
    if row is None or row["watermark"] is None:
        return None
    since = to_utc_iso(parse_instant(row["watermark"]) - timedelta(days=overlap_days))
    return since.replace("+00:00", "Z")


def auto_since_by_type(conn: sqlite3.Connection, *, overlap_days: int = 2) -> dict[str, str]:
    """Per-record-type incremental watermarks.

    :func:`auto_since` takes the MIN across types so an interrupted ingest can't
    strand a type past the watermark. That is safe but pessimistic: a type with
    no NEW data (no workouts logged for a week) is indistinguishable from a type
    that was never fetched, so its stale watermark drags EVERY type's window
    back — re-fetching days that are already stored, forever.

    Resuming each type from its OWN newest record keeps the interrupted-ingest
    guarantee (a type that missed a run still resumes from where it really got
    to) without letting a quiet type hold the others back.
    """
    from datetime import timedelta

    from ...timeutil import parse_instant, to_utc_iso

    out: dict[str, str] = {}
    for row in conn.execute(
        "SELECT record_type, MAX(recorded_at) AS m FROM raw_events "
        "WHERE source='whoop_api' AND recorded_at IS NOT NULL "
        "GROUP BY record_type"
    ):
        watermark = row["m"]
        if not watermark:
            continue
        since = to_utc_iso(parse_instant(watermark) - timedelta(days=overlap_days))
        out[str(row["record_type"])] = since.replace("+00:00", "Z")
    return out


def ingest_whoop(
    conn: sqlite3.Connection,
    client: WhoopClient,
    *,
    since: str | Mapping[str, str],
    until: str | None = None,
    user_id: int = 1,
) -> dict[str, dict[str, int]]:
    """Ingest recovery, cycles, sleep, workouts, and body measurement.

    Returns ``{record_type: {"inserted": n, "skipped": n}}``.
    """
    result: dict[str, dict[str, int]] = {}

    def _since_for(record_type: str) -> str:
        """This type's window. A mapping resumes each type from its own
        watermark; a bare string applies one window to all (explicit backfill).
        Falls back to the earliest known window so a type absent from the
        mapping is re-fetched rather than silently skipped."""
        if isinstance(since, str):
            return since
        return since.get(record_type) or min(since.values())

    plan = [
        ("recovery", "sleep_id", "created_at"),
        ("cycle", "id", "start"),
        ("sleep", "id", "end"),
        ("workout", "id", "start"),
    ]
    fetch = {
        "recovery": client.get_recovery,
        "cycle": client.get_cycles,
        "sleep": client.get_sleep,
        "workout": client.get_workouts,
    }
    for record_type, id_key, time_key in plan:
        type_since = _since_for(record_type)
        records = fetch[record_type](type_since, until)
        ins, skip = _ingest_records(
            conn,
            records,
            record_type=record_type,
            id_key=id_key,
            time_key=time_key,
            user_id=user_id,
        )
        result[record_type] = {"inserted": ins, "skipped": skip}

    # body measurement is a single object
    body = client.get_body_measurement()
    _, was_new = insert_raw_event(
        conn,
        source="whoop_api",
        record_type="body_measurement",
        payload=body,
        external_id=None,
        user_id=user_id,
    )
    result["body_measurement"] = {"inserted": int(was_new), "skipped": int(not was_new)}
    return result
