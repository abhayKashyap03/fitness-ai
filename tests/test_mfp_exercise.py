"""MFP `exercise_entry` -> canonical workout (and what must NOT become one).

Shape taken from a REAL diary payload: fields are flat on the item, with
`exercise` holding the definition (description / type / mets).
"""

from __future__ import annotations

import pytest

from coach.normalize.myfitnesspal import parse_exercise


def _walk_item(**over):
    item = {
        "type": "exercise_entry",
        "id": "f41e8121",
        "date": "2026-07-27",
        "start_time": "2026-07-27T08:30:00Z",
        "duration": 2700,
        "energy": {"unit": "calories", "value": 203.0},
        "distance": {"unit": "miles", "value": 2.5},
        "is_calorie_adjustment": False,
        "avg_heart_rate": None,
        "max_heart_rate": None,
        "exercise": {"description": "Walking, 3.0 mph, mod. pace", "type": "cardio"},
    }
    item.update(over)
    return item


def _record(*items, day="2026-07-27"):
    return {"date": day, "diary": {"items": list(items)}}


def test_parses_a_real_walking_session():
    (w,) = parse_exercise(_record(_walk_item()))
    assert w.source == "myfitnesspal"
    assert w.sport_type == "walk"
    assert w.source_sport_raw == "Walking, 3.0 mph, mod. pace"
    assert w.duration_s == 2700
    assert w.kcal_active == pytest.approx(203.0)
    assert w.day_key == "2026-07-27"
    assert w.start_at == "2026-07-27T08:30:00+00:00"
    assert w.end_at == "2026-07-27T09:15:00+00:00"  # start + duration
    assert w.distance_m == pytest.approx(4023.36)  # 2.5 miles
    assert w.strain is None  # WHOOP-proprietary, not comparable (§5)
    assert w.tz_name is None  # MFP gives no zone; strictly IANA or NULL (§2.6)


def test_calorie_adjustment_is_not_a_workout():
    """Device burn adjustments are not sessions — counting them invents training."""
    assert parse_exercise(_record(_walk_item(is_calorie_adjustment=True))) == []


def test_zero_or_missing_duration_is_skipped():
    assert parse_exercise(_record(_walk_item(duration=0))) == []
    assert parse_exercise(_record(_walk_item(duration=None))) == []


def test_food_and_step_items_are_ignored():
    other = [{"type": "diary_meal", "diary_meal": "Lunch"}, {"type": "steps_aggregate"}]
    assert parse_exercise(_record(*other)) == []


def test_empty_or_malformed_records_yield_nothing():
    assert parse_exercise({}) == []
    assert parse_exercise({"date": "2026-07-27"}) == []
    assert parse_exercise({"date": "2026-07-27", "diary": {"items": None}}) == []
    assert parse_exercise(_record()) == []


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Walking, 3.0 mph", "walk"),
        ("Hiking, cross country", "walk"),
        ("Running, 6 mph", "run"),
        ("Cycling, 12-13.9 mph", "cycle"),
        ("Swimming laps, freestyle", "swim"),
        ("Rowing machine, moderate", "rowing"),
        ("Yoga, hatha", "yoga"),
        ("Strength training (weight lifting)", "strength"),
        ("Circuit training", "hiit"),
    ],
)
def test_description_maps_into_the_canonical_vocabulary(description, expected):
    (w,) = parse_exercise(_record(_walk_item(exercise={"description": description})))
    assert w.sport_type == expected


def test_unknown_cardio_stays_other_rather_than_guessing():
    (w,) = parse_exercise(
        _record(_walk_item(exercise={"description": "Zumba class", "type": "cardio"}))
    )
    assert w.sport_type == "other"


def test_strength_falls_back_to_the_coarse_type():
    (w,) = parse_exercise(
        _record(_walk_item(exercise={"description": "Unlisted machine", "type": "strength"}))
    )
    assert w.sport_type == "strength"


def test_unknown_distance_unit_is_absent_not_zero():
    (w,) = parse_exercise(_record(_walk_item(distance={"unit": "furlongs", "value": 3})))
    assert w.distance_m is None


def test_missing_start_time_anchors_to_the_day():
    (w,) = parse_exercise(_record(_walk_item(start_time=None)))
    assert w.start_at.startswith("2026-07-27T00:00:00")
    assert w.day_key == "2026-07-27"  # day_key stays exact regardless


# ---- cross-source precedence (MFP owns training) ----------------------------


def _wk(conn, wid, source, sport, start, day, kcal, dur, strain=None, grp=None):
    conn.execute(
        "INSERT INTO workout (id, user_id, source, external_id, sport_type, "
        "start_at, end_at, day_key, duration_s, kcal_active, strain, "
        "session_group_id, derived_at) "
        "VALUES (?,1,?,?,?,?,?,?,?,?,?,?,'2026-01-01T00:00:00+00:00')",
        (wid, source, wid, sport, start, start, day, dur, kcal, strain, grp),
    )


