"""Hand-logged exercise (P12, own logging).

Written after the food path shipped without rebuild-safety, so the rebuild test
here is not an afterthought — it is the first thing this module was checked for.
"""

from __future__ import annotations

import pytest

from coach.normalize.exercise import canonical_sport, parse_exercise_log
from coach.services.exercise_log import log_exercise

START = "2026-08-03T12:00:00-04:00"


def _log(conn, **kw):
    params = {
        "sport": "strength",
        "duration_s": 45 * 60,
        "started_at": START,
        "utc_offset": "-04:00",
        "tz_name": "America/New_York",
    }
    params.update(kw)
    return log_exercise(conn, **params)


# ---- sport mapping ---------------------------------------------------------


def test_canonical_sports_pass_through():
    assert canonical_sport("strength") == "strength"
    assert canonical_sport("RUN") == "run"


def test_common_words_map_onto_the_canonical_enum():
    assert canonical_sport("weights") == "strength"
    assert canonical_sport("jogging") == "run"
    assert canonical_sport("spin") == "cycle"
    assert canonical_sport("hiking") == "walk"


def test_an_unknown_sport_becomes_other_rather_than_being_rejected():
    """A session the user actually did must not be lost because we lack a word
    for it. The original text survives in source_sport_raw."""
    assert canonical_sport("underwater basket weaving") == "other"
    assert canonical_sport(None) == "other"


# ---- parsing ---------------------------------------------------------------


def test_a_session_parses_with_a_derived_end_and_day():
    row = parse_exercise_log(
        {"sport": "weights", "duration_s": 2700, "started_at": START, "utc_offset": "-04:00"}
    )
    assert row is not None
    assert row.source == "manual"
    assert row.sport_type == "strength"
    assert row.source_sport_raw == "weights"  # audit trail for the mapping
    assert row.duration_s == 2700
    assert row.day_key == "2026-08-03"
    assert row.end_at.startswith("2026-08-03T16:45")  # 12:00-04:00 + 45min = 16:45Z


def test_no_calorie_estimate_is_ever_invented():
    """ADR-0007: wearable calories-out never drives anything important, and a
    duration plus a sport cannot give energy expenditure for a specific body.
    An invented figure would flow into the day's balance as if measured."""
    row = parse_exercise_log(
        {"sport": "run", "duration_s": 3600, "started_at": START, "utc_offset": "-04:00"}
    )
    assert row is not None
    assert row.kcal_active is None
    assert row.kcal_total is None


def test_strain_is_never_invented_either():
    """WHOOP-proprietary and not computable here (ADR-0015 keeps it a documented
    WHOOP-only exception)."""
    row = parse_exercise_log(
        {"sport": "run", "duration_s": 3600, "started_at": START, "utc_offset": "-04:00"}
    )
    assert row is not None
    assert row.strain is None


def test_a_session_with_no_duration_is_not_a_session():
    for bad in (
        {"sport": "run", "started_at": START},
        {"sport": "run", "duration_s": 0, "started_at": START},
        {"sport": "run", "duration_s": 600},
    ):
        assert parse_exercise_log(bad) is None


def test_a_stated_calorie_figure_is_kept():
    row = parse_exercise_log({"sport": "run", "duration_s": 3600, "started_at": START, "kcal": 420})
    assert row is not None
    assert row.kcal_active == 420


# ---- logging + rebuild -----------------------------------------------------


def test_logging_writes_raw_and_canonical(migrated_conn):
    logged = _log(migrated_conn)
    migrated_conn.commit()
    raw = migrated_conn.execute(
        "SELECT source, record_type FROM raw_events WHERE id=?", (logged.raw_ref,)
    ).fetchone()
    assert raw["source"] == "manual"
    assert raw["record_type"] == "exercise_log"

    row = migrated_conn.execute("SELECT * FROM workout WHERE id=?", (logged.workout_id,)).fetchone()
    assert row["raw_ref"] == logged.raw_ref
    assert row["sport_type"] == "strength"
    assert row["duration_s"] == 2700


def test_a_hand_logged_session_survives_a_full_rebuild(migrated_conn):
    """The invariant the food path missed. `workout` is raw-derived and the
    rebuild owns it, so the raw event must be sufficient to reproduce the row."""
    from coach.normalize.runner import normalize_all
    from coach.store.canonical import canonical_fingerprint

    _log(migrated_conn)
    _log(migrated_conn, sport="run", duration_s=1800, started_at="2026-08-04T07:00:00-04:00")
    migrated_conn.commit()
    before = canonical_fingerprint(migrated_conn)
    assert migrated_conn.execute("SELECT COUNT(*) AS n FROM workout").fetchone()["n"] == 2

    normalize_all(migrated_conn, user_id=1, rebuild=True)

    assert migrated_conn.execute("SELECT COUNT(*) AS n FROM workout").fetchone()["n"] == 2
    assert canonical_fingerprint(migrated_conn) == before, "rebuild must be byte-identical"


def test_logging_the_same_session_twice_is_idempotent(migrated_conn):
    _log(migrated_conn)
    _log(migrated_conn)
    migrated_conn.commit()
    assert migrated_conn.execute("SELECT COUNT(*) AS n FROM workout").fetchone()["n"] == 1


def test_two_sessions_the_same_day_stay_two(migrated_conn):
    _log(migrated_conn)
    _log(migrated_conn, sport="run", duration_s=1800, started_at="2026-08-03T18:00:00-04:00")
    migrated_conn.commit()
    assert migrated_conn.execute("SELECT COUNT(*) AS n FROM workout").fetchone()["n"] == 2


def test_a_session_with_no_duration_is_refused_loudly(migrated_conn):
    with pytest.raises(ValueError, match="positive duration"):
        _log(migrated_conn, duration_s=0)
