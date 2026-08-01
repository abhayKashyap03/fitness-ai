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
    adherence_label,
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


# ---- adherence (the "already started" case) --------------------------------


def test_adherence_label_on_track_ahead_behind():
    # target -0.4 kg/wk (a cut)
    assert adherence_label(-0.40, -0.40) == "on_track"
    assert adherence_label(-0.60, -0.40) == "ahead"  # losing faster
    assert adherence_label(-0.20, -0.40) == "behind"  # losing slower


def test_adherence_label_wrong_way_when_gaining_during_cut():
    assert adherence_label(0.30, -0.40) == "wrong_way"


def test_adherence_label_maintain_band():
    assert adherence_label(0.10, 0.0) == "on_track"
    assert adherence_label(0.40, 0.0) == "wrong_way"


def test_plan_status_backdated_start_shows_progress_and_adherence():
    # started 80kg on 2026-06-26, now 78.4kg on 2026-07-26 (30 days)
    # target -0.5%/wk of 78.4 = -0.392 kg/wk; actual = -1.6kg/30d*7 = -0.373 kg/wk
    st = plan_status(
        direction="cut",
        target_rate_pct_per_week=-0.5,
        goal_weight_kg=76.0,
        tdee_kcal=2500.0,
        current_trend_kg=78.4,
        end_day="2026-07-26",
        start_day_key="2026-06-26",
        start_weight_kg=80.0,
    )
    assert not isinstance(st, Insufficient)
    assert st.elapsed_days == 30
    assert st.kg_changed_so_far == pytest.approx(-1.6)
    assert st.actual_rate_kg_per_week == pytest.approx(-0.3733, abs=1e-3)
    assert st.adherence == "on_track"  # within 25% of target


def test_plan_status_same_day_plan_has_no_progress():
    # plan set today: start == end, so nothing to show yet (§2.7, not a zero)
    st = plan_status(
        direction="cut",
        target_rate_pct_per_week=-0.5,
        goal_weight_kg=None,
        tdee_kcal=2500.0,
        current_trend_kg=80.0,
        end_day="2026-07-26",
        start_day_key="2026-07-26",
        start_weight_kg=80.0,
    )
    assert not isinstance(st, Insufficient)
    assert st.elapsed_days is None
    assert st.actual_rate_kg_per_week is None
    assert st.adherence is None


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
    # The REAL reason propagates (TDEE needs 10 logged-intake days), not a
    # flattened "need 1, have 0" that tells the user nothing actionable.
    assert out["insufficient"] == {"have": 0, "needed": 10}


# ---- the §8.6 floor must stretch the timeline, not fake it ------------------


def _clamped():
    """TDEE 2000 with a -1%/wk target on 80kg: implies 1114 kcal, below the floor."""
    return plan_status(
        direction="cut",
        target_rate_pct_per_week=-1.0,
        goal_weight_kg=72.0,
        tdee_kcal=2000.0,
        current_trend_kg=80.0,
        end_day="2026-07-28",
    )


def test_floor_clamped_goal_is_self_consistent():
    """goal == TDEE + effective delta, so the numbers on screen add up."""
    st = _clamped()
    assert not isinstance(st, Insufficient)
    assert st.floor_clamped is True
    assert st.calorie_goal_kcal == pytest.approx(2000.0 + st.effective_daily_kcal_delta, abs=0.5)
    # the target's implied delta is steeper than what the floor actually allows
    assert st.daily_kcal_delta < st.effective_daily_kcal_delta


def test_floor_clamped_projection_uses_the_achievable_rate():
    """ADR-0013 promises the timeline stretches rather than promising an unsafe
    date. Projecting from the target rate would promise exactly that date."""
    st = _clamped()
    assert not isinstance(st, Insufficient)
    # floor allows 2000-1200 = 800 kcal/day = 0.727 kg/wk, not the 0.8 target
    assert st.effective_rate_kg_per_week == pytest.approx(-0.7273, abs=1e-3)
    assert abs(st.effective_rate_kg_per_week) < abs(st.target_rate_kg_per_week)
    # 8 kg to go: 11.0 weeks at the achievable rate, 10.0 at the target
    assert st.weeks_to_goal == pytest.approx(11.0, abs=0.1)


def test_unclamped_plan_is_unchanged_by_the_effective_rate():
    """The fix must be a no-op whenever the floor doesn't bind."""
    st = plan_status(
        direction="cut",
        target_rate_pct_per_week=-0.5,
        goal_weight_kg=76.0,
        tdee_kcal=2500.0,
        current_trend_kg=80.0,
        end_day="2026-07-26",
    )
    assert not isinstance(st, Insufficient)
    assert st.floor_clamped is False
    assert st.effective_rate_kg_per_week == pytest.approx(st.target_rate_kg_per_week)
    assert st.effective_daily_kcal_delta == pytest.approx(st.daily_kcal_delta)
    assert st.projected_goal_day == "2026-10-04"  # unchanged from before the fix