def test_mfp_wins_the_session_numbers_but_whoop_keeps_strain(migrated_conn):
    """WHOOP is the recovery strap; the training log is MyFitnessPal's."""
    from coach.compute.daily import daily_status

    day, grp = "2026-07-23", "grp:1:strength:x"
    _wk(
        migrated_conn,
        "w1",
        "whoop_api",
        "strength",
        f"{day}T22:39:00+00:00",
        day,
        kcal=240.0,
        dur=1800,
        strain=15.5,
        grp=grp,
    )
    _wk(
        migrated_conn,
        "m1",
        "myfitnesspal",
        "strength",
        f"{day}T11:30:00+00:00",
        day,
        kcal=500.0,
        dur=5400,
        strain=None,
        grp=grp,
    )
    migrated_conn.commit()

    t = daily_status(migrated_conn, day).training
    assert t.sessions == 1  # one real session, not two (§5 double-count)
    assert t.kcal_active == pytest.approx(500.0)  # MFP's log, not WHOOP's estimate
    assert t.duration_s == 5400
    assert t.strain == pytest.approx(15.5)  # WHOOP-only metric survives the flip


def test_strain_counted_once_per_group_not_per_row(migrated_conn):
    from coach.compute.daily import daily_status

    day, grp = "2026-07-22", "grp:1:walk:y"
    _wk(
        migrated_conn,
        "w1",
        "whoop_api",
        "walk",
        f"{day}T18:00:00+00:00",
        day,
        kcal=100.0,
        dur=600,
        strain=8.0,
        grp=grp,
    )
    _wk(
        migrated_conn,
        "m1",
        "myfitnesspal",
        "walk",
        f"{day}T08:30:00+00:00",
        day,
        kcal=150.0,
        dur=900,
        strain=None,
        grp=grp,
    )
    migrated_conn.commit()

    assert daily_status(migrated_conn, day).training.strain == pytest.approx(8.0)


def test_ungrouped_sessions_still_count_separately(migrated_conn):
    """Distinct sessions must not be collapsed just because they share a day."""
    from coach.compute.daily import daily_status

    day = "2026-07-21"
    _wk(
        migrated_conn,
        "m1",
        "myfitnesspal",
        "walk",
        f"{day}T08:30:00+00:00",
        day,
        kcal=150.0,
        dur=900,
        grp="g1",
    )
    _wk(
        migrated_conn,
        "m2",
        "myfitnesspal",
        "strength",
        f"{day}T11:30:00+00:00",
        day,
        kcal=300.0,
        dur=3600,
        grp="g2",
    )
    migrated_conn.commit()

    t = daily_status(migrated_conn, day).training
    assert t.sessions == 2
    assert t.kcal_active == pytest.approx(450.0)


# ---- session detail ---------------------------------------------------------


def test_training_sessions_lists_one_row_per_real_session(migrated_conn):
    """The rollup says how much; this says what — deduped the same way."""
    from coach.compute.daily import training_sessions

    day, grp = "2026-07-23", "grp:1:strength:z"
    _wk(
        migrated_conn,
        "w1",
        "whoop_api",
        "strength",
        f"{day}T22:39:00+00:00",
        day,
        kcal=240.0,
        dur=1800,
        strain=15.5,
        grp=grp,
    )
    _wk(
        migrated_conn,
        "m1",
        "myfitnesspal",
        "strength",
        f"{day}T11:30:00+00:00",
        day,
        kcal=500.0,
        dur=5400,
        strain=None,
        grp=grp,
    )
    _wk(
        migrated_conn,
        "m2",
        "myfitnesspal",
        "walk",
        f"{day}T08:30:00+00:00",
        day,
        kcal=150.0,
        dur=900,
        strain=None,
        grp="grp:other",
    )
    migrated_conn.commit()

    out = training_sessions(migrated_conn, day)
    assert len(out) == 2  # the shared strength session is listed once
    strength = next(s for s in out if s.sport_type == "strength")
    assert strength.source == "myfitnesspal"  # MFP owns training (ADR-0015)
    assert strength.kcal_active == pytest.approx(500.0)
    assert strength.strain == pytest.approx(15.5)  # ...strap metric still carried


def test_training_sessions_empty_day(migrated_conn):
    from coach.compute.daily import training_sessions

    assert training_sessions(migrated_conn, "2026-07-20") == []


def test_get_training_sessions_tool_shape(migrated_conn):
    from coach.coach.tools import get_training_sessions

    out = get_training_sessions(migrated_conn, date="2026-07-20")
    assert out == {"date": "2026-07-20", "sessions": [], "count": 0}
