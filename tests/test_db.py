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
        assert [m.version for m in applied] == list(range(1, 16))
        assert db.current_version(conn) == 15
        # second run is a no-op
        assert db.migrate(conn) == []
        assert db.pending_migrations(conn) == []
    finally:
        conn.close()


def test_failed_migration_is_atomic(tmp_path: Path, db_path: Path):
    """A migration failing mid-file must leave NONE of its statements applied."""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_good.sql").write_text("CREATE TABLE a (x INTEGER);\n")
    (mdir / "0002_broken.sql").write_text(
        "CREATE TABLE b (x INTEGER);\nCREATE TABLE b (x INTEGER);\n"  # dup -> fails
    )
    conn = db.connect(db_path)
    try:
        import pytest as _pytest

        with _pytest.raises(Exception, match="already exists"):
            db.migrate(conn, mdir)
        # 0001 committed; 0002 fully rolled back — b must NOT exist
        assert db.current_version(conn) == 1
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "a" in tables
        assert "b" not in tables
        # fixing the file lets a re-run resume cleanly
        (mdir / "0002_broken.sql").write_text("CREATE TABLE b (x INTEGER);\n")
        assert [m.version for m in db.migrate(conn, mdir)] == [2]
        assert db.current_version(conn) == 2
    finally:
        conn.close()


def test_migration_leaving_dangling_fk_is_rolled_back(tmp_path: Path, db_path: Path):
    """The runner drops FK enforcement during a migration but verifies the whole
    DB with foreign_key_check before COMMIT — a migration that orphans a child
    row must roll back, so the relaxed enforcement never lets integrity rot in.
    """
    import pytest as _pytest

    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_base.sql").write_text(
        "CREATE TABLE parent (id TEXT PRIMARY KEY);\n"
        "CREATE TABLE child (id TEXT PRIMARY KEY, pref TEXT REFERENCES parent(id));\n"
    )
    # inserts a child pointing at a non-existent parent -> dangling FK
    (mdir / "0002_orphan.sql").write_text("INSERT INTO child (id, pref) VALUES ('c1', 'ghost');\n")
    conn = db.connect(db_path)
    try:
        with _pytest.raises(Exception, match="foreign-key violation"):
            db.migrate(conn, mdir)
        assert db.current_version(conn) == 1  # 0002 rolled back
        assert conn.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
        # enforcement restored for normal operation
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_parent_table_rebuild_preserves_fk_children(tmp_path: Path, db_path: Path):
    """A migration may DROP+recreate a table that other tables reference by FK
    (the raw_events source-CHECK removal in 0006), and the children still
    resolve afterwards.
    """
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_base.sql").write_text(
        "CREATE TABLE parent (id TEXT PRIMARY KEY, v TEXT NOT NULL CHECK (v IN ('a','b')));\n"
        "CREATE TABLE child (id TEXT PRIMARY KEY, pref TEXT REFERENCES parent(id));\n"
    )
    # rebuild parent to drop its CHECK, preserving rows
    (mdir / "0002_widen.sql").write_text(
        "CREATE TABLE parent_new (id TEXT PRIMARY KEY, v TEXT NOT NULL);\n"
        "INSERT INTO parent_new (id, v) SELECT id, v FROM parent;\n"
        "DROP TABLE parent;\n"
        "ALTER TABLE parent_new RENAME TO parent;\n"
    )
    conn = db.connect(db_path)
    try:
        db.migrate(conn, mdir)  # applies both in order
        conn.execute("INSERT INTO parent (id, v) VALUES ('p1', 'a')")
        conn.execute("INSERT INTO child (id, pref) VALUES ('c1', 'p1')")
        conn.commit()
        # CHECK is gone: a formerly-illegal value now inserts
        conn.execute("INSERT INTO parent (id, v) VALUES ('p2', 'anything')")
        conn.commit()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        joined = conn.execute(
            "SELECT COUNT(*) FROM child c JOIN parent p ON c.pref = p.id"
        ).fetchone()[0]
        assert joined == 1
    finally:
        conn.close()


def test_split_statements_shares_a_line(tmp_path: Path, db_path: Path):
    """Two statements on one line must split and apply (line-based split broke this)."""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_oneline.sql").write_text("CREATE TABLE a (x INTEGER); CREATE TABLE b (y TEXT);\n")
    conn = db.connect(db_path)
    try:
        db.migrate(conn, mdir)
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"a", "b"} <= tables
    finally:
        conn.close()


def test_migration_own_transaction_control_rejected(tmp_path: Path, db_path: Path):
    """Runner owns the transaction; BEGIN/COMMIT in a file must fail loudly."""
    import pytest as _pytest

    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_txn.sql").write_text("BEGIN;\nCREATE TABLE a (x INTEGER);\nCOMMIT;\n")
    conn = db.connect(db_path)
    try:
        with _pytest.raises(ValueError, match="transaction control"):
            db.migrate(conn, mdir)
    finally:
        conn.close()


def test_split_statements_semicolon_in_string_literal():
    stmts = db._split_statements("INSERT INTO t VALUES ('a;b'); CREATE TABLE u (x);\n")
    assert len(stmts) == 2
    assert "a;b" in stmts[0]


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
