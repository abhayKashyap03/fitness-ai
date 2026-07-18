"""Daily rollup (T3.1): explicit missing-data states, grouped workout counting."""

from __future__ import annotations

from coach.compute.daily import daily_status

DAY = "2026-07-10"


def _recovery(conn, score=66):
    conn.execute(
        "INSERT INTO recovery (id, user_id, day_key, source, score, hrv_rmssd_ms, "
        "resting_hr_bpm, score_method, is_official, derived_at) "
        "VALUES ('r1',1,?,'whoop_api',?,48.3,52,'whoop_proprietary',1,'t')",
        (DAY, score),
    )
    conn.commit()


def _weight(conn, kg=80.0):
    conn.execute(
        "INSERT INTO weight_measurement (id,user_id,day_key,source,measured_at,weight_kg,"
        "derived_at) VALUES ('w1',1,?,'withings','2026-07-10T06:00:00Z',?,'t')",
        (DAY, kg),
    )
    conn.commit()


def _food(conn, **kw):
    conn.execute(
        "INSERT INTO food_entry (id,user_id,day_key,source,entry_type,kcal,protein_g,"
        "carbs_g,fat_g,derived_at) VALUES (:id,1,:day,'manual',:et,:kcal,:p,:c,:f,'t')",
        {
            "id": kw["id"],
            "day": DAY,
            "et": kw.get("et", "item"),
            "kcal": kw.get("kcal"),
            "p": kw.get("p"),
            "c": kw.get("c"),
            "f": kw.get("f"),
        },
    )
    conn.commit()


def _workout(conn, id_, group, kcal, dur=2700):
    conn.execute(
        "INSERT INTO workout (id,user_id,source,sport_type,start_at,end_at,day_key,"
        "duration_s,kcal_total,strain,session_group_id,derived_at) "
        "VALUES (?,1,'whoop_api','run','2026-07-10T12:00:00Z','2026-07-10T12:45:00Z',?,?,?,9.0,?,'t')",
        (id_, DAY, dur, kcal, group),
    )
    conn.commit()


def test_food_not_logged_is_distinct_from_zero(migrated_conn):
    s = daily_status(migrated_conn, DAY)
    assert s.food.logged is False
    assert s.food.kcal is None  # not 0
    assert any("not logged" in n for n in s.notes)


def test_food_fast_is_known_zero(migrated_conn):
    _food(migrated_conn, id="a", et="fast", kcal=0)
    s = daily_status(migrated_conn, DAY)
    assert s.food.logged is True
    assert s.food.is_fast is True
    assert s.food.kcal == 0


def test_food_incomplete_flagged(migrated_conn):
    _food(migrated_conn, id="a", kcal=500, p=40, c=50, f=10)
    _food(migrated_conn, id="b", kcal=None, p=20)
    s = daily_status(migrated_conn, DAY)
    assert s.food.is_complete is False


def test_recovery_and_weight_present(migrated_conn):
    _recovery(migrated_conn, score=66)
    _weight(migrated_conn, kg=80.0)
    s = daily_status(migrated_conn, DAY)
    assert s.recovery is not None and s.recovery.score == 66
    assert s.weight is not None and s.weight.weight_kg == 80.0
    assert s.weight.trend_kg == 80.0  # single reading => trend == weight


def test_missing_recovery_weight_reported(migrated_conn):
    s = daily_status(migrated_conn, DAY)
    assert s.recovery is None
    assert s.weight is None


def test_workout_counted_once_per_group(migrated_conn):
    # two rows, same session group => 1 session, kcal not double-counted
    _workout(migrated_conn, "a", "grp:1", kcal=400)
    _workout(migrated_conn, "b", "grp:1", kcal=400)
    s = daily_status(migrated_conn, DAY)
    assert s.training.sessions == 1
    assert s.training.kcal_active == 400  # representative only, not 800


def test_two_distinct_sessions_counted_twice(migrated_conn):
    _workout(migrated_conn, "a", "grp:1", kcal=400)
    _workout(migrated_conn, "b", "grp:2", kcal=300)
    s = daily_status(migrated_conn, DAY)
    assert s.training.sessions == 2
    assert s.training.kcal_active == 700
