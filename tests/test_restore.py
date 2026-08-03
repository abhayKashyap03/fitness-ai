"""Backup rehearsal and restore (ADR-0019 §5).

"The restore must be rehearsed, not assumed" is the whole point, so the tests
that matter are the ones about *not* making things worse: refusing a corrupt
snapshot, and never destroying the database being replaced.
"""

from __future__ import annotations

import sqlite3

import pytest

from coach.store import db
from coach.store.maintenance import backup_db, rehearse_restore, restore_db


def _seed_weight(conn: sqlite3.Connection, day: str, kg: float) -> None:
    conn.execute(
        "INSERT INTO weight_measurement (id, user_id, day_key, source, source_app, "
        "weight_kg, raw_ref, derived_at) VALUES (?,1,?,'healthkit','okok',?,NULL,?)",
        (f"wt:{day}", day, kg, f"{day}T00:00:00+00:00"),
    )
    conn.commit()


# ---- rehearsal -------------------------------------------------------------


def test_rehearsal_reports_a_healthy_snapshot(migrated_conn, db_path, tmp_path):
    _seed_weight(migrated_conn, "2026-05-01", 83.0)
    snap = backup_db(migrated_conn, db_path, tmp_path / "snap.db")
    r = rehearse_restore(snap, live_conn=migrated_conn)
    assert r.ok
    assert r.schema_version == db.current_version(migrated_conn)
    assert r.fingerprint_matches is True
    assert r.row_delta == dict.fromkeys(r.row_delta, 0)
    assert "identical" in r.summary


def test_rehearsal_touches_nothing(migrated_conn, db_path, tmp_path):
    """A check that can damage the thing it checks will not get run."""
    _seed_weight(migrated_conn, "2026-05-01", 83.0)
    snap = backup_db(migrated_conn, db_path, tmp_path / "snap.db")
    before = (db_path.read_bytes(), snap.read_bytes())
    rehearse_restore(snap, live_conn=migrated_conn)
    assert (db_path.read_bytes(), snap.read_bytes()) == before


def test_a_stale_backup_is_reported_as_behind_not_broken(migrated_conn, db_path, tmp_path):
    """A nightly backup pulled before the morning sync is legitimately behind.

    Calling that a failure trains the operator to ignore the check, which is
    worse than not having one.
    """
    _seed_weight(migrated_conn, "2026-05-01", 83.0)
    snap = backup_db(migrated_conn, db_path, tmp_path / "snap.db")
    _seed_weight(migrated_conn, "2026-05-02", 82.8)  # live moves on
    r = rehearse_restore(snap, live_conn=migrated_conn)
    assert r.ok is True
    assert r.fingerprint_matches is False
    assert r.row_delta["weight_measurement"] == -1
    assert "DIFFERS" in r.summary


def test_no_live_database_means_no_comparison_not_a_match(migrated_conn, db_path, tmp_path):
    """Absence is absence (§2.7) — 'nothing to compare' must not read as 'matched'."""
    snap = backup_db(migrated_conn, db_path, tmp_path / "snap.db")
    r = rehearse_restore(snap, live_conn=None)
    assert r.fingerprint_matches is None
    assert r.live_fingerprint is None
    assert r.row_delta is None


def test_a_corrupt_snapshot_fails_rehearsal(tmp_path):
    bad = tmp_path / "not-a-database.db"
    bad.write_bytes(b"this is not sqlite, it is a text file with a .db name")
    with pytest.raises(sqlite3.DatabaseError):
        rehearse_restore(bad)


def test_an_unmigrated_file_is_not_a_backup_of_anything(tmp_path):
    """It opens cleanly and contains nothing. 'Opens' is not 'restorable'."""
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    r = rehearse_restore(empty)
    assert r.ok is False
    assert r.schema_version == 0


def test_a_missing_snapshot_says_so(tmp_path):
    with pytest.raises(FileNotFoundError):
        rehearse_restore(tmp_path / "nope.db")


# ---- the real restore ------------------------------------------------------


def test_restore_brings_the_data_back(migrated_conn, db_path, tmp_path):
    _seed_weight(migrated_conn, "2026-05-01", 83.0)
    snap = backup_db(migrated_conn, db_path, tmp_path / "snap.db")
    _seed_weight(migrated_conn, "2026-05-02", 82.8)
    migrated_conn.close()

    result = restore_db(snap, db_path)

    conn = db.connect(db_path)
    try:
        days = [r["day_key"] for r in conn.execute("SELECT day_key FROM weight_measurement")]
    finally:
        conn.close()
    assert days == ["2026-05-01"]  # the post-backup row is gone, as intended
    assert result.restored_from == snap


