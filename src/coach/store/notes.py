"""Coaching memory: the record of decisions, not of measurements (ADR-0016).

Append-only, like `plan` (ADR-0013). Nothing here is ever edited or deleted — a
superseded note is followed by a new one, so what was believed at the time stays
recoverable.

**Authors are `user` or `system` only.** The model reads this store and never
writes it: a model-authored note would let a fabricated number become persistent
truth that nothing recomputes, which is precisely what §2.2 exists to prevent.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

USER = "user"
SYSTEM = "system"
AUTHORS = frozenset({USER, SYSTEM})


@dataclass(frozen=True)
class NoteRow:
    id: str
    user_id: int
    created_at: str  # UTC ISO-8601
    day_key: str  # the local day the note is ABOUT
    author: str  # 'user' | 'system'
    kind: str  # 'plan' | 'advice' | 'observation' | 'note'
    text: str


def note_id(user_id: int, created_at: str) -> str:
    return f"note:{user_id}:{created_at}"


def add_note(
    conn: sqlite3.Connection,
    *,
    day_key: str,
    text: str,
    kind: str = "note",
    author: str = USER,
    user_id: int = 1,
) -> NoteRow:
    """Append one note. Caller owns the transaction.

    Rejects an unknown author rather than coercing it — the CHECK constraint
    would catch it anyway, but failing here names the actual rule (ADR-0016).
    """
    if author not in AUTHORS:
        raise ValueError(
            f"author must be one of {sorted(AUTHORS)}, got {author!r} — the model "
            "does not author memory (ADR-0016)"
        )
    text = text.strip()
    if not text:
        raise ValueError("a note needs text; an empty note records nothing")

    created_at = datetime.now(UTC).isoformat()
    row = NoteRow(
        id=note_id(user_id, created_at),
        user_id=user_id,
        created_at=created_at,
        day_key=day_key,
        author=author,
        kind=kind,
        text=text,
    )
    conn.execute(
        "INSERT INTO coach_note (id, user_id, created_at, day_key, author, kind, text) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (row.id, row.user_id, row.created_at, row.day_key, row.author, row.kind, row.text),
    )
    return row


def recent_notes(conn: sqlite3.Connection, *, limit: int = 20, user_id: int = 1) -> list[NoteRow]:
    """The most recent notes, newest first. Empty list = no memory yet (§2.7)."""
    rows = conn.execute(
        "SELECT id, user_id, created_at, day_key, author, kind, text "
        "FROM coach_note WHERE user_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [
        NoteRow(
            id=r["id"],
            user_id=r["user_id"],
            created_at=r["created_at"],
            day_key=r["day_key"],
            author=r["author"],
            kind=r["kind"],
            text=r["text"],
        )
        for r in rows
    ]
