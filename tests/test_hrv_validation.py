"""HRV validation harness — hand-computed fixtures, no network (risk #6).

The statistics are the product here: they must be right, and they must refuse
to answer (Insufficient) rather than mislead on thin data (§2.2/§2.7).
"""

from __future__ import annotations

import pytest

from coach.compute.hrv_validation import (
    MIN_PAIRS,
    Correlation,
    coefficient_of_variation,
    deviation_series,
    hrv_validation_report,
    lag1_autocorrelation,
    lagged_pairs,
    pearson,
)
from coach.compute.trends import Insufficient


def _days(start_day: int, values: list[float], month: int = 3) -> dict[str, float]:
    return {f"2026-{month:02d}-{start_day + i:02d}": v for i, v in enumerate(values)}


# ---- pearson ----------------------------------------------------------------


def test_pearson_perfect_positive_and_negative():
    xs = [float(i) for i in range(MIN_PAIRS)]
    up = pearson(xs, [2 * x + 1 for x in xs])
    down = pearson(xs, [-x for x in xs])
    assert isinstance(up, Correlation) and up.r == pytest.approx(1.0)
    assert isinstance(down, Correlation) and down.r == pytest.approx(-1.0)
    assert up.n == MIN_PAIRS


def test_pearson_below_min_pairs_is_insufficient():
    xs = [float(i) for i in range(MIN_PAIRS - 1)]
    r = pearson(xs, xs)
    assert isinstance(r, Insufficient)
    assert r.have == MIN_PAIRS - 1 and r.needed == MIN_PAIRS


def test_pearson_constant_side_is_insufficient_not_zero():
    xs = [float(i) for i in range(MIN_PAIRS)]
    assert isinstance(pearson(xs, [5.0] * MIN_PAIRS), Insufficient)


def test_pearson_rejects_mismatched_lengths():
    with pytest.raises(ValueError, match="paired"):
        pearson([1.0], [1.0, 2.0])


# ---- series helpers ---------------------------------------------------------


def test_lag1_autocorrelation_pairs_only_consecutive_days():
    # 03-01..03-03 consecutive, then a gap, then 03-10 (pairs: 2, not 3)
    series = {**_days(1, [50.0, 55.0, 52.0]), "2026-03-10": 60.0}
    # expected pairing by hand: (01->02), (02->03) only — the gap breaks 03->10
    r = lag1_autocorrelation(series)
    assert isinstance(r, Insufficient)  # 2 pairs < MIN_PAIRS, but honestly counted
    assert r.have == 2


def test_lagged_pairs_skips_missing_days_no_interpolation():
    a = _days(1, [1.0, 2.0, 3.0])          # 03-01..03-03
    b = {**_days(2, [10.0]), "2026-03-04": 30.0}  # 03-02 and 03-04 only
    xs, ys = lagged_pairs(a, b, lag_days=1)
    # 03-01 -> 03-02 ok; 03-02 -> 03-03 missing in b; 03-03 -> 03-04 ok
    assert xs == [1.0, 3.0]
    assert ys == [10.0, 30.0]


def test_deviation_series_needs_full_prior_window():
    series = _days(1, [50.0] * 7 + [60.0])  # 7-day flat baseline, then +20%
    dev = deviation_series(series, window=7)
    assert list(dev) == ["2026-03-08"]  # first 7 days have no full prior window
    assert dev["2026-03-08"] == pytest.approx(20.0)


def test_coefficient_of_variation_hand_calc():
    vals = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 14.0, 6.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    cv = coefficient_of_variation(vals)
    assert isinstance(cv, float)
    # mean=10; var=(4^2+4^2)/14=2.2857; stdev=1.51186; CV=stdev/mean
    assert cv == pytest.approx(0.1511858, abs=1e-6)
    assert isinstance(coefficient_of_variation([1.0] * (MIN_PAIRS - 1)), Insufficient)


# ---- DB assembly ------------------------------------------------------------


def _seed_recovery(conn, day: str, hrv: float, score: float) -> None:
    conn.execute(
        "INSERT INTO recovery (id, user_id, day_key, source, score_method, is_official, "
        "hrv_rmssd_ms, score, raw_ref, derived_at) "
        "VALUES (?, 1, ?, 'whoop_api', 'whoop_proprietary', 1, ?, ?, NULL, "
        "'2026-01-01T00:00:00+00:00')",
        (f"rec:t:{day}", day, hrv, score),
    )


def test_report_assembles_and_degrades_honestly(migrated_conn):
    # 30 consecutive days of recovery; NO workouts at all
    for i in range(30):
        day = f"2026-03-{i + 1:02d}"
        _seed_recovery(migrated_conn, day, hrv=50.0 + (i % 5), score=60.0 + (i % 7))
    migrated_conn.commit()

    rep = hrv_validation_report(migrated_conn, end="2026-03-30", window=90)
    assert rep.hrv_days == 30
    assert isinstance(rep.hrv_cv, float)
    assert isinstance(rep.hrv_lag1_autocorr, Correlation)
    assert isinstance(rep.dev_vs_next_score, Correlation)
    # no strain rows at all -> Insufficient, never r=0
    assert isinstance(rep.dev_vs_next_strain, Insufficient)


def test_report_on_empty_db_is_all_insufficient(migrated_conn):
    rep = hrv_validation_report(migrated_conn, end="2026-03-30", window=90)
    assert rep.hrv_days == 0
    assert isinstance(rep.hrv_cv, Insufficient)
    assert isinstance(rep.hrv_lag1_autocorr, Insufficient)
    assert isinstance(rep.dev_vs_next_score, Insufficient)
    assert isinstance(rep.dev_vs_next_strain, Insufficient)
