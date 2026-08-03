"""Shared pytest fixtures."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from coach.store import db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_coach.db"


@pytest.fixture
def migrated_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A connection to a freshly-migrated temp database."""
    conn = db.connect(db_path)
    db.migrate(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def acknowledged(db_path: Path):
    """Pre-accept the §8.6 medical disclaimer for every account in the test DB.

    The web app gates every advice surface until the signed-in user has accepted
    the notice (migration 0015). That gate is real behaviour and it is owned by
    ``tests/test_disclaimer_gate.py``, which exercises it deliberately — from
    both sides, including the case where an unacknowledged user must be blocked.

    Every OTHER web test is about something else (tenancy, jobs, rendering), and
    making each of them click through a consent page would test the gate 40
    times and the actual subject once. So they opt into this fixture instead.

    Deliberately **not** autouse: a fixture that silently disables a safety gate
    everywhere is how the gate stops being tested at all. A test that wants past
    it has to say so by name.
    """
    from coach.store import disclaimer as D

    def _ack() -> None:
        conn = db.connect(db_path)
        try:
            for row in conn.execute("SELECT id FROM app_user").fetchall():
                if not D.has_acknowledged(conn, user_id=row["id"]):
                    D.acknowledge(conn, user_id=row["id"], user_agent="pytest")
            conn.commit()
        finally:
            conn.close()

    # Called once now for accounts that already exist, and returned so a test
    # that creates a user mid-run (an invite acceptance) can re-run it.
    _ack()
    return _ack
