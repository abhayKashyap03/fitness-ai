"""Fetch MyFitnessPal diary days and write them verbatim to ``raw_events``.

MFP's diary is per-day, so we ingest one raw event per day (record_type
``diary``), append-only and idempotent: an unchanged day re-ingests to the same
payload_hash and inserts nothing; an EDITED day writes a new sibling row (§2.3)
that the normalizer resolves by newest ingest. raw is sacred (§2.1) — no
field is touched here.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from ...store.raw import insert_raw_event
from .client import MfpClient

SOURCE = "myfitnesspal"


def _day_range(since: str, until: str) -> list[str]:
    d0 = date.fromisoformat(since)
    d1 = date.fromisoformat(until)
    if d1 < d0:
        return []
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def auto_since(conn: sqlite3.Connection, *, overlap_days: int = 3) -> str | None:
    """Derive an incremental ``since`` from the newest MFP diary day already stored.

    Watermark = MAX day_key ingested, backed off ``overlap_days`` (recent days
    are the ones most likely still being edited; overlap is free — ingest dedups
    on payload_hash). Returns None when no MFP data exists yet, so the caller
    demands an explicit ``--since`` rather than guessing a backfill window.
    """
    row = conn.execute(
        "SELECT MAX(external_id) AS last FROM raw_events "
        "WHERE source=? AND record_type='diary'",
        (SOURCE,),
    ).fetchone()
    if row is None or row["last"] is None:
        return None
    # external_id is "mfp:diary:YYYY-MM-DD" — the day is the trailing token.
    last_day = str(row["last"]).rsplit(":", 1)[-1]
    try:
        anchor = date.fromisoformat(last_day)
    except ValueError:
        return None
    return (anchor - timedelta(days=overlap_days)).isoformat()


def ingest_mfp(
    conn: sqlite3.Connection,
    client: MfpClient,
    *,
    since: str,
    until: str,
    user_id: int = 1,
) -> dict[str, int]:
    """Ingest MFP diary days in ``[since, until]`` (inclusive, ``YYYY-MM-DD``).

    Returns ``{"inserted": n, "skipped": n, "days": n}``.
    """
    inserted = skipped = 0
    days = _day_range(since, until)
    for day in days:
        payload = client.get_diary(day)
        # Stamp the day onto the stored payload so the normalizer never has to
        # re-derive it from the (reconciliation-risk) inner fields.
        record = {"date": day, "diary": payload}
        _, was_new = insert_raw_event(
            conn,
            source=SOURCE,
            record_type="diary",
            payload=record,
            external_id=f"mfp:diary:{day}",
            recorded_at=day,
            user_id=user_id,
        )
        inserted += int(was_new)
        skipped += int(not was_new)
    return {"inserted": inserted, "skipped": skipped, "days": len(days)}