def test_clamped_adherence_is_judged_against_what_is_achievable():
    """Following a clamped plan perfectly must not read as 'behind' forever."""
    st = plan_status(
        direction="cut",
        target_rate_pct_per_week=-1.0,
        goal_weight_kg=72.0,
        tdee_kcal=2000.0,
        current_trend_kg=80.0,
        end_day="2026-07-28",
        start_day_key="2026-06-28",
        start_weight_kg=83.12,  # -3.12 kg / 30d = -0.728 kg/wk, the achievable rate
    )
    assert not isinstance(st, Insufficient)
    assert st.adherence == "on_track"


# ---- protein target (was stored but never read) -----------------------------
#
# `plan set --protein` wrote protein_g_per_kg to the plan row from the start, and
# nothing ever consumed it — the 0010 migration comment said "future use". So a
# user could set a protein target and the tool would silently ignore it. The
# 2026-07-26 placeholder sweep missed this because it looked for FAKE OUTPUT, and
# this produced no output at all.


def _st(**kw):
    base = dict(
        direction="cut",
        target_rate_pct_per_week=-0.5,
        goal_weight_kg=75.0,
        tdee_kcal=2400.0,
        current_trend_kg=80.0,
        end_day="2026-07-30",
    )
    base.update(kw)
    st = plan_status(**base)
    assert not isinstance(st, Insufficient)
    return st


def test_no_protein_figure_means_no_target():
    """Absent stays absent — a recommended g/kg is a coaching opinion, not a
    measurement, so the tool must not supply one the user never chose."""
    st = _st()
    assert st.protein_target_g_per_day is None
    assert st.protein_gap_g is None
    assert st.protein_met is None


def test_protein_target_scales_off_the_trend_weight():
    # 1.8 g/kg * 80.0 kg trend = 144.0 g/day
    st = _st(protein_g_per_kg=1.8)
    assert st.protein_target_g_per_day == pytest.approx(144.0)


def test_protein_target_uses_trend_not_a_noisy_scale_reading():
    """A target that jumps with daily scale noise is not a target."""
    lean = _st(protein_g_per_kg=2.0, current_trend_kg=78.0)
    heavier = _st(protein_g_per_kg=2.0, current_trend_kg=82.0)
    assert lean.protein_target_g_per_day == pytest.approx(156.0)  # 2.0 * 78
    assert heavier.protein_target_g_per_day == pytest.approx(164.0)  # 2.0 * 82


def test_logged_protein_above_target_is_met():
    # target 1.6 * 80 = 128 ; logged 150 -> +22 over
    st = _st(protein_g_per_kg=1.6, logged_protein_g=150.0)
    assert st.protein_target_g_per_day == pytest.approx(128.0)
    assert st.protein_gap_g == pytest.approx(22.0)
    assert st.protein_met is True


def test_logged_protein_below_target_is_short():
    # target 128 ; logged 100 -> -28 short
    st = _st(protein_g_per_kg=1.6, logged_protein_g=100.0)
    assert st.protein_gap_g == pytest.approx(-28.0)
    assert st.protein_met is False


def test_exactly_hitting_the_target_counts_as_met():
    st = _st(protein_g_per_kg=1.6, logged_protein_g=128.0)
    assert st.protein_gap_g == pytest.approx(0.0)
    assert st.protein_met is True


def test_unlogged_protein_is_not_a_missed_target():
    """The load-bearing case: not logged != ate none (§2.7).

    Scoring an unlogged day as 'short' would tell the user they missed a target
    on a day the tool has no idea about.
    """
    st = _st(protein_g_per_kg=1.8, logged_protein_g=None)
    assert st.protein_target_g_per_day == pytest.approx(144.0)  # target still known
    assert st.protein_logged_g is None
    assert st.protein_gap_g is None
    assert st.protein_met is None  # NOT False


def test_protein_does_not_disturb_the_calorie_goal():
    """A macro target must not change the energy math it sits beside."""
    without = _st()
    with_protein = _st(protein_g_per_kg=2.0, logged_protein_g=120.0)
    assert with_protein.calorie_goal_kcal == without.calorie_goal_kcal
    assert with_protein.effective_rate_kg_per_week == without.effective_rate_kg_per_week
    assert with_protein.alerts == without.alerts


def test_protein_target_survives_the_tool_boundary(migrated_conn):
    """End to end: a plan set with --protein must reach get_plan_status."""
    from coach.coach.tools import get_plan_status

    created = "2026-07-01T00:00:00+00:00"
    insert_plan(
        migrated_conn,
        PlanRow(
            id=plan_id(1, created),
            user_id=1,
            created_at=created,
            start_day_key="2026-07-01",
            direction="cut",
            target_rate_pct_per_week=-0.5,
            start_weight_kg=82.0,
            goal_weight_kg=78.0,
            protein_g_per_kg=1.8,
        ),
    )
    migrated_conn.commit()
    out = get_plan_status(migrated_conn, end="2026-07-30")
    # the g/kg figure is exposed on the plan even when TDEE is insufficient,
    # so the user can see what they set rather than wondering if it took
    assert out["plan"]["protein_g_per_kg"] == 1.8
