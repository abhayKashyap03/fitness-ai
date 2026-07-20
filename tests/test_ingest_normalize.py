"""Raw ingest idempotency, normalize end-to-end, rebuild determinism.

Uses REAL (scrubbed) WHOOP fixtures. See tests/fixtures/whoop/README.md.
"""

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
    """Serves recorded fixture payloads (no live calls)."""

    def get_recovery(self, start=None, end=None):
        return _load("recovery_page1.json")["records"] + _load("recovery_page2.json")["records"]

    def get_cycles(self, start=None, end=None):
        # real cycles supply the timezone offset for each recovery
        return _load("cycle_page1.json")["records"]

    def get_sleep(self, start=None, end=None):
        # synthetic sleeps whose ids match the real recoveries' sleep_id
        return _load("sleep_page1.synthetic.json")["records"]

    def get_workouts(self, start=None, end=None):
        return _load("workout_page1.json")["records"]

    def get_body_measurement(self):
        return _load("body_measurement.json")


# ---- ingest ----------------------------------------------------------------


def test_ingest_writes_raw_and_is_idempotent(migrated_conn):
    conn = migrated_conn
    r1 = ingest_whoop(conn, FakeClient(), since="2026-06-01T00:00:00.000Z")
    # raw is sacred: all 3 recovery records stored (incl. the PENDING one)
    assert r1["recovery"]["inserted"] == 3
    assert r1["workout"]["inserted"] == 2
    n_first = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]

    # re-ingest the same window: nothing new
    r2 = ingest_whoop(conn, FakeClient(), since="2026-06-01T00:00:00.000Z")
    assert r2["recovery"]["inserted"] == 0
    assert r2["recovery"]["skipped"] == 3
    n_second = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
    assert n_first == n_second


# ---- normalize -------------------------------------------------------------


def _seed(conn):
    ingest_whoop(conn, FakeClient(), since="2026-06-01T00:00:00.000Z")


def test_normalize_populates_canonical(migrated_conn):
    _seed(migrated_conn)
    counts = normalize_all(migrated_conn)
    # 3 recovery records but one is PENDING -> 2 canonical recovery rows
    assert counts["recovery"] == 2
    assert counts["workout"] == 2
    n_rec = migrated_conn.execute("SELECT COUNT(*) FROM recovery").fetchone()[0]
    assert n_rec == 2
    # recovery day_key uses the cycle's offset; offset stored in utc_offset,
    # tz_name stays NULL (IANA-only) per §2.6
    row = migrated_conn.execute(
        "SELECT day_key, tz_name, utc_offset FROM recovery WHERE score=37.0"
    ).fetchone()
    assert row["day_key"] == "2026-06-01"
    assert row["utc_offset"] == "-10:00"
    assert row["tz_name"] is None


def test_resp_rate_joined_from_sleep(migrated_conn):
    """Respiratory rate lives on the sleep record; joined into recovery by sleep_id."""
    _seed(migrated_conn)
    normalize_all(migrated_conn)
    rows = {
        r["score"]: r["resp_rate_bpm"]
        for r in migrated_conn.execute("SELECT score, resp_rate_bpm FROM recovery")
    }
    assert rows[37.0] == 16.25
    assert rows[52.0] == 15.8


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
    # the Hawaii walk (06:11Z at -10:00) lands on the local previous day 05-31;
    # the swim (17:43Z at -10:00) is same local day 06-02
    assert days == {"2026-05-31", "2026-06-02"}
