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