def test_the_replaced_database_is_preserved_not_deleted(migrated_conn, db_path, tmp_path):
    """§8.5. A restore performed in a panic is a decision that may need undoing.

    If the file it overwrote is gone, that decision is final and the state it
    destroyed is unrecoverable.
    """
    _seed_weight(migrated_conn, "2026-05-01", 83.0)
    snap = backup_db(migrated_conn, db_path, tmp_path / "snap.db")
    _seed_weight(migrated_conn, "2026-05-02", 82.8)
    migrated_conn.close()

    result = restore_db(snap, db_path)

    assert result.replaced_to is not None
    assert result.replaced_to.exists()
    conn = db.connect(result.replaced_to)
    try:
        days = [r["day_key"] for r in conn.execute("SELECT day_key FROM weight_measurement")]
    finally:
        conn.close()
    assert days == ["2026-05-01", "2026-05-02"]  # the overwritten state survived


def test_a_corrupt_snapshot_never_reaches_the_live_database(migrated_conn, db_path, tmp_path):
    """The failure most likely to happen during a real incident.

    Restoring a broken backup over a working database is strictly worse than
    refusing, and a hand-rolled `cp` cannot tell the difference.
    """
    _seed_weight(migrated_conn, "2026-05-01", 83.0)
    migrated_conn.close()
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"definitely not sqlite")

    with pytest.raises(sqlite3.DatabaseError):
        restore_db(bad, db_path)

    conn = db.connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM weight_measurement").fetchone()["n"]
    finally:
        conn.close()
    assert n == 1  # untouched


def test_an_unmigrated_snapshot_is_refused(migrated_conn, db_path, tmp_path):
    _seed_weight(migrated_conn, "2026-05-01", 83.0)
    migrated_conn.close()
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()

    with pytest.raises(ValueError, match="unhealthy snapshot"):
        restore_db(empty, db_path)

    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM weight_measurement").fetchone()["n"] == 1
    finally:
        conn.close()


def _cli_env(monkeypatch, db_path) -> None:
    """Point the CLI at the temp DB — it reads settings from the environment."""
    monkeypatch.setenv("COACH_DB_PATH", str(db_path))
    monkeypatch.setenv("COACH_HOME_TZ", "America/New_York")


def test_the_cli_reports_a_corrupt_snapshot_instead_of_crashing(
    migrated_conn, db_path, tmp_path, monkeypatch
):
    """Found by running it: both commands raised a raw sqlite3 traceback.

    The moment this output is read is an incident, and a Python stack trace is
    the worst possible thing to hand someone then — it does not say whether the
    live database survived, which is the only question that matters.
    """
    from coach.cli.main import main

    _seed_weight(migrated_conn, "2026-05-01", 83.0)
    migrated_conn.close()
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"definitely not sqlite")
    _cli_env(monkeypatch, db_path)

    assert main(["db", "rehearse-restore", str(bad)]) == 1
    assert main(["db", "restore", str(bad), "--yes"]) == 1

    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM weight_measurement").fetchone()["n"] == 1
    finally:
        conn.close()


def test_the_cli_refuses_to_restore_without_explicit_confirmation(
    migrated_conn, db_path, tmp_path, monkeypatch
):
    """§8.5 — the destructive path must not be reachable by accident."""
    from coach.cli.main import main

    snap = backup_db(migrated_conn, db_path, tmp_path / "snap.db")
    migrated_conn.close()
    _cli_env(monkeypatch, db_path)
    assert main(["db", "restore", str(snap)]) == 2


def test_restoring_where_no_database_exists_is_fine(migrated_conn, db_path, tmp_path):
    """The disaster-recovery case: the live file is gone entirely."""
    _seed_weight(migrated_conn, "2026-05-01", 83.0)
    snap = backup_db(migrated_conn, db_path, tmp_path / "snap.db")
    migrated_conn.close()
    db_path.unlink()

    result = restore_db(snap, db_path)

    assert result.replaced_to is None  # nothing was replaced, so nothing was kept
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM weight_measurement").fetchone()["n"] == 1
    finally:
        conn.close()
