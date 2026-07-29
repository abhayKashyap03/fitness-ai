"""Coaching memory (ADR-0016) — append-only, and NOT model-authored.

The load-bearing property is the author constraint: a model-authored note would
let a fabricated number become persistent truth that nothing recomputes, which is
exactly what §2.2 exists to prevent.
"""

from __future__ import annotations

import sqlite3

import pytest

from coach.coach.tools import get_coach_notes
from coach.store.notes import SYSTEM, USER, add_note, recent_notes


def test_add_and_read_back(migrated_conn):
    add_note(migrated_conn, day_key="2026-07-20", text="Deload week.", kind="advice")
    migrated_conn.commit()

    (n,) = recent_notes(migrated_conn)
    assert n.text == "Deload week."
    assert n.kind == "advice"
    assert n.author == USER  # default author is the human, never the model
    assert n.day_key == "2026-07-20"


def test_notes_are_newest_first(migrated_conn):
    for i in range(3):
        add_note(migrated_conn, day_key=f"2026-07-2{i}", text=f"note {i}")
    migrated_conn.commit()

    texts = [n.text for n in recent_notes(migrated_conn)]
    assert texts == ["note 2", "note 1", "note 0"]


def test_limit_is_respected(migrated_conn):
    for i in range(5):
        add_note(migrated_conn, day_key="2026-07-20", text=f"note {i}")
    migrated_conn.commit()
    assert len(recent_notes(migrated_conn, limit=2)) == 2


def test_the_model_cannot_author_a_note(migrated_conn):
    """ADR-0016: authors are user or system. There is no 'model'."""
    with pytest.raises(ValueError, match="ADR-0016"):
        add_note(migrated_conn, day_key="2026-07-20", text="TDEE is 2100.", author="model")


def test_author_constraint_is_enforced_by_the_schema_too(migrated_conn):
    """Belt and braces: bypassing the helper must still fail."""
    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO coach_note (id, user_id, created_at, day_key, author, kind, text) "
            "VALUES ('n:x', 1, '2026-07-20T00:00:00+00:00', '2026-07-20', 'model', 'note', 'x')"
        )


def test_empty_note_is_refused(migrated_conn):
    with pytest.raises(ValueError, match="records nothing"):
        add_note(migrated_conn, day_key="2026-07-20", text="   ")


def test_notes_are_append_only_across_users(migrated_conn):
    add_note(migrated_conn, day_key="2026-07-20", text="mine", user_id=1)
    add_note(migrated_conn, day_key="2026-07-20", text="theirs", user_id=2)
    migrated_conn.commit()
    assert [n.text for n in recent_notes(migrated_conn, user_id=1)] == ["mine"]
    assert [n.text for n in recent_notes(migrated_conn, user_id=2)] == ["theirs"]


# ---- tool -------------------------------------------------------------------


def test_tool_reports_absence_honestly(migrated_conn):
    out = get_coach_notes(migrated_conn)
    assert out == {"notes": [], "count": 0}  # no memory yet; never invented history


def test_tool_shape(migrated_conn):
    add_note(migrated_conn, day_key="2026-07-20", text="Set a cut.", kind="plan", author=SYSTEM)
    migrated_conn.commit()

    out = get_coach_notes(migrated_conn)
    assert out["count"] == 1
    note = out["notes"][0]
    assert note["author"] == SYSTEM
    assert note["kind"] == "plan"
    assert set(note) == {"created_at", "day_key", "author", "kind", "text"}


def test_dispatch_does_not_inject_a_date_into_a_dateless_tool(migrated_conn):
    """get_coach_notes has no day argument; the today-filler must skip it."""
    from coach.coach import tools

    out = tools.dispatch(migrated_conn, "get_coach_notes", {}, today="2026-07-20")
    assert "date" not in out and "end" not in out
    assert out["count"] == 0
