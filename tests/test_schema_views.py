"""Phase-0 resolver/rollup views: food_daily, weight_resolved_daily, weight_trend."""

from __future__ import annotations

import sqlite3


def _food(conn: sqlite3.Connection, **kw: object) -> None:
    cols = {
        "id": kw["id"],
        "user_id": 1,
        "day_key": kw["day_key"],
        "source": kw.get("source", "manual"),
        "entry_type": kw.get("entry_type", "item"),
        "kcal": kw.get("kcal"),
        "protein_g": kw.get("protein_g"),
        "carbs_g": kw.get("carbs_g"),
        "fat_g": kw.get("fat_g"),
        "derived_at": "2026-01-01T00:00:00Z",
    }
    conn.execute(
        "INSERT INTO food_entry (id,user_id,day_key,source,entry_type,kcal,"
        "protein_g,carbs_g,fat_g,derived_at) VALUES "
        "(:id,:user_id,:day_key,:source,:entry_type,:kcal,:protein_g,:carbs_g,:fat_g,:derived_at)",
        cols,
    )
    conn.commit()


def _weight(conn: sqlite3.Connection, **kw: object) -> None:
    conn.execute(
        "INSERT INTO weight_measurement (id,user_id,day_key,source,measured_at,"
        "weight_kg,derived_at) VALUES (?,?,?,?,?,?,?)",
        (
            kw["id"],
            1,
            kw["day_key"],
            kw.get("source", "withings"),
            kw.get("measured_at"),
            kw.get("weight_kg"),
            "2026-01-01T00:00:00Z",
        ),
    )
    conn.commit()


# ---- food ------------------------------------------------------------------


def test_not_logged_day_has_no_row(migrated_conn):
    _food(migrated_conn, id="a", day_key="2026-07-10", kcal=500)
    rows = migrated_conn.execute("SELECT * FROM food_daily WHERE day_key = '2026-07-11'").fetchall()
    assert rows == []  # absence == "not logged", never a misleading 0


def test_fast_day_is_known_zero(migrated_conn):
    _food(migrated_conn, id="a", day_key="2026-07-11", entry_type="fast", kcal=0)
    row = migrated_conn.execute(
        "SELECT kcal_total, is_fast, is_complete FROM food_daily WHERE day_key='2026-07-11'"
    ).fetchone()
    assert row["kcal_total"] == 0
    assert row["is_fast"] == 1
    assert row["is_complete"] == 1


def test_partial_macros_surface_incompleteness(migrated_conn):
    _food(migrated_conn, id="a", day_key="2026-07-10", kcal=500, protein_g=40)
    _food(migrated_conn, id="b", day_key="2026-07-10", kcal=None, protein_g=20)
    row = migrated_conn.execute(
        "SELECT items_missing_kcal_n, is_complete FROM food_daily WHERE day_key='2026-07-10'"
    ).fetchone()
    assert row["items_missing_kcal_n"] == 1
    assert row["is_complete"] == 0


def test_food_daily_resolves_one_source_no_double_count(migrated_conn):
    # same day, two sources logging a meal each; manual has precedence
    _food(migrated_conn, id="a", day_key="2026-07-12", source="healthkit", kcal=800)
    _food(migrated_conn, id="b", day_key="2026-07-12", source="manual", kcal=600)
    rows = migrated_conn.execute(
        "SELECT source, kcal_total FROM food_daily WHERE day_key='2026-07-12'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "manual"
    assert rows[0]["kcal_total"] == 600  # NOT 1400


# ---- weight ----------------------------------------------------------------


def test_weight_picks_earliest_reading_of_day(migrated_conn):
    _weight(
        migrated_conn,
        id="a",
        day_key="2026-07-10",
        measured_at="2026-07-10T12:00:00Z",
        weight_kg=80.5,
    )
    _weight(
        migrated_conn,
        id="b",
        day_key="2026-07-10",
        measured_at="2026-07-10T06:00:00Z",
        weight_kg=80.0,
    )
    row = migrated_conn.execute(
        "SELECT weight_kg FROM weight_resolved_daily WHERE day_key='2026-07-10'"
    ).fetchone()
    assert row["weight_kg"] == 80.0  # morning-fasted proxy


def test_weight_source_precedence(migrated_conn):
    _weight(
        migrated_conn,
        id="a",
        day_key="2026-07-11",
        source="manual",
        measured_at="2026-07-11T06:00:00Z",
        weight_kg=85.0,
    )
    _weight(
        migrated_conn,
        id="b",
        day_key="2026-07-11",
        source="withings",
        measured_at="2026-07-11T07:00:00Z",
        weight_kg=80.2,
    )
    row = migrated_conn.execute(
        "SELECT source, weight_kg FROM weight_resolved_daily WHERE day_key='2026-07-11'"
    ).fetchone()
    assert row["source"] == "withings"
    assert row["weight_kg"] == 80.2


def test_weight_trend_ewma_matches_hand_calc(migrated_conn):
    # alpha = 0.10: t0=80.0; t1=0.1*80.2+0.9*80.0=80.02; t2=0.1*80.4+0.9*80.02=80.058
    _weight(
        migrated_conn,
        id="a",
        day_key="2026-07-10",
        measured_at="2026-07-10T06:00:00Z",
        weight_kg=80.0,
    )
    _weight(
        migrated_conn,
        id="b",
        day_key="2026-07-11",
        measured_at="2026-07-11T06:00:00Z",
        weight_kg=80.2,
    )
    _weight(
        migrated_conn,
        id="c",
        day_key="2026-07-12",
        measured_at="2026-07-12T06:00:00Z",
        weight_kg=80.4,
    )
    rows = migrated_conn.execute(
        "SELECT day_key, trend_kg FROM weight_trend ORDER BY day_key"
    ).fetchall()
    trends = [r["trend_kg"] for r in rows]
    assert trends[0] == 80.0
    assert abs(trends[1] - 80.02) < 1e-9
    assert abs(trends[2] - 80.058) < 1e-9
