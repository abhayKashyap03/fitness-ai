"""Persistence for medical-disclaimer acknowledgements (migration 0015).

The text itself lives in :mod:`coach.disclaimer`; this module only records who
agreed to which version, and answers the one question every advice surface needs
to ask before it renders a number: *has this user acknowledged the notice they
are currently being governed by?*

Append-only (§2.1). Acknowledging twice writes two rows and loses nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from ..disclaimer import DISCLAIMER_VERSION


@dataclass(frozen=True)
class Acknowledgement:
    user_id: int
    version: int
    acknowledged_at: str
    user_agent: str | None


def acknowledge(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    version: int = DISCLAIMER_VERSION,
    user_agent: str | None = None,
) -> Acknowledgement:
    """Record that ``user_id`` accepted disclaimer ``version``.

    ``version`` defaults to the current one but is explicit in the signature so
    a caller that rendered an older text cannot accidentally record consent to
    the newer one.
    """
    at = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO user_disclaimer_ack (user_id, version, acknowledged_at, user_agent) "
        "VALUES (?,?,?,?)",
        (user_id, version, at, user_agent),
    )
    return Acknowledgement(
        user_id=user_id, version=version, acknowledged_at=at, user_agent=user_agent
    )


def has_acknowledged(
    conn: sqlite3.Connection, *, user_id: int, version: int = DISCLAIMER_VERSION
) -> bool:
    """True if this user has accepted **this** version of the notice.

    An acknowledgement of an older version deliberately does not count. That is
    the whole point of versioning it: consent is to a specific text, and a
    revision means a limitation was added or changed.
    """
    row = conn.execute(
        "SELECT 1 FROM user_disclaimer_ack WHERE user_id = ? AND version = ? LIMIT 1",
        (user_id, version),
    ).fetchone()
    return row is not None


def latest(
    conn: sqlite3.Connection, *, user_id: int
) -> Acknowledgement | None:
    """The most recent acknowledgement of any version, or None if never.

    Returns the row rather than a bool so the account page can show *what* was
    agreed to and when, instead of an unfalsifiable green tick.
    """
    row = conn.execute(
        "SELECT user_id, version, acknowledged_at, user_agent FROM user_disclaimer_ack "
        "WHERE user_id = ? ORDER BY acknowledged_at DESC, id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return Acknowledgement(
        user_id=row["user_id"],
        version=row["version"],
        acknowledged_at=row["acknowledged_at"],
        user_agent=row["user_agent"],
    )
