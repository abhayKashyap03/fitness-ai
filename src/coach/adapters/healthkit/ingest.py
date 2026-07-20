"""Ingest Apple Health body-composition records into ``raw_events`` (T5.3).

Streams the export (memory-flat, via :mod:`coach.adapters.healthkit.parser`),
keeps only **body-composition** records, and writes each verbatim to
``raw_events`` with ``source='healthkit'`` (already in the CHECK — no rebuild of
the sacred raw table, per D3/ADR-0008). Idempotent: the deterministic
``external_id`` + payload hash means re-ingesting the same export inserts nothing.

**Dietary records are deliberately skipped here.** HealthKit is our weight/body
source only — MFP stopped writing food to Apple Health after Oct 2025, leaving
~5 unusable dietary days. Real nutrition comes from the MFP CSV adapter
(Phase 6); admitting the stale HealthKit food as a sibling would only pollute
provenance. (Raw is still regenerable from the export file itself if that ever
changes.)
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import IO

from ...store.raw import insert_raw_event
from . import BODY_TYPES
from .parser import HKRecord, iter_records

SOURCE = "healthkit"


def _is_body(record_type: str) -> bool:
    return record_type in BODY_TYPES


def healthkit_external_id(rec: HKRecord) -> str:
    """Deterministic id from ``(type, sourceName, startDate, value)``.

    Body records carry no ``HKExternalUUID`` (recon T5.1), so we derive a stable
    id ourselves. Deterministic => re-ingest dedups and ``--rebuild`` is exact.
    """
    key = f"{rec.type}|{rec.source_name}|{rec.start_date}|{rec.value}"
    return "hk:" + hashlib.sha256(key.encode()).hexdigest()[:32]


def ingest_healthkit(
    conn: sqlite3.Connection,
    source: str | Path | IO[bytes],
    *,
    user_id: int = 1,
) -> dict[str, int]:
    """Ingest body-composition records from an Apple Health export.

    ``source`` is a path to ``export.xml`` / ``export.zip`` or an open binary
    stream. Returns ``{"inserted": n, "skipped": n}``.
    """
    inserted = skipped = 0
    for rec in iter_records(source, wanted=_is_body):
        payload = asdict(rec)
        _, was_new = insert_raw_event(
            conn,
            source=SOURCE,
            record_type=rec.type,
            payload=payload,
            external_id=healthkit_external_id(rec),
            recorded_at=rec.start_date,
            user_id=user_id,
        )
        inserted += int(was_new)
        skipped += int(not was_new)
    return {"inserted": inserted, "skipped": skipped}
