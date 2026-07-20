"""Fetch WHOOP data and write it verbatim to ``raw_events`` (T2.3).

We ingest more than we currently normalize (cycles, sleep, body): raw is
sacred and cheap, cycles supply recovery's timezone offset, and sleep/body are
future slices. Idempotent — re-running a window inserts no duplicates.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

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
    """Derive an incremental ``since`` from the newest ingested WHOOP record.

    Backs off ``overlap_days`` so late-arriving edits inside the window are
    re-fetched — overlap is free because ingest dedups on payload_hash. Returns
    None when no WHOOP data exists yet (caller must demand an explicit
    ``--since`` rather than silently guessing a backfill window).
    """
    from datetime import timedelta

    from ...timeutil import parse_instant, to_utc_iso

    row = conn.execute(
        "SELECT MAX(recorded_at) AS m FROM raw_events WHERE source='whoop_api'"
    ).fetchone()
    if row is None or row["m"] is None:
        return None
    return to_utc_iso(parse_instant(row["m"]) - timedelta(days=overlap_days))


def ingest_whoop(
    conn: sqlite3.Connection,
    client: WhoopClient,
    *,
    since: str,
    until: str | None = None,
    user_id: int = 1,
) -> dict[str, dict[str, int]]:
    """Ingest recovery, cycles, sleep, workouts, and body measurement.

    Returns ``{record_type: {"inserted": n, "skipped": n}}``.
    """
    result: dict[str, dict[str, int]] = {}

    plan = [
        ("recovery", client.get_recovery(since, until), "sleep_id", "created_at"),
        ("cycle", client.get_cycles(since, until), "id", "start"),
        ("sleep", client.get_sleep(since, until), "id", "end"),
        ("workout", client.get_workouts(since, until), "id", "start"),
    ]
    for record_type, records, id_key, time_key in plan:
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
