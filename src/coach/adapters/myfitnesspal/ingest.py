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
from typing import TypedDict

from ...store.raw import insert_raw_event
from .client import MfpClient

SOURCE = "myfitnesspal"


class MfpIngestResult(TypedDict):
    diary: dict[str, int]
    weight: dict[str, int]
    days: int


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
) -> MfpIngestResult:
    """Ingest MFP diary + weight for ``[since, until]`` (inclusive, ``YYYY-MM-DD``).

    One raw event per day per kind: ``record_type='diary'`` (food) and
    ``record_type='measurement'`` (weight). Append-only + idempotent; an edited
    day writes a new sibling row resolved by newest ingest. Returns
    ``{"diary": {inserted, skipped}, "weight": {inserted, skipped}, "days": n}``.
    """
    diary_ins = diary_skip = wt_ins = wt_skip = 0
    days = _day_range(since, until)
    for day in days:
        # --- food diary ---
        diary = client.get_diary(day)
        _, new = insert_raw_event(
            conn,
            source=SOURCE,
            record_type="diary",
            # stamp the day so the normalizer never re-derives it from the
            # (reconciliation-risk) inner fields
            payload={"date": day, "diary": diary},
            external_id=f"mfp:diary:{day}",
            recorded_at=day,
            user_id=user_id,
        )
        diary_ins += int(new)
        diary_skip += int(not new)

        # --- weight measurement ---
        weight = client.get_weight(day)
        _, new = insert_raw_event(
            conn,
            source=SOURCE,
            record_type="measurement",
            payload={"date": day, "measurement": weight},
            external_id=f"mfp:measurement:weight:{day}",
            recorded_at=day,
            user_id=user_id,
        )
        wt_ins += int(new)
        wt_skip += int(not new)

    return {
        "diary": {"inserted": diary_ins, "skipped": diary_skip},
        "weight": {"inserted": wt_ins, "skipped": wt_skip},
        "days": len(days),
    }
