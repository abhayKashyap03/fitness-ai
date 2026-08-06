"""Database maintenance: backup + integrity verification.

The whole store — including the sacred, never-regenerable ``raw_events`` (§2.1)
— is one SQLite file. That makes protection cheap, so we make it easy:

  * :func:`backup_db` — consistent online snapshot via SQLite's backup API
    (safe while the CLI or another process holds the file open).
  * :func:`verify_db` — integrity + foreign-key checks plus per-table row
    counts and the canonical fingerprint, so "is my data intact?" is one
    command instead of ad-hoc SQL.

  * :func:`rehearse_restore` — proves a snapshot would actually come back,
    without touching the live database at all.
  * :func:`restore_db` — the real thing, once a human has said so.

**Why restore stopped being "just copy the file yourself".** This module used to
say restores were deliberately manual, on the reasoning that an automated
destructive overwrite should stay a human action (§8.5). That reasoning was
half right and produced a bad outcome: the operation nobody had ever run was the
one that would first be attempted, by hand, under pressure, at the exact moment
the data was already in trouble.

[ADR-0019](../../../docs/adr/0019-hosting-the-owners-instance.md) makes the
laptop the archive for a hosted database and says the restore must be
**rehearsed, not assumed** — an untested backup is a belief, not a backup. So
the split is now by *destructiveness*, not by automation:

  * Rehearsal destroys nothing, can run on a schedule, and answers "would this
    file come back, and is it the data I think it is?"
  * The real restore still requires explicit human confirmation (§8.5), but it
    **verifies the snapshot before it touches anything** and **preserves the
    database it replaces**. Restoring a corrupt backup over a live database is
    a worse outcome than not restoring at all, and a hand-rolled ``cp`` cannot
    check for it.
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


# ---- restore ---------------------------------------------------------------


@dataclass(frozen=True)
class RehearsalReport:
    """What a restore of ``snapshot`` would produce — computed without doing it."""

    snapshot: Path
    ok: bool
    report: VerifyReport
    schema_version: int
    # Populated only when a live database was available to compare against.
    # None means "no comparison was made", never "they matched" (§2.7).
    live_fingerprint: str | None
    fingerprint_matches: bool | None
    row_delta: dict[str, int] | None

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"WOULD NOT RESTORE — {self.report.integrity}"
        if self.fingerprint_matches is None:
            return "restorable (no live database to compare against)"
        if self.fingerprint_matches:
            return "restorable, canonical data identical to live"
        return "restorable, but canonical data DIFFERS from live"


def rehearse_restore(
    snapshot: Path, *, live_conn: sqlite3.Connection | None = None
) -> RehearsalReport:
    """Prove ``snapshot`` would come back. Touches nothing.

    Opens the snapshot as its own database and runs the same verification the
    live file gets, then — when a live connection is supplied — compares the
    canonical fingerprint and row counts so the operator learns not just "it
    opens" but *how far behind* the backup is.

    A fingerprint mismatch is **not** a failure. A nightly backup pulled before
    the morning's sync is legitimately behind, and reporting that as broken
    would train the operator to ignore the check. It is reported as a delta.
    """
    if not snapshot.exists():
        raise FileNotFoundError(f"no such snapshot: {snapshot}")

    from . import db as _db

    conn = _db.connect(snapshot)
    try:
        report = verify_db(conn)
        version = _db.current_version(conn)
        snap_counts = report.row_counts
        fingerprint = report.canonical_fingerprint
    finally:
        conn.close()

    live_fp: str | None = None
    matches: bool | None = None
    delta: dict[str, int] | None = None
    if live_conn is not None:
        live = verify_db(live_conn)
        live_fp = live.canonical_fingerprint
        matches = live_fp == fingerprint
        delta = {t: snap_counts.get(t, 0) - live.row_counts.get(t, 0) for t in snap_counts}

    return RehearsalReport(
        snapshot=snapshot,
        # "Restorable" means integrity-clean and actually migrated. A file that
        # opens but has no canonical tables is not a backup of anything.
        ok=report.ok and version > 0,
        report=report,
        schema_version=version,
        live_fingerprint=live_fp,
        fingerprint_matches=matches,
        row_delta=delta,
    )


@dataclass(frozen=True)
class RestoreResult:
    restored_from: Path
    db_path: Path
    # Where the database that was REPLACED went. Never deleted (§8.5) — if the
    # restore turns out to have been the mistake, the only copy of the state it
    # overwrote is this file.
    replaced_to: Path | None
    schema_version: int


def restore_db(snapshot: Path, db_path: Path) -> RestoreResult:
    """Replace the database at ``db_path`` with ``snapshot``. **Destructive.**

    Callers must have obtained explicit human confirmation first (§8.5); this
    function does not prompt, because prompting from a library makes it
    untestable and unscriptable.

    Two invariants make this safer than the ``cp`` it replaces:

    1. **The snapshot is verified before anything is touched.** Restoring a
       corrupt backup over a live database is strictly worse than refusing, and
       it is the failure most likely to happen during an actual incident.
    2. **The replaced database is preserved, not deleted.** It is renamed
       alongside itself with a UTC stamp. A restore performed in a panic is a
       decision that may itself need undoing.
    """
    rehearsal = rehearse_restore(snapshot)
    if not rehearsal.ok:
        raise ValueError(
            f"refusing to restore from an unhealthy snapshot: {rehearsal.report.integrity} "
            f"(schema version {rehearsal.schema_version})"
        )

    replaced: Path | None = None
    if db_path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        replaced = db_path.with_name(f"{db_path.stem}-replaced-{stamp}{db_path.suffix}")
        db_path.replace(replaced)

    # Copy via SQLite's own backup API rather than the filesystem, so a live
    # WAL/journal beside the snapshot cannot produce a torn copy.
    from . import db as _db

    src = _db.connect(snapshot)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        dst = sqlite3.connect(db_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    except BaseException:
        # Put the original back rather than leaving the operator with neither.
        if replaced is not None:
            db_path.unlink(missing_ok=True)
            replaced.replace(db_path)
        raise
    finally:
        src.close()

    return RestoreResult(
        restored_from=snapshot,
        db_path=db_path,
        replaced_to=replaced,
        schema_version=rehearsal.schema_version,
    )
