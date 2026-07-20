"""Database maintenance: backup + integrity verification.

The whole store — including the sacred, never-regenerable ``raw_events`` (§2.1)
— is one SQLite file. That makes protection cheap, so we make it easy:

  * :func:`backup_db` — consistent online snapshot via SQLite's backup API
    (safe while the CLI or another process holds the file open).
  * :func:`verify_db` — integrity + foreign-key checks plus per-table row
    counts and the canonical fingerprint, so "is my data intact?" is one
    command instead of ad-hoc SQL.

Restores are deliberately manual (copy the snapshot over the db path yourself):
an automated restore is a destructive overwrite and stays a human action (§8.5).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .canonical import canonical_fingerprint

_COUNT_TABLES = (
    "raw_events",
    "recovery",
    "workout",
    "weight_measurement",
    "food_entry",
)


def backup_db(conn: sqlite3.Connection, db_path: Path, dest: Path | None = None) -> Path:
    """Write a consistent snapshot of the live DB; returns the snapshot path.

    Default destination: ``<db dir>/backups/<db stem>-<UTC timestamp>.db``.
    Never overwrites an existing file — a backup that clobbers a backup is how
    you lose the copy you needed.
    """
    if dest is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        dest = db_path.parent / "backups" / f"{db_path.stem}-{stamp}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(f"refusing to overwrite existing backup: {dest}")
    # write to a .part temp, atomically rename on success — a failed backup must
    # never leave a partial file that looks like a valid snapshot
    tmp = dest.with_suffix(dest.suffix + ".part")
    target = sqlite3.connect(tmp)
    try:
        conn.backup(target)
        target.close()
        tmp.replace(dest)
    except BaseException:
        target.close()
        tmp.unlink(missing_ok=True)
        raise
    return dest


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    integrity: str  # 'ok' or the first reported corruption line
    fk_violations: int
    row_counts: dict[str, int]
    canonical_fingerprint: str


def verify_db(conn: sqlite3.Connection) -> VerifyReport:
    """Run integrity + FK checks and gather row counts. Read-only."""
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    counts: dict[str, int] = {}
    for table in _COUNT_TABLES:
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[table] = -1  # table missing (pre-migration db)
    try:
        fingerprint = canonical_fingerprint(conn)
    except sqlite3.OperationalError:
        # pre-migration DB: canonical tables absent — same state the counts
        # loop already reports as -1; don't crash the very tool that checks it
        fingerprint = "unavailable (canonical tables missing — run `coach db init`)"
    return VerifyReport(
        ok=(integrity == "ok" and not fk),
        integrity=integrity,
        fk_violations=len(fk),
        row_counts=counts,
        canonical_fingerprint=fingerprint,
    )
