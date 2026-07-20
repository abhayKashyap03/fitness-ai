"""Backup/verify maintenance + incremental auto-since + CLI surface tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from coach.adapters.whoop.ingest import auto_since
from coach.cli.main import build_parser
from coach.store.maintenance import backup_db, verify_db
from coach.store.raw import insert_raw_event

FIXHK = Path(__file__).parent / "fixtures" / "healthkit" / "export_sample.xml"


# ---- backup ----------------------------------------------------------------


def test_backup_creates_snapshot_with_data(migrated_conn, db_path, tmp_path):
    insert_raw_event(
        migrated_conn,
        source="healthkit",
        record_type="t",
        payload={"x": 1},
        external_id="e1",
    )
    dest = tmp_path / "snap.db"
    out = backup_db(migrated_conn, db_path, dest)
    assert out == dest and dest.exists()
    snap = sqlite3.connect(dest)
    try:
        assert snap.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 1
    finally:
        snap.close()


def test_backup_never_overwrites(migrated_conn, db_path, tmp_path):
    dest = tmp_path / "snap.db"
    backup_db(migrated_conn, db_path, dest)
    with pytest.raises(FileExistsError):
        backup_db(migrated_conn, db_path, dest)


def test_backup_default_dest_under_db_dir(migrated_conn, db_path):
    out = backup_db(migrated_conn, db_path)
    assert out.parent == db_path.parent / "backups"
    assert out.name.startswith(db_path.stem + "-")


# ---- verify ----------------------------------------------------------------


def test_verify_ok_on_fresh_db(migrated_conn):
    report = verify_db(migrated_conn)
    assert report.ok
    assert report.integrity == "ok"
    assert report.fk_violations == 0
    assert report.row_counts["raw_events"] == 0
    assert len(report.canonical_fingerprint) == 64


# ---- auto_since ------------------------------------------------------------


def test_auto_since_none_when_empty(migrated_conn):
    assert auto_since(migrated_conn) is None


def test_auto_since_backs_off_overlap(migrated_conn):
    insert_raw_event(
        migrated_conn,
        source="whoop_api",
        record_type="recovery",
        payload={"a": 1},
        external_id="r1",
        recorded_at="2026-06-10T08:00:00.000Z",
    )
    assert auto_since(migrated_conn, overlap_days=2) == "2026-06-08T08:00:00Z"


def test_auto_since_uses_min_across_record_types(migrated_conn):
    # an interrupted ingest can leave one type behind; watermark must follow
    # the LAGGING type so nothing is stranded past it
    insert_raw_event(
        migrated_conn,
        source="whoop_api",
        record_type="recovery",
        payload={"a": 2},
        external_id="r2",
        recorded_at="2026-06-15T08:00:00.000Z",
    )
    insert_raw_event(
        migrated_conn,
        source="whoop_api",
        record_type="workout",
        payload={"b": 1},
        external_id="w1",
        recorded_at="2026-06-10T08:00:00.000Z",
    )
    assert auto_since(migrated_conn, overlap_days=2) == "2026-06-08T08:00:00Z"


def test_auto_since_ignores_other_sources(migrated_conn):
    insert_raw_event(
        migrated_conn,
        source="healthkit",
        record_type="t",
        payload={"a": 1},
        external_id="h1",
        recorded_at="2026-06-10 08:00:00 -0500",
    )
    assert auto_since(migrated_conn) is None


# ---- CLI surface -----------------------------------------------------------


def test_parser_accepts_new_commands():
    p = build_parser()
    assert p.parse_args(["db", "backup"]).func
    assert p.parse_args(["db", "verify"]).func
    assert p.parse_args(["doctor"]).func
    assert p.parse_args(["sync"]).func
    a = p.parse_args(["status", "--json"])
    assert a.json and a.date is None
    a = p.parse_args(["tdee", "--json"])
    assert a.json and a.end is None
    assert p.parse_args(["ingest", "whoop"]).since is None  # now optional
