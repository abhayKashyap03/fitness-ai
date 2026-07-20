"""Deterministic health-safety guardrails (§8.6).

Hard limits live **in code**, not in a prompt the model could argue past. These
functions compute safety flags from canonical numbers and clamp unsafe targets.
The coach *surfaces* what these return; it never overrides them and never invents
its own numbers (§2.2). Everything here is pure — no I/O — so it is fully
testable and identically enforced every call.

Scope today: the weight-trend rate check is food-independent and runs now. The
calorie-floor clamp is ready for the Phase-4 target-setting layer; it fires only
when a target is proposed. These are floors/ceilings, not coaching advice — a
low HRV means "train lighter," never a diagnosis (§8.6).
"""

from __future__ import annotations

from dataclasses import dataclass

from .trends import Insufficient

# ---- hard limits (code-enforced, §8.6) -------------------------------------
# A sustainable cut tops out around ~1% of body-mass per week; beyond that,
# lean-mass loss and rebound risk climb. These are ceilings the coach cannot
# recommend past — not targets to hit.
WARN_LOSS_PCT_PER_WEEK = 1.0
CRITICAL_LOSS_PCT_PER_WEEK = 1.5
# Absolute intake floor. Conservative, sex-agnostic v0; a lower bound no target
# may drop beneath regardless of what the deficit math "wants".
MIN_CALORIE_FLOOR_KCAL = 1200.0


@dataclass(frozen=True)
class Alert:
    """A surfaced safety flag. ``message`` is code-authored (never model prose)."""

    level: str  # 'warn' | 'critical'
    code: str
    message: str
    evidence: dict


@dataclass(frozen=True)
class TrendPoint:
    day_key: str
    trend_kg: float


def _span_days(first: str, last: str) -> int:
    from datetime import date

    return (date.fromisoformat(last) - date.fromisoformat(first)).days


def weight_loss_rate_alert(series: list[TrendPoint]) -> Alert | None | Insufficient:
    """Flag an unsafe rate of weight loss from the EWMA trend.

    Uses the smoothed trend (not noisy raw weight). Needs >= 2 points spanning a
    positive number of days. Returns:
      * ``Insufficient`` when there isn't enough to judge honestly,
      * ``None`` when the rate is within safe limits (including weight *gain*),
      * an ``Alert`` (warn/critical) when loss exceeds the code limits.
    """
    if len(series) < 2:
        return Insufficient(have=len(series), needed=2)
    first, last = series[0], series[-1]
    span = _span_days(first.day_key, last.day_key)
    if span <= 0:
        return Insufficient(have=len(series), needed=2)

    loss_kg = first.trend_kg - last.trend_kg  # positive => losing
    kg_per_week = loss_kg / span * 7
    denom = last.trend_kg
    pct_per_week = (kg_per_week / denom * 100) if denom else 0.0

    if pct_per_week < WARN_LOSS_PCT_PER_WEEK:
        return None  # safe, or gaining

    evidence = {
        "kg_per_week": round(kg_per_week, 3),
        "pct_per_week": round(pct_per_week, 3),
        "span_days": span,
        "from_trend_kg": round(first.trend_kg, 3),
        "to_trend_kg": round(last.trend_kg, 3),
    }
    if pct_per_week >= CRITICAL_LOSS_PCT_PER_WEEK:
        return Alert(
            level="critical",
            code="rapid_weight_loss",
            message=(
                f"Weight-trend loss is {pct_per_week:.1f}%/week — above the "
                f"{CRITICAL_LOSS_PCT_PER_WEEK:.1f}%/week hard limit. Ease the "
                "deficit; this rate risks lean-mass loss."
            ),
            evidence=evidence,
        )
    return Alert(
        level="warn",
        code="fast_weight_loss",
        message=(
            f"Weight-trend loss is {pct_per_week:.1f}%/week — over the "
            f"{WARN_LOSS_PCT_PER_WEEK:.1f}%/week sustainable ceiling."
        ),
        evidence=evidence,
    )


def clamp_calorie_target(
    target_kcal: float, *, floor: float = MIN_CALORIE_FLOOR_KCAL
) -> float:
    """Return a target never below the hard floor. The floor wins, always."""
    return max(target_kcal, floor)


def calorie_floor_alert(
    target_kcal: float, *, floor: float = MIN_CALORIE_FLOOR_KCAL
) -> Alert | None:
    """Alert when a proposed intake target sits below the hard floor."""
    if target_kcal >= floor:
        return None
    return Alert(
        level="critical",
        code="below_calorie_floor",
        message=(
            f"Proposed intake {target_kcal:.0f} kcal is below the "
            f"{floor:.0f} kcal hard floor; clamped up to the floor."
        ),
        evidence={"proposed_kcal": round(target_kcal, 1), "floor_kcal": floor},
    )
