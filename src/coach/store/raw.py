"""Raw ingestion: write source payloads verbatim, append-only, idempotent.

§2.1 — raw is sacred. Payloads are stored exactly as received. Idempotency is by
``(source, external_id, payload_hash)``: re-ingesting the same window inserts
nothing new. The canonical layer never reads a vendor field except through a
normalizer.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def payload_hash(source: str, external_id: str | None, payload_json: str) -> str:
    h = hashlib.sha256()
    h.update(source.encode())
    h.update(b"\x00")
    h.update((external_id or "").encode())
    h.update(b"\x00")
    h.update(payload_json.encode())
    return h.hexdigest()


def canonical_json(payload: dict) -> str:
    """Deterministic serialization so identical payloads hash identically."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def insert_raw_event(
    conn: sqlite3.Connection,
    *,
    source: str,
    record_type: str,
    payload: dict,
    external_id: str | None = None,
    recorded_at: str | None = None,
    ingested_at: str | None = None,
    user_id: int = 1,
) -> tuple[str, bool]:
    """Insert one raw event. Returns ``(row_id, inserted)``.

    ``inserted`` is False when an identical payload already existed (dedup hit).
    """
    pj = canonical_json(payload)
    phash = payload_hash(source, external_id, pj)

    existing = conn.execute(
        "SELECT id FROM raw_events WHERE source=? AND external_id IS ? AND payload_hash=?",
        (source, external_id, phash),
    ).fetchone()
    if existing:
        return existing["id"], False

    row_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO raw_events (id, user_id, source, record_type, external_id, "
        "recorded_at, ingested_at, payload, payload_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row_id,
            user_id,
            source,
            record_type,
            external_id,
            recorded_at,
            ingested_at or _utcnow_iso(),
            pj,
            phash,
        ),
    )
    conn.commit()
    return row_id, True
