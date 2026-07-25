"""MFP ingest -> normalize -> resolver, end to end on a migrated DB.

Uses a stub diary client (duck-typed for ingest_mfp; no network, §6.2). Proves
idempotency, edited-day newest-wins, canonical writes, and read-time precedence
(direct MFP outranks a HealthKit-mirrored copy of the same day).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from coach.adapters.myfitnesspal.ingest import auto_since, ingest_mfp
from coach.normalize.runner import normalize_all

FIX = Path(__file__).parent / "fixtures" / "myfitnesspal" / "diary_sample.json"


class FakeMfp:
    """Duck-typed MfpClient: returns a canned diary payload per day."""

    def __init__(self, by_day: dict[str, dict]):
        self.by_day = by_day
        self.calls: list[str] = []

    def get_diary(self, day: str) -> dict:
        self.calls.append(day)
        return self.by_day.get(day, {"items": []})


def _sample() -> dict:
    return json.loads(FIX.read_text())


def test_ingest_is_idempotent_and_covers_the_window(migrated_conn: sqlite3.Connection):
    client = FakeMfp({"2026-06-15": _sample()})
    r1 = ingest_mfp(migrated_conn, client, since="2026-06-14", until="2026-06-16")
    assert r1 == {"inserted": 3, "skipped": 0, "days": 3}
    # re-running the same window inserts nothing new (dedup on payload_hash)
    r2 = ingest_mfp(migrated_conn, client, since="2026-06-14", until="2026-06-16")
    assert r2["inserted"] == 0
    assert r2["skipped"] == 3


def test_normalize_writes_food_and_view_resolves_mfp(migrated_conn: sqlite3.Connection):
    ingest_mfp(migrated_conn, FakeMfp({"2026-06-15": _sample()}), since="2026-06-15", until="2026-06-15")
    counts = normalize_all(migrated_conn)
    assert counts["food"] == 4

    # per-source rollup: MFP present, totals summed, kcal complete
    row = migrated_conn.execute(
        "SELECT source, kcal_total, is_complete FROM food_daily_by_source "
        "WHERE day_key='2026-06-15' AND source='myfitnesspal'"
    ).fetchone()
    assert row is not None
    assert row["kcal_total"] == 688.0  # 150 + 248 + 200 + 90
    assert row["is_complete"] == 1

    # the day's authoritative source is myfitnesspal
    picked = migrated_conn.execute(
        "SELECT source FROM food_daily WHERE day_key='2026-06-15'"
    ).fetchone()
    assert picked["source"] == "myfitnesspal"


def test_direct_mfp_outranks_healthkit_mirror(migrated_conn: sqlite3.Connection):
    ingest_mfp(migrated_conn, FakeMfp({"2026-06-15": _sample()}), since="2026-06-15", until="2026-06-15")
    normalize_all(migrated_conn)
    # a HealthKit-mirrored MFP copy of the same day (source_app='myfitnesspal')
    migrated_conn.execute(
        "INSERT INTO food_entry (id, user_id, day_key, source, source_app, entry_type, "
        "kcal, derived_at) VALUES ('food:mirror:1', 1, '2026-06-15', 'healthkit', "
        "'myfitnesspal', 'item', 700.0, '2026-01-01T00:00:00+00:00')"
    )
    migrated_conn.commit()
    picked = migrated_conn.execute(
        "SELECT source FROM food_daily WHERE day_key='2026-06-15'"
    ).fetchone()
    assert picked["source"] == "myfitnesspal"  # direct beats mirror


def test_edited_day_newest_ingest_wins(migrated_conn: sqlite3.Connection):
    # first log: full 4-item day
    ingest_mfp(migrated_conn, FakeMfp({"2026-06-15": _sample()}), since="2026-06-15", until="2026-06-15")
    # later the user edits the day down to a single item
    edited = {"items": [{"id": "999", "meal_name": "Breakfast",
                         "food": {"description": "Just coffee"},
                         "nutritional_contents": {"energy": {"value": 5, "unit": "calories"}}}]}
    ingest_mfp(migrated_conn, FakeMfp({"2026-06-15": edited}), since="2026-06-15", until="2026-06-15")
    normalize_all(migrated_conn)

    rows = migrated_conn.execute(
        "SELECT description, kcal FROM food_entry WHERE day_key='2026-06-15'"
    ).fetchall()
    # newest snapshot wins: one item, not five
    assert len(rows) == 1
    assert rows[0]["description"] == "Just coffee"
    assert rows[0]["kcal"] == 5.0


def test_auto_since_from_watermark(migrated_conn: sqlite3.Connection):
    assert auto_since(migrated_conn) is None  # nothing ingested yet
    ingest_mfp(migrated_conn, FakeMfp({}), since="2026-06-10", until="2026-06-12")
    # newest day 2026-06-12, backed off 3 days -> 2026-06-09
    assert auto_since(migrated_conn) == "2026-06-09"


def test_rebuild_is_byte_identical(migrated_conn: sqlite3.Connection):
    from coach.store.canonical import canonical_fingerprint

    ingest_mfp(migrated_conn, FakeMfp({"2026-06-15": _sample()}), since="2026-06-15", until="2026-06-15")
    normalize_all(migrated_conn)
    fp1 = canonical_fingerprint(migrated_conn)
    normalize_all(migrated_conn, rebuild=True)
    fp2 = canonical_fingerprint(migrated_conn)
    assert fp1 == fp2  # §2.1: canonical fully regenerable from raw
