"""Adaptive TDEE estimate (T3.3) — MacroFactor-style energy balance.

Expenditure is inferred from **logged intake + smoothed weight trend**, NOT from
wearable calorie estimates (which are unreliable; §8). The identity:

    mean_intake - TDEE = daily_energy_balance
    daily_energy_balance = (Δ trend_weight_kg * KCAL_PER_KG) / span_days
    =>  TDEE = mean_intake - (Δ trend_weight_kg * KCAL_PER_KG) / span_days

Using the EWMA *trend* weight (not raw scale weight) cancels day-to-day water
noise. Degrades gracefully: too few logged-intake days or no weight span returns
:class:`Insufficient` rather than a confident-but-wrong number (§2.2).

See docs/adr/0005-adaptive-tdee.md for the method + tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass

from .trends import Insufficient

# ~7700 kcal per kg of body-mass change (standard mixed-tissue approximation).
KCAL_PER_KG = 7700.0


@dataclass(frozen=True)
class DayPoint:
    day_key: str
    intake_kcal: float | None
    trend_kg: float | None


@dataclass(frozen=True)
class TDEEEstimate:
    tdee_kcal: float
    mean_intake_kcal: float
    trend_delta_kg: float
    span_days: int
    intake_days: int


def _span_days(first: str, last: str) -> int:
    from datetime import date

    d0 = date.fromisoformat(first)
    d1 = date.fromisoformat(last)
    return (d1 - d0).days


def build_window(conn, end_day: str, window: int, user_id: int = 1) -> list[DayPoint]:
    """Assemble ``window`` DayPoints ending at ``end_day`` from canonical views.

    Impure helper (DB I/O), kept separate from the pure estimator. Intake comes
    from ``food_daily.kcal_total``; trend from ``weight_trend.trend_kg``. Days
    with no data are still emitted (as None) so gaps stay explicit.
    """
    from datetime import date, timedelta

    end = date.fromisoformat(end_day)
    points: list[DayPoint] = []
    for i in range(window - 1, -1, -1):
        d = (end - timedelta(days=i)).isoformat()
        food = conn.execute(
            "SELECT kcal_total FROM food_daily WHERE user_id=? AND day_key=?",
            (user_id, d),
        ).fetchone()
        wt = conn.execute(
            "SELECT trend_kg FROM weight_trend WHERE user_id=? AND day_key=?",
            (user_id, d),
        ).fetchone()
        points.append(
            DayPoint(
                day_key=d,
                intake_kcal=food["kcal_total"] if food else None,
                trend_kg=wt["trend_kg"] if wt else None,
            )
        )
    return points


def estimate_tdee(
    days: list[DayPoint],
    *,
    kcal_per_kg: float = KCAL_PER_KG,
    min_intake_days: int = 10,
) -> TDEEEstimate | Insufficient:
    """Estimate TDEE over a window of daily points (chronological order).

    Needs at least ``min_intake_days`` days with logged intake and a weight
    trend at both ends of a non-zero calendar span.
    """
    intake_days = [d for d in days if d.intake_kcal is not None]
    trend_pts = [d for d in days if d.trend_kg is not None]

    if len(intake_days) < min_intake_days or len(trend_pts) < 2:
        return Insufficient(have=len(intake_days), needed=min_intake_days)

    first, last = trend_pts[0], trend_pts[-1]
    span = _span_days(first.day_key, last.day_key)
    if span <= 0:
        return Insufficient(have=len(trend_pts), needed=2)

    mean_intake = sum(d.intake_kcal for d in intake_days) / len(intake_days)  # type: ignore[misc]
    delta_kg = last.trend_kg - first.trend_kg  # type: ignore[operator]
    daily_balance = (delta_kg * kcal_per_kg) / span
    tdee = mean_intake - daily_balance

    return TDEEEstimate(
        tdee_kcal=round(tdee, 1),
        mean_intake_kcal=round(mean_intake, 1),
        trend_delta_kg=round(delta_kg, 4),
        span_days=span,
        intake_days=len(intake_days),
    )
