"""HealthKit weight: raw ingest + normalize + resolver, end to end (T5.3-T5.7)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coach.adapters.healthkit.ingest import healthkit_external_id, ingest_healthkit
from coach.adapters.healthkit.parser import HKRecord
from coach.normalize.healthkit import LB_TO_KG
from coach.normalize.runner import normalize_all
from coach.store.canonical import canonical_fingerprint

FIX = Path(__file__).parent / "fixtures" / "healthkit" / "export_sample.xml"


# ---- migration 0005 --------------------------------------------------------


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_adds_source_app_and_utc_offset(migrated_conn):
    for table in ("weight_measurement", "food_entry"):
        cols = _cols(migrated_conn, table)
        assert "source_app" in cols
        assert "utc_offset" in cols


# ---- T5.3 raw ingest -------------------------------------------------------


def test_ingest_body_only_skips_dietary(migrated_conn):
    res = ingest_healthkit(migrated_conn, FIX)
    # fixture has 3 body records (OKOK mass, OKOK fat, MFP mass); dietary skipped
    assert res == {"inserted": 3, "skipped": 0}
    rows = migrated_conn.execute(
        "SELECT DISTINCT record_type FROM raw_events WHERE source='healthkit'"
    ).fetchall()
    types = {r["record_type"] for r in rows}
    assert all(t.startswith("HKQuantityTypeIdentifier") for t in types)
    assert not any("Dietary" in t for t in types)


def test_ingest_is_idempotent(migrated_conn):
    ingest_healthkit(migrated_conn, FIX)
    before = migrated_conn.execute("SELECT COUNT(*) c FROM raw_events").fetchone()["c"]
    res2 = ingest_healthkit(migrated_conn, FIX)
    after = migrated_conn.execute("SELECT COUNT(*) c FROM raw_events").fetchone()["c"]
    assert res2 == {"inserted": 0, "skipped": 3}
    assert before == after


def test_external_id_deterministic():
    rec = HKRecord(
        type="HKQuantityTypeIdentifierBodyMass",
        source_name="OKOK·International Version",
        unit="lb",
        value=183.5,
        start_date="2026-01-02 07:00:00 -0500",
        end_date=None,
        creation_date=None,
        metadata={},
    )
    assert healthkit_external_id(rec) == healthkit_external_id(rec)
    assert healthkit_external_id(rec).startswith("hk:")


# ---- T5.4-T5.7 normalize + resolve -----------------------------------------


def test_normalize_produces_weight_rows(migrated_conn):
    ingest_healthkit(migrated_conn, FIX)
    counts = normalize_all(migrated_conn)
    # 3 body records -> 3 canonical rows (OKOK weight, OKOK fat, MFP weight)
    assert counts["weight"] == 3
    okok = migrated_conn.execute(
        "SELECT weight_kg FROM weight_measurement "
        "WHERE source_app='okok' AND weight_kg IS NOT NULL"
    ).fetchone()
    assert okok["weight_kg"] == pytest.approx(183.5 * LB_TO_KG, abs=1e-4)


def test_resolver_prefers_okok_scale_over_mfp_weight(migrated_conn):
    # OKOK scale (183.5 lb) and MFP-mirrored weight (184.0 lb) same day -> scale wins
    ingest_healthkit(migrated_conn, FIX)
    normalize_all(migrated_conn)
    r = migrated_conn.execute(
        "SELECT source_app, weight_kg FROM weight_resolved_daily WHERE day_key='2026-01-02'"
    ).fetchone()
    assert r["source_app"] == "okok"
    assert r["weight_kg"] == pytest.approx(183.5 * LB_TO_KG, abs=1e-4)


def test_weight_trend_seeds_from_resolved(migrated_conn):
    ingest_healthkit(migrated_conn, FIX)
    normalize_all(migrated_conn)
    t = migrated_conn.execute(
        "SELECT trend_kg FROM weight_trend WHERE day_key='2026-01-02'"
    ).fetchone()
    # single point: trend seeds at the value itself
    assert t["trend_kg"] == pytest.approx(183.5 * LB_TO_KG, abs=1e-3)


def test_rebuild_is_byte_identical(migrated_conn):
    ingest_healthkit(migrated_conn, FIX)
    normalize_all(migrated_conn)
    fp1 = canonical_fingerprint(migrated_conn)
    normalize_all(migrated_conn, rebuild=True)
    fp2 = canonical_fingerprint(migrated_conn)
    assert fp1 == fp2
