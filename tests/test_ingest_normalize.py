"""Raw ingest idempotency, normalize end-to-end, rebuild determinism, resolver."""

from __future__ import annotations

import json
from pathlib import Path

from coach.adapters.whoop.ingest import ingest_whoop
from coach.normalize.runner import normalize_all
from coach.store.canonical import canonical_fingerprint

FIX = Path(__file__).parent / "fixtures" / "whoop"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


class FakeClient:
    """Serves fixture pages; records call counts to prove no live calls."""

    def get_recovery(self, start=None, end=None):
        return _load("recovery_page1.json")["records"] + _load("recovery_page2.json")["records"]

    def get_cycles(self, start=None, end=None):
        # two cycles supplying timezone offsets for the recovery records
        return [
            {"id": 93845, "start": "2026-07-10T04:00:00Z", "timezone_offset": "-04:00"},
            {"id": 93847, "start": "2026-07-12T04:00:00Z", "timezone_offset": "-04:00"},
        ]

    def get_sleep(self, start=None, end=None):
        return []

    def get_workouts(self, start=None, end=None):
        return _load("workout_page1.json")["records"]

    def get_body_measurement(self):
        return _load("body_measurement.json")


# ---- ingest ----------------------------------------------------------------


def test_ingest_writes_raw_and_is_idempotent(migrated_conn):
    conn = migrated_conn
    r1 = ingest_whoop(conn, FakeClient(), since="2026-07-10")
    # raw is sacred: all 3 recovery records stored (incl. the PENDING one)
    assert r1["recovery"]["inserted"] == 3
    assert r1["workout"]["inserted"] == 2
    n_first = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]

    # re-ingest the same window: nothing new
    r2 = ingest_whoop(conn, FakeClient(), since="2026-07-10")
    assert r2["recovery"]["inserted"] == 0
    assert r2["recovery"]["skipped"] == 3
    n_second = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
    assert n_first == n_second


# ---- normalize -------------------------------------------------------------


def _seed(conn):
    ingest_whoop(conn, FakeClient(), since="2026-07-10")


def test_normalize_populates_canonical(migrated_conn):
    _seed(migrated_conn)
    counts = normalize_all(migrated_conn)
    # 3 recovery records but one is PENDING -> 2 canonical recovery rows
    assert counts["recovery"] == 2
    assert counts["workout"] == 2
    n_rec = migrated_conn.execute("SELECT COUNT(*) FROM recovery").fetchone()[0]
    assert n_rec == 2
    # recovery day_key uses the cycle's offset
    row = migrated_conn.execute("SELECT day_key, tz_name FROM recovery WHERE score=66").fetchone()
    assert row["day_key"] == "2026-07-10"
    assert row["tz_name"] == "-04:00"


def test_normalize_idempotent_and_rebuild_identical(migrated_conn):
    _seed(migrated_conn)
    normalize_all(migrated_conn)
    fp1 = canonical_fingerprint(migrated_conn)
    # re-run incremental: identical
    normalize_all(migrated_conn)
    assert canonical_fingerprint(migrated_conn) == fp1
    # full rebuild: byte-identical canonical output
    normalize_all(migrated_conn, rebuild=True)
    assert canonical_fingerprint(migrated_conn) == fp1


def test_workout_day_boundary_persisted(migrated_conn):
    _seed(migrated_conn)
    normalize_all(migrated_conn)
    days = {r["day_key"] for r in migrated_conn.execute("SELECT day_key FROM workout")}
    assert days == {"2026-07-10", "2026-07-09"}  # the -05:00 run lands on the 9th
