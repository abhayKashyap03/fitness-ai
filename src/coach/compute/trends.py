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
