"""Medical disclaimer: text, acknowledgement store, and the web gate (§8.6).

The failures worth pinning here are not "does a string appear on a page". They
are the two ways this protection quietly stops protecting:

  1. the user and the model being held to different texts (drift), and
  2. a gate that either lets an unacknowledged user through, or traps a user who
     does not want to accept.
"""

from __future__ import annotations

import sqlite3

import pytest

from coach.disclaimer import DISCLAIMER_VERSION, FULL, LLM_SCOPE, SHORT
from coach.store import disclaimer as D

# ---- the text --------------------------------------------------------------


def test_the_model_is_held_to_the_text_the_user_was_shown():
    """The system prompt's scope section IS the disclaimer module's, verbatim.

    Two copies would drift, and the drift is invisible from either side: the
    user reads one set of limits while the model obeys another. This repo has
    already shipped that bug twice with ordinary logic (`plan set`, credentials).
    """
    from coach.coach.grounding import SYSTEM_PROMPT

    assert LLM_SCOPE in SYSTEM_PROMPT


def test_the_notice_names_concrete_limits_not_boilerplate():
    """Guards against the text decaying into 'consult a physician'.

    Each of these is a limitation this codebase measured and documented — the
    self-report error band (risk #7), recovery as signal rather than diagnosis
    (§8.6), and the disordered-eating case that a daily calorie number makes
    materially worse. Losing them silently is how a disclaimer becomes noise.
    """
    for phrase in ("20-40%", "disordered eating", "training lighter", "not a medical device"):
        assert phrase in FULL.lower(), f"the notice no longer mentions {phrase!r}"


def test_the_model_scope_forbids_the_specific_things_it_must():
    """The scope block is the model's half of the same promise.

    'Do not diagnose' alone is not enough — the failures that actually happen
    are softer: calling a number normal for someone whose history you don't
    have, and hedging into a medical answer instead of declining it.
    """
    low = LLM_SCOPE.lower()
    for phrase in ("train lighter", "do not diagnose", "normal", "clinician", "restriction"):
        assert phrase in low, f"the model's scope no longer forbids {phrase!r}"


def test_short_form_stays_short_enough_to_survive_a_footer():
    assert len(SHORT) < 160


# ---- the acknowledgement store --------------------------------------------


def test_unacknowledged_by_default(migrated_conn):
    assert D.has_acknowledged(migrated_conn, user_id=1) is False
    assert D.latest(migrated_conn, user_id=1) is None


def test_acknowledging_records_version_and_time(migrated_conn):
    ack = D.acknowledge(migrated_conn, user_id=1, user_agent="pytest/1.0")
    assert ack.version == DISCLAIMER_VERSION
    assert D.has_acknowledged(migrated_conn, user_id=1) is True
    latest = D.latest(migrated_conn, user_id=1)
    assert latest is not None
    assert latest.user_agent == "pytest/1.0"
    assert latest.acknowledged_at.endswith("+00:00")  # UTC instant (§2.6)


def test_an_old_acknowledgement_is_not_consent_to_a_new_text(migrated_conn):
    """The whole point of versioning it.

    A revision means a limitation was added or changed. Counting the old row
    would mean nobody ever reads the new one.
    """
    D.acknowledge(migrated_conn, user_id=1, version=DISCLAIMER_VERSION - 1)
    assert D.has_acknowledged(migrated_conn, user_id=1, version=DISCLAIMER_VERSION) is False
    assert D.has_acknowledged(migrated_conn, user_id=1, version=DISCLAIMER_VERSION - 1) is True


def test_acknowledgement_is_per_user_not_global(migrated_conn):
    """One user agreeing must never speak for another."""
    migrated_conn.execute(
        "INSERT INTO app_user (id, email, status, role, created_at) "
        "VALUES (2,'friend@example.test','active','member','2026-08-02T00:00:00+00:00')"
    )
    D.acknowledge(migrated_conn, user_id=1)
    assert D.has_acknowledged(migrated_conn, user_id=1) is True
    assert D.has_acknowledged(migrated_conn, user_id=2) is False


def test_acknowledging_twice_keeps_both_rows(migrated_conn):
    """Append-only (§2.1) — the history of what was agreed to stays readable."""
    D.acknowledge(migrated_conn, user_id=1)
    D.acknowledge(migrated_conn, user_id=1)
    n = migrated_conn.execute(
        "SELECT COUNT(*) AS n FROM user_disclaimer_ack WHERE user_id = 1"
    ).fetchone()["n"]
    assert n == 2


def test_ack_requires_a_real_user(migrated_conn):
    """The FK is not decoration: an acknowledgement by nobody is not evidence."""
    migrated_conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        D.acknowledge(migrated_conn, user_id=999)
