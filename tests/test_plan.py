"""Plan layer (Phase 7, ADR-0013) — target math, guardrail clamps, persistence.

The numbers here are the product: the daily calorie goal steers real eating, so
the arithmetic is hand-checked and the §8.6 clamps must be provably unskippable.
"""

from __future__ import annotations

import pytest

from coach.compute.guardrails import (
    MAX_TARGET_LOSS_PCT_PER_WEEK,
    clamp_target_loss_rate,
)
from coach.compute.plan import (
    direction_for_rate,
    plan_status,
    rate_from_deadline,
    resolve_target_rate,
)
from coach.compute.trends import Insufficient
from coach.store.plan import PlanRow, active_plan, insert_plan, plan_id

# ---- guardrail clamp --------------------------------------------------------


def test_clamp_target_loss_rate_clamps_excess_loss():
    assert clamp_target_loss_rate(-2.0) == -MAX_TARGET_LOSS_PCT_PER_WEEK


def test_clamp_target_loss_rate_passes_safe_loss_gain_and_maintain():
    assert clamp_target_loss_rate(-0.5) == -0.5
    assert clamp_target_loss_rate(0.5) == 0.5  # bulk unbounded here
    assert clamp_target_loss_rate(0.0) == 0.0


def test_clamp_target_loss_rate_boundary_is_inclusive():
    # exactly at the ceiling is allowed, not clamped
    assert clamp_target_loss_rate(-MAX_TARGET_LOSS_PCT_PER_WEEK) == -MAX_TARGET_LOSS_PCT_PER_WEEK


# ---- rate helpers -----------------------------------------------------------


def test_direction_for_rate():
    assert direction_for_rate(-0.5) == "cut"
    assert direction_for_rate(0.3) == "bulk"
    assert direction_for_rate(0.0) == "maintain"


def test_rate_from_deadline_hand_calc():
    # 80 -> 76 kg over 10 weeks = -5% total / 10 = -0.5%/week
    r = rate_from_deadline(current_weight_kg=80.0, goal_weight_kg=76.0, weeks=10.0)
    assert r == pytest.approx(-0.5)


def test_rate_from_deadline_rejects_nonpositive_inputs():
    with pytest.raises(ValueError, match="weeks"):
        rate_from_deadline(current_weight_kg=80.0, goal_weight_kg=76.0, weeks=0)
    with pytest.raises(ValueError, match="current_weight_kg"):
        rate_from_deadline(current_weight_kg=0.0, goal_weight_kg=76.0, weeks=10.0)


def test_resolve_target_rate_clamps_and_notes():
    t = resolve_target_rate(-2.0)  # too aggressive
    assert t.rate_pct_per_week == -MAX_TARGET_LOSS_PCT_PER_WEEK
    assert t.clamped is True
    assert t.direction == "cut"
    assert t.note and "clamped" in t.note


def test_resolve_target_rate_leaves_safe_rate_untouched():
    t = resolve_target_rate(-0.5)
    assert t.rate_pct_per_week == -0.5
    assert t.clamped is False
    assert t.note is None


# ---- plan_status ------------------------------------------------------------


def test_plan_status_cut_calorie_goal_hand_calc():
    # tdee 2500, trend 80kg, -0.5%/week => -0.4 kg/wk => -440 kcal/day => 2060
    st = plan_status(
        direction="cut",
        target_rate_pct_per_week=-0.5,
        goal_weight_kg=None,
        tdee_kcal=2500.0,
        current_trend_kg=80.0,
    )
    assert not isinstance(st, Insufficient)
    assert st.target_rate_kg_per_week == pytest.approx(-0.4)
    assert st.daily_kcal_delta == pytest.approx(-440.0)
    assert st.calorie_goal_kcal == pytest.approx(2060.0)
    assert st.floor_clamped is False
    assert st.alerts == []


def test_plan_status_floor_clamp_wins_and_alerts():
    # low TDEE + aggressive-but-legal rate drives the raw goal below 1200
    st = plan_status(
        direction="cut",
        target_rate_pct_per_week=-1.0,
        goal_weight_kg=None,
        tdee_kcal=1300.0,
        current_trend_kg=80.0,
    )
    assert not isinstance(st, Insufficient)
    # raw = 1300 - (0.8*7700/7=880) = 420 -> clamped to 1200
    assert st.calorie_goal_kcal == pytest.approx(1200.0)
    assert st.floor_clamped is True
    assert any(a.code == "below_calorie_floor" for a in st.alerts)


