"""SQLite connection + a minimal, explicit migration runner.

No Alembic (CLAUDE.md T1.3): a numbered ``schema/migrations/`` directory and a
``schema_version`` table is enough at n=1. Migrations are applied in ascending
numeric order, each in its own transaction, and recorded so re-running is a
no-op (idempotent).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..paths import migrations_dir

_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (and if needed create the parent dir for) a SQLite connection.

    Enables foreign keys and returns rows as :class:`sqlite3.Row`.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """Return migrations sorted by version. Raises on duplicate versions."""
    directory = directory or migrations_dir()
    found: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        m = _MIGRATION_RE.match(path.name)
        if not m:
            raise ValueError(f"Migration file {path.name!r} does not match NNNN_name.sql")
        version = int(m.group(1))
        if version in found:
            raise ValueError(
                f"Duplicate migration version {version}: {found[version].path.name} and {path.name}"
            )
        found[version] = Migration(version=version, name=path.stem, path=path)
    return [found[v] for v in sorted(found)]


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
          version    INTEGER PRIMARY KEY,
          name       TEXT NOT NULL,
          applied_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def current_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration version, or 0 if none."""
    _ensure_version_table(conn)
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row and row["v"] is not None else 0


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_version_table(conn)
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {int(r["version"]) for r in rows}


def pending_migrations(conn: sqlite3.Connection, directory: Path | None = None) -> list[Migration]:
    done = applied_versions(conn)
    return [m for m in discover_migrations(directory) if m.version not in done]


def _split_statements(sql: str) -> list[str]:
    """Split a migration file into individual SQL statements.

    Uses :func:`sqlite3.complete_statement` (semicolon-aware, string/comment
    safe) so statements can run one-by-one inside a single explicit
    transaction — unlike ``executescript``, which issues an implicit COMMIT
    first and leaves partial DDL behind when a later statement fails.
    """
    stmts: list[str] = []
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmts.append(buf.strip())
            buf = ""
    tail = buf.strip()
    if tail and any(
        ln.strip() and not ln.strip().startswith("--") for ln in tail.splitlines()
    ):
        stmts.append(tail)  # trailing statement missing its semicolon
    return stmts


def migrate(conn: sqlite3.Connection, directory: Path | None = None) -> list[Migration]:
    """Apply all pending migrations in order. Returns the ones applied.

    Each migration file is **atomic**: all its statements plus the
    schema_version record commit together, or roll back together. A migration
    that raises leaves every *earlier* migration committed and this one fully
    unapplied, so a fixed re-run resumes cleanly. Safe to call repeatedly —
    already-applied migrations are skipped.
    """
    applied: list[Migration] = []
    for mig in pending_migrations(conn, directory):
        conn.commit()  # flush any caller transaction before our explicit one
        try:
            conn.execute("BEGIN")
            for stmt in _split_statements(mig.sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
                (mig.version, mig.name, _utcnow_iso()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        applied.append(mig)
    return applied
