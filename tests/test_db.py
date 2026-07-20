"""Migration runner: discovery, ordering, idempotency, version reporting."""

from __future__ import annotations

from pathlib import Path

import pytest

from coach.store import db


def test_discover_migrations_ordered_and_numbered():
    migs = db.discover_migrations()
    versions = [m.version for m in migs]
    assert versions == sorted(versions)
    assert versions[:3] == [1, 2, 3]


def test_discover_rejects_bad_filename(tmp_path: Path):
    (tmp_path / "nope.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="does not match"):
        db.discover_migrations(tmp_path)


def test_discover_rejects_duplicate_version(tmp_path: Path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0001_b.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="Duplicate migration version"):
        db.discover_migrations(tmp_path)


def test_migrate_applies_all_then_idempotent(db_path: Path):
    conn = db.connect(db_path)
    try:
        assert db.current_version(conn) == 0
        applied = db.migrate(conn)
        assert [m.version for m in applied] == [1, 2, 3, 4, 5]
        assert db.current_version(conn) == 5
        # second run is a no-op
        assert db.migrate(conn) == []
        assert db.pending_migrations(conn) == []
    finally:
        conn.close()


def test_core_tables_and_views_exist(migrated_conn):
    names = {
        r["name"]
        for r in migrated_conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    for expected in (
        "raw_events",
        "recovery",
        "workout",
        "recovery_resolved",
        "food_entry",
        "food_daily",
        "weight_measurement",
        "weight_trend",
        "schema_version",
    ):
        assert expected in names, f"missing {expected}"


def test_foreign_keys_enabled(migrated_conn):
    assert migrated_conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
