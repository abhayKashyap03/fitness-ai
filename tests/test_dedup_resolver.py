"""Workout dedup grouping (T2.6) and recovery resolver precedence (T2.7)."""

from __future__ import annotations

from coach.normalize.dedup import WkSlot, assign_session_groups


def _slot(id_, start, end, sport="run", user=1):
    return WkSlot(id=id_, user_id=user, sport_type=sport, start_at=start, end_at=end)


def test_same_workout_from_two_sources_one_group():
    a = _slot("a", "2026-07-10T12:00:00Z", "2026-07-10T12:45:00Z")
    b = _slot("b", "2026-07-10T12:02:00Z", "2026-07-10T12:47:00Z")  # +2 min, overlaps
    groups = assign_session_groups([a, b], tolerance_s=300)
    assert groups["a"] == groups["b"]
    assert len(set(groups.values())) == 1


def test_back_to_back_distinct_two_groups():
    a = _slot("a", "2026-07-10T12:00:00Z", "2026-07-10T12:30:00Z")
    b = _slot("b", "2026-07-10T12:40:00Z", "2026-07-10T13:10:00Z")  # gap 10 min, no overlap
    groups = assign_session_groups([a, b], tolerance_s=300)
    assert groups["a"] != groups["b"]
    assert len(set(groups.values())) == 2


def test_overlapping_but_different_sport_two_groups():
    a = _slot("a", "2026-07-10T12:00:00Z", "2026-07-10T12:45:00Z", sport="run")
    b = _slot("b", "2026-07-10T12:01:00Z", "2026-07-10T12:46:00Z", sport="strength")
    groups = assign_session_groups([a, b], tolerance_s=300)
    assert groups["a"] != groups["b"]


def test_group_id_deterministic():
    a = _slot("a", "2026-07-10T12:00:00Z", "2026-07-10T12:45:00Z")
    b = _slot("b", "2026-07-10T12:02:00Z", "2026-07-10T12:47:00Z")
    g1 = assign_session_groups([a, b])
    g2 = assign_session_groups([b, a])  # order-independent
    assert set(g1.values()) == set(g2.values())


# ---- resolver: precedence flip changes the winner, no data mutation --------


def _add_recovery(conn, *, source, day, score, method):
    conn.execute(
        "INSERT INTO recovery (id, user_id, day_key, source, score, score_method, "
        "is_official, derived_at) VALUES (?,?,?,?,?,?,?,?)",
        (f"{source}:{day}", 1, day, source, score, method, 1, "2026-01-01T00:00:00Z"),
    )
    conn.commit()


def test_resolver_picks_highest_precedence_source(migrated_conn):
    # both sources have a recovery for the same day
    _add_recovery(
        migrated_conn, source="whoop_api", day="2026-07-10", score=66, method="whoop_proprietary"
    )
    _add_recovery(
        migrated_conn, source="whoop_ble", day="2026-07-10", score=70, method="rmssd_baseline_v1"
    )
    row = migrated_conn.execute(
        "SELECT source, score FROM recovery_resolved WHERE day_key='2026-07-10'"
    ).fetchone()
    assert row["source"] == "whoop_api"  # official wins today
    # both raw rows still present — nothing mutated/deleted
    assert migrated_conn.execute("SELECT COUNT(*) FROM recovery").fetchone()[0] == 2


def test_resolver_flip_changes_winner(migrated_conn, db_path):
    """Reordering the CASE (simulated via a swapped view) flips the winner with
    zero data mutation — proving precedence is a read-time rule."""
    _add_recovery(
        migrated_conn, source="whoop_api", day="2026-07-10", score=66, method="whoop_proprietary"
    )
    _add_recovery(
        migrated_conn, source="whoop_ble", day="2026-07-10", score=70, method="rmssd_baseline_v1"
    )

    # post-membership precedence: whoop_ble wins. This is the one-line reorder.
    migrated_conn.executescript(
        """
        DROP VIEW recovery_resolved;
        CREATE VIEW recovery_resolved AS
        WITH ranked AS (
          SELECT r.*, ROW_NUMBER() OVER (
            PARTITION BY user_id, day_key
            ORDER BY CASE source WHEN 'whoop_ble' THEN 1 WHEN 'whoop_api' THEN 2 ELSE 9 END
          ) AS rnk FROM recovery r
        ) SELECT * FROM ranked WHERE rnk = 1;
        """
    )
    row = migrated_conn.execute(
        "SELECT source FROM recovery_resolved WHERE day_key='2026-07-10'"
    ).fetchone()
    assert row["source"] == "whoop_ble"  # winner flipped, data untouched
    assert migrated_conn.execute("SELECT COUNT(*) FROM recovery").fetchone()[0] == 2
