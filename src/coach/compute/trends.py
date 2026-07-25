"""Pure trend math (T3.2). No I/O, no DB.

Every function that can't produce an honest number returns :class:`Insufficient`
instead of a misleading one (§2.2: never interpolate or invent). Callers must
handle both arms — that's the point.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Insufficient:
    """Not enough data to compute honestly."""

    have: int
    needed: int

    def __bool__(self) -> bool:  # so `if result:` reads naturally
        return False


@dataclass(frozen=True)
class Baseline:
    baseline: float
    latest: float
    deviation: float
    deviation_pct: float


def ewma_series(values: list[float], alpha: float = 0.1) -> list[float]:
    """Exponentially-weighted moving average series.

    trend[0] = values[0]; trend[t] = alpha*values[t] + (1-alpha)*trend[t-1].
    """
    if not 0 < alpha <= 1:
        raise ValueError(f"alpha must be in (0, 1], got {alpha}")
    if not values:
        return []
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def gap_aware_ewma(
    points: list[tuple[str, float]], alpha: float = 0.1
) -> list[tuple[str, float]]:
    """EWMA over irregularly-sampled daily points (``[(day_key, value), ...]``).

    Row-based EWMA treats a weigh-in after a 14-day travel gap exactly like one
    after a single day — the stale trend keeps 90% of its weight and the trend
    lags reality for weeks (risk #3: async/partial data). Fix: scale the
    smoothing to the calendar, not the row count. For a gap of ``g`` days the
    effective alpha is ``1 - (1-alpha)^g`` — algebraically identical to running
    the daily EWMA ``g`` times toward the new observation, so evenly-sampled
    data is completely unchanged (g=1 -> plain alpha) and a long gap lets the
    new reading pull harder exactly as if the days had passed one at a time.

    Points must be sorted ascending by day_key with no duplicate days (the
    resolver view guarantees one row per day). Missing days are NOT
    interpolated — absence stays absence (§2.7); only the decay accounts for
    elapsed time.
    """
    if not 0 < alpha <= 1:
        raise ValueError(f"alpha must be in (0, 1], got {alpha}")
    if not points:
        return []
    from datetime import date

    out = [points[0]]
    prev_day, prev_trend = date.fromisoformat(points[0][0]), points[0][1]
    for day_key, value in points[1:]:
        day = date.fromisoformat(day_key)
        gap = (day - prev_day).days
        if gap <= 0:
            raise ValueError(f"points must be strictly ascending by day; got {day_key} after {prev_day}")
        eff = 1 - (1 - alpha) ** gap
        prev_trend = eff * value + (1 - eff) * prev_trend
        out.append((day_key, prev_trend))
        prev_day = day
    return out


def latest_ewma(values: list[float], alpha: float = 0.1) -> float | Insufficient:
    if not values:
        return Insufficient(have=0, needed=1)
    return ewma_series(values, alpha)[-1]


def rolling_mean(values: list[float], window: int) -> float | Insufficient:
    """Mean of the last ``window`` values, or Insufficient if fewer exist."""
    if window <= 0:
        raise ValueError("window must be positive")
    if len(values) < window:
        return Insufficient(have=len(values), needed=window)
    tail = values[-window:]
    return sum(tail) / window


def baseline_deviation(values: list[float], window: int = 7) -> Baseline | Insufficient:
    """Compare the latest value to a trailing baseline over the PRIOR ``window``.

    Baseline excludes the latest point so a reading is compared against its own
    recent history (e.g. today's HRV vs the prior 7-day mean). Needs
    ``window + 1`` points.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    needed = window + 1
    if len(values) < needed:
        return Insufficient(have=len(values), needed=needed)
    latest = values[-1]
    prior = values[-needed:-1]
    baseline = sum(prior) / window
    deviation = latest - baseline
    pct = (deviation / baseline * 100) if baseline else 0.0
    return Baseline(
        baseline=baseline,
        latest=latest,
        deviation=deviation,
        deviation_pct=pct,
    )