def test_plan_status_insufficient_without_tdee_or_trend():
    assert isinstance(
        plan_status(
            direction="cut",
            target_rate_pct_per_week=-0.5,
            goal_weight_kg=None,
            tdee_kcal=None,
            current_trend_kg=80.0,
        ),
        Insufficient,
    )
    assert isinstance(
        plan_status(
            direction="cut",
            target_rate_pct_per_week=-0.5,
            goal_weight_kg=None,
            tdee_kcal=2500.0,
            current_trend_kg=None,
        ),
        Insufficient,
    )


def test_plan_status_projects_timeline_toward_goal():
    st = plan_status(
        direction="cut",
        target_rate_pct_per_week=-0.5,
        goal_weight_kg=76.0,
        tdee_kcal=2500.0,
        current_trend_kg=80.0,
        end_day="2026-07-26",
    )
    assert not isinstance(st, Insufficient)
    # remaining -4 kg at -0.4 kg/wk = 10 weeks = 70 days
    assert st.weeks_to_goal == pytest.approx(10.0)
    assert st.projected_goal_day == "2026-10-04"


def test_plan_status_no_projection_when_rate_moves_away_from_goal():
    # a cut (losing) but the goal is ABOVE current weight -> never reached
    st = plan_status(
        direction="cut",
        target_rate_pct_per_week=-0.5,
        goal_weight_kg=85.0,
        tdee_kcal=2500.0,
        current_trend_kg=80.0,
        end_day="2026-07-26",
    )
    assert not isinstance(st, Insufficient)
    assert st.weeks_to_goal is None
    assert st.projected_goal_day is None


def test_plan_status_maintain_no_projection_even_with_goal():
    st = plan_status(
        direction="maintain",
        target_rate_pct_per_week=0.0,
        goal_weight_kg=80.0,
        tdee_kcal=2500.0,
        current_trend_kg=80.0,
    )
    assert not isinstance(st, Insufficient)
    assert st.daily_kcal_delta == pytest.approx(0.0)
    assert st.calorie_goal_kcal == pytest.approx(2500.0)
    assert st.weeks_to_goal is None


# ---- persistence ------------------------------------------------------------


def _row(created_at: str, rate: float, *, user_id: int = 1) -> PlanRow:
    return PlanRow(
        id=plan_id(user_id, created_at),
        user_id=user_id,
        created_at=created_at,
        start_day_key=created_at[:10],
        direction="cut" if rate < 0 else "bulk" if rate > 0 else "maintain",
        target_rate_pct_per_week=rate,
        start_weight_kg=80.0,
        goal_weight_kg=76.0,
    )


def test_active_plan_none_on_empty(migrated_conn):
    assert active_plan(migrated_conn) is None


def test_insert_plan_sets_active_and_supersedes_prior(migrated_conn):
    insert_plan(migrated_conn, _row("2026-07-01T00:00:00+00:00", -0.5))
    insert_plan(migrated_conn, _row("2026-07-20T00:00:00+00:00", -0.75))
    migrated_conn.commit()

    got = active_plan(migrated_conn)
    assert got is not None
    assert got.target_rate_pct_per_week == pytest.approx(-0.75)  # the newer one

    # history preserved: the superseded row still exists, just inactive
    n = migrated_conn.execute("SELECT COUNT(*) AS c FROM plan").fetchone()["c"]
    assert n == 2
    active_count = migrated_conn.execute(
        "SELECT COUNT(*) AS c FROM plan WHERE is_active = 1"
    ).fetchone()["c"]
    assert active_count == 1


def test_plan_direction_check_constraint(migrated_conn):
    import sqlite3

    bad = _row("2026-07-01T00:00:00+00:00", -0.5)
    bad = PlanRow(**{**bad.__dict__, "direction": "shrink"})
    with pytest.raises(sqlite3.IntegrityError):
        insert_plan(migrated_conn, bad)


# ---- get_plan_status tool (DB assembly) ------------------------------------


def test_get_plan_status_tool_no_plan(migrated_conn):
    from coach.coach.tools import get_plan_status

    out = get_plan_status(migrated_conn, end="2026-07-26")
    assert out["plan"] is None
    assert out["status"] is None
    assert out["insufficient"] is None


def test_get_plan_status_tool_insufficient_without_data(migrated_conn):
    from coach.coach.tools import get_plan_status

    insert_plan(migrated_conn, _row("2026-07-01T00:00:00+00:00", -0.5))
    migrated_conn.commit()

    out = get_plan_status(migrated_conn, end="2026-07-26")
    # plan echoed back, but no TDEE/trend yet -> honest insufficient, never a goal
    assert out["plan"] is not None
    assert out["plan"]["direction"] == "cut"
    assert out["status"] is None
    assert out["insufficient"] == {"have": 0, "needed": 1}
