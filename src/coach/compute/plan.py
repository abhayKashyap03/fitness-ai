"""Cut/bulk plan math (Phase 7, ADR-0013) — deterministic, pure (§2.2).

The plan layer is the first thing that *steers* rather than observes. This module
turns a declared plan (a signed target rate) plus the measured state (adaptive
TDEE + smoothed weight trend) into a **daily calorie goal**, a projected
timeline, and an on/off-track read — all in code, so the LLM only ever narrates
these numbers.

Two entry styles collapse to one canonical quantity (ADR-0013):
  * a %/week rate is used directly;
  * a goal-weight + deadline is converted to a rate here (:func:`rate_from_deadline`)
    and immediately clamped to the §8.6 sustainable-loss ceiling — the deadline
    is convenience, the clamped rate is the truth.

Everything degrades to :class:`Insufficient` rather than inventing a number when
TDEE or the weight trend can't be measured (§2.7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .guardrails import (
    Alert,
    calorie_floor_alert,
    clamp_calorie_target,
    clamp_target_loss_rate,
)
from .tdee import KCAL_PER_KG
from .trends import Insufficient


@dataclass(frozen=True)
class TargetRate:
    """A resolved, §8.6-clamped plan target."""

    direction: str  # 'cut' | 'bulk' | 'maintain'
    rate_pct_per_week: float  # signed; negative = loss
    clamped: bool  # True if the requested loss was clamped to the ceiling
    note: str | None = None


@dataclass(frozen=True)
class PlanStatus:
    tdee_kcal: float
    current_trend_kg: float
    direction: str
    target_rate_pct_per_week: float
    target_rate_kg_per_week: float
    daily_kcal_delta: float  # signed; added to TDEE to get the goal
    calorie_goal_kcal: float  # after the §8.6 floor clamp
    floor_clamped: bool
    goal_weight_kg: float | None
    weeks_to_goal: float | None
    projected_goal_day: str | None
    alerts: list[Alert]


def direction_for_rate(rate_pct_per_week: float) -> str:
    """The label implied by a signed rate (0 = maintain)."""
    if rate_pct_per_week < 0:
        return "cut"
    if rate_pct_per_week > 0:
        return "bulk"
    return "maintain"


def rate_from_deadline(
    *, current_weight_kg: float, goal_weight_kg: float, weeks: float
) -> float:
    """Signed %/week rate implied by reaching ``goal`` from ``current`` in ``weeks``.

    Pure conversion (NOT yet clamped — callers pass the result through
    :func:`resolve_target_rate`). ``weeks`` must be positive.
    """
    if weeks <= 0:
        raise ValueError(f"weeks must be positive, got {weeks}")
    if current_weight_kg <= 0:
        raise ValueError(f"current_weight_kg must be positive, got {current_weight_kg}")
    total_pct = (goal_weight_kg - current_weight_kg) / current_weight_kg * 100
    return total_pct / weeks


def resolve_target_rate(rate_pct_per_week: float) -> TargetRate:
    """Clamp a requested signed rate to the §8.6 loss ceiling and label it.

    This is the single choke point every entry style flows through (ADR-0013):
    a rate that would lose weight faster than the sustainable ceiling is clamped,
    and the fact is recorded so the coach can say the timeline stretched.
    """
    clamped_rate = clamp_target_loss_rate(rate_pct_per_week)
    was_clamped = clamped_rate != rate_pct_per_week
    note = None
    if was_clamped:
        note = (
            f"requested {rate_pct_per_week:+.2f}%/week clamped to "
            f"{clamped_rate:+.2f}%/week (sustainable-loss ceiling, §8.6)"
        )
    return TargetRate(
        direction=direction_for_rate(clamped_rate),
        rate_pct_per_week=clamped_rate,
        clamped=was_clamped,
        note=note,
    )


def plan_status(
    *,
    direction: str,
    target_rate_pct_per_week: float,
    goal_weight_kg: float | None,
    tdee_kcal: float | None,
    current_trend_kg: float | None,
    end_day: str | None = None,
    kcal_per_kg: float = KCAL_PER_KG,
) -> PlanStatus | Insufficient:
    """Daily calorie goal + projection for an active plan against measured state.

    Returns :class:`Insufficient` when TDEE or the current weight trend can't be
    measured — a plan can't be steered without both, and a guessed goal would
    violate §2.2. The calorie goal always passes through the §8.6 floor clamp
    (the floor wins even for a safe rate on a low TDEE).
    """
    if tdee_kcal is None or current_trend_kg is None:
        return Insufficient(have=0, needed=1)

    rate_kg_per_week = target_rate_pct_per_week / 100 * current_trend_kg
    daily_kcal_delta = rate_kg_per_week * kcal_per_kg / 7
    raw_goal = tdee_kcal + daily_kcal_delta
    calorie_goal = clamp_calorie_target(raw_goal)
    floor_clamped = calorie_goal > raw_goal

    alerts: list[Alert] = []
    floor_alert = calorie_floor_alert(raw_goal)
    if floor_alert is not None:
        alerts.append(floor_alert)

    weeks_to_goal: float | None = None
    projected_goal_day: str | None = None
    if goal_weight_kg is not None and rate_kg_per_week != 0:
        remaining = goal_weight_kg - current_trend_kg
        # only project when the rate actually moves toward the goal
        if (remaining > 0) == (rate_kg_per_week > 0):
            weeks_to_goal = remaining / rate_kg_per_week
            if end_day is not None:
                projected_goal_day = (
                    date.fromisoformat(end_day) + timedelta(days=round(weeks_to_goal * 7))
                ).isoformat()

    return PlanStatus(
        tdee_kcal=round(tdee_kcal, 1),
        current_trend_kg=round(current_trend_kg, 3),
        direction=direction,
        target_rate_pct_per_week=target_rate_pct_per_week,
        target_rate_kg_per_week=round(rate_kg_per_week, 4),
        daily_kcal_delta=round(daily_kcal_delta, 1),
        calorie_goal_kcal=round(calorie_goal, 0),
        floor_clamped=floor_clamped,
        goal_weight_kg=goal_weight_kg,
        weeks_to_goal=round(weeks_to_goal, 1) if weeks_to_goal is not None else None,
        projected_goal_day=projected_goal_day,
        alerts=alerts,
    )
