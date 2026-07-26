"""HRV-differentiator validation harness (risk #6).

The project's bet is that recovery/HRV adds coaching signal beyond
weight + intake alone. MacroFactor deliberately excludes HRV as too noisy —
so this must be MEASURED on the user's own data, not assumed. This module is
that measurement: deterministic statistics over the canonical tables (§2.2),
returning :class:`Insufficient` rather than a misleading number whenever the
data can't support an honest answer (§2.7).

What it reports, per metric window:
  * **Noise profile** — day-to-day coefficient of variation and lag-1
    autocorrelation of HRV. If HRV is mostly noise (low autocorrelation),
    acting on daily readings is astrology.
  * **Predictive probes** — correlation of today's HRV deviation-from-baseline
    with tomorrow's recovery score and tomorrow's training strain. Nonzero,
    stable correlations are necessary (not sufficient) for HRV-informed
    coaching to beat weight+intake alone.

Interpretation stays with the human; this module only computes. No causal
claims — these are observational correlations on n=1 data.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from .trends import Insufficient

# Below this many pairs a correlation is anecdote, not measurement.
MIN_PAIRS = 14

# Verdict thresholds (deterministic, §2.2 — the answer is code, not model or
# human judgment). Deliberately conservative: HRV must clear a real bar to be
# called signal, because the whole point is to test the differentiator honestly
# (risk #6), not to flatter it.
#   * autocorrelation this high means today's reading carries real information
#     about tomorrow's (a pure-noise series sits near 0).
MIN_AUTOCORR = 0.30
#   * at least one deviation->next-day correlation this strong means HRV
#     deviation tracks something a coach could act on.
MIN_DEV_CORR = 0.20

# Verdict labels.
SIGNAL = "signal"          # HRV clears both bars — worth informing coaching
NOISE = "noise"            # enough data, bars not cleared — indistinguishable
INSUFFICIENT = "insufficient"  # not enough paired days to judge either way


@dataclass(frozen=True)
class Correlation:
    r: float
    n: int


def pearson(xs: list[float], ys: list[float]) -> Correlation | Insufficient:
    """Pearson r over paired samples; Insufficient below :data:`MIN_PAIRS`.

    Also Insufficient when either side is constant (r undefined — a zero
    variance denominator is absence of signal, not r=0).
    """
    if len(xs) != len(ys):
        raise ValueError(f"paired samples must match: {len(xs)} vs {len(ys)}")
    n = len(xs)
    if n < MIN_PAIRS:
        return Insufficient(have=n, needed=MIN_PAIRS)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return Insufficient(have=0, needed=1)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return Correlation(r=sxy / math.sqrt(sxx * syy), n=n)


def lag1_autocorrelation(series: dict[str, float]) -> Correlation | Insufficient:
    """Correlation of a daily series with itself shifted by one calendar day.

    Pairs only CONSECUTIVE days (a gap breaks the pair — no interpolation,
    §2.7). High values mean today's reading carries real information about
    tomorrow's; values near zero mean day-to-day readings are mostly noise.
    """
    xs, ys = [], []
    for day_key, value in series.items():
        nxt = (date.fromisoformat(day_key) + timedelta(days=1)).isoformat()
        if nxt in series:
            xs.append(value)
            ys.append(series[nxt])
    return pearson(xs, ys)


def coefficient_of_variation(values: list[float]) -> float | Insufficient:
    """Population CV (stdev/mean) — the day-to-day noise magnitude."""
    n = len(values)
    if n < MIN_PAIRS:
        return Insufficient(have=n, needed=MIN_PAIRS)
    mean = sum(values) / n
    if mean == 0:
        return Insufficient(have=0, needed=1)
    var = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(var) / abs(mean)


def lagged_pairs(
    a: dict[str, float], b: dict[str, float], *, lag_days: int = 1
) -> tuple[list[float], list[float]]:
    """Pair ``a[day]`` with ``b[day + lag_days]`` where BOTH exist.

    Days missing on either side simply produce no pair (§2.7) — the pair count
    is part of the result's honesty, surfaced via Correlation.n.
    """
    xs, ys = [], []
    for day_key, value in a.items():
        target = (date.fromisoformat(day_key) + timedelta(days=lag_days)).isoformat()
        if target in b:
            xs.append(value)
            ys.append(b[target])
    return xs, ys


def deviation_series(series: dict[str, float], *, window: int = 7) -> dict[str, float]:
    """Per-day % deviation from the trailing ``window``-day baseline.

    A day only gets a deviation when a FULL prior window of consecutive-or-not
    readings exists (baseline = mean of the prior ``window`` observed values,
    by day order). Mirrors trends.baseline_deviation, vectorized over the
    series. Days without enough history are absent, not zero.
    """
    days = sorted(series)
    out: dict[str, float] = {}
    for i, day_key in enumerate(days):
        if i < window:
            continue
        prior = [series[d] for d in days[i - window : i]]
        baseline = sum(prior) / window
        if baseline == 0:
            continue
        out[day_key] = (series[day_key] - baseline) / baseline * 100
    return out


# ---- DB assembly (impure boundary; math above stays pure) -------------------


def _daily(conn: sqlite3.Connection, sql: str, args: tuple) -> dict[str, float]:
    return {
        r["day_key"]: float(r["v"])
        for r in conn.execute(sql, args)
        if r["v"] is not None
    }


def hrv_verdict(
    autocorr: Correlation | Insufficient,
    dev_correlations: list[Correlation | Insufficient],
) -> tuple[str, str]:
    """Deterministic signal/noise call from the computed stats (§2.2).

    Returns ``(verdict, one-line rationale)``. The verdict is driven entirely by
    the real numbers against fixed thresholds — no static "it's noisy" string,
    no model judgment:

      * INSUFFICIENT — the autocorrelation itself couldn't be measured (too few
        paired days). Can't judge either way; get more data.
      * SIGNAL — today's HRV predicts tomorrow's (autocorr >= MIN_AUTOCORR) AND
        at least one deviation->next-day correlation clears MIN_DEV_CORR. Both
        bars: HRV is both stable enough to trust and predictive of something.
      * NOISE — enough data, bars not cleared. Indistinguishable from noise on
        THIS user's history (the honest null result risk #6 warns about).
    """
    if isinstance(autocorr, Insufficient):
        return INSUFFICIENT, (
            f"only {autocorr.have} consecutive-day HRV pairs; need "
            f"{autocorr.needed} to judge."
        )
    measured = [c for c in dev_correlations if isinstance(c, Correlation)]
    strongest = max((abs(c.r) for c in measured), default=0.0)
    ac = abs(autocorr.r)

    if ac >= MIN_AUTOCORR and strongest >= MIN_DEV_CORR:
        return SIGNAL, (
            f"autocorrelation {autocorr.r:+.2f} (>= {MIN_AUTOCORR}) and a "
            f"next-day correlation of {strongest:.2f} (>= {MIN_DEV_CORR}) — HRV "
            "carries actionable signal here."
        )
    reasons = []
    if ac < MIN_AUTOCORR:
        reasons.append(f"autocorrelation {autocorr.r:+.2f} < {MIN_AUTOCORR}")
    if strongest < MIN_DEV_CORR:
        reasons.append(f"best next-day correlation {strongest:.2f} < {MIN_DEV_CORR}")
    return NOISE, (
        "indistinguishable from noise on your data (" + "; ".join(reasons) + ")."
    )


@dataclass(frozen=True)
class HrvValidationReport:
    window_days: int
    hrv_days: int
    hrv_cv: float | Insufficient
    hrv_lag1_autocorr: Correlation | Insufficient
    dev_vs_next_score: Correlation | Insufficient
    dev_vs_next_strain: Correlation | Insufficient
    verdict: str = NOISE
    rationale: str = ""


def hrv_validation_report(
    conn: sqlite3.Connection, *, user_id: int = 1, end: str, window: int = 90
) -> HrvValidationReport:
    """Assemble the report over ``[end - window, end]`` from canonical tables.

    Reads the resolver-preferred recovery rows (is_official first — during the
    calibration window WHOOP's numbers are ground truth) and daily total strain
    from workouts. Every statistic degrades to Insufficient independently.
    """
    start = (date.fromisoformat(end) - timedelta(days=window)).isoformat()

    hrv = _daily(
        conn,
        "SELECT day_key, hrv_rmssd_ms AS v FROM recovery "
        "WHERE user_id=? AND day_key BETWEEN ? AND ? AND hrv_rmssd_ms IS NOT NULL "
        "ORDER BY day_key",
        (user_id, start, end),
    )
    score = _daily(
        conn,
        "SELECT day_key, score AS v FROM recovery "
        "WHERE user_id=? AND day_key BETWEEN ? AND ? AND score IS NOT NULL "
        "ORDER BY day_key",
        (user_id, start, end),
    )
    strain = _daily(
        conn,
        "SELECT day_key, SUM(strain) AS v FROM workout "
        "WHERE user_id=? AND day_key BETWEEN ? AND ? AND strain IS NOT NULL "
        "GROUP BY day_key ORDER BY day_key",
        (user_id, start, end),
    )

    dev = deviation_series(hrv)
    autocorr = lag1_autocorrelation(hrv)
    dev_score = pearson(*lagged_pairs(dev, score, lag_days=1))
    dev_strain = pearson(*lagged_pairs(dev, strain, lag_days=1))
    verdict, rationale = hrv_verdict(autocorr, [dev_score, dev_strain])
    return HrvValidationReport(
        window_days=window,
        hrv_days=len(hrv),
        hrv_cv=coefficient_of_variation(list(hrv.values())),
        hrv_lag1_autocorr=autocorr,
        dev_vs_next_score=dev_score,
        dev_vs_next_strain=dev_strain,
        verdict=verdict,
        rationale=rationale,
    )
