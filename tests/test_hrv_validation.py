"""HRV validation harness — hand-computed fixtures, no network (risk #6).

The statistics are the product here: they must be right, and they must refuse
to answer (Insufficient) rather than mislead on thin data (§2.2/§2.7).
"""

from __future__ import annotations

import pytest

from coach.compute.hrv_validation import (
    INSUFFICIENT,
    MIN_AUTOCORR,
    MIN_DEV_CORR,
    MIN_PAIRS,
    NOISE,
    SIGNAL,
    Correlation,
    coefficient_of_variation,
    deviation_series,
    hrv_validation_report,
    hrv_verdict,
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
    a = _days(1, [1.0, 2.0, 3.0])  # 03-01..03-03
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


# ---- deterministic verdict (the answer to risk #6) -------------------------


def test_verdict_signal_needs_both_bars():
    # autocorr clears bar AND a dev-correlation clears bar -> SIGNAL
    v, why = hrv_verdict(
        Correlation(r=MIN_AUTOCORR + 0.05, n=30),
        [Correlation(r=MIN_DEV_CORR + 0.05, n=30), Correlation(r=0.0, n=30)],
    )
    assert v == SIGNAL
    assert "actionable" in why


def test_verdict_noise_when_autocorr_too_low():
    # strong next-day correlation but unstable series -> still NOISE
    v, why = hrv_verdict(
        Correlation(r=0.10, n=30),
        [Correlation(r=0.9, n=30)],
    )
    assert v == NOISE
    assert "autocorrelation" in why


def test_verdict_noise_when_no_dev_correlation_clears():
    v, _ = hrv_verdict(
        Correlation(r=0.8, n=30),
        [Correlation(r=0.05, n=30), Correlation(r=-0.10, n=30)],
    )
    assert v == NOISE


def test_verdict_uses_absolute_correlation_both_directions():
    # a strong NEGATIVE next-day correlation is still signal (direction agnostic)
    v, _ = hrv_verdict(
        Correlation(r=-(MIN_AUTOCORR + 0.1), n=30),
        [Correlation(r=-(MIN_DEV_CORR + 0.1), n=30)],
    )
    assert v == SIGNAL


def test_verdict_insufficient_when_autocorr_unmeasurable():
    v, why = hrv_verdict(Insufficient(have=5, needed=MIN_PAIRS), [])
    assert v == INSUFFICIENT
    assert "5" in why and str(MIN_PAIRS) in why


def test_verdict_insufficient_dev_correlations_are_ignored_not_zeroed():
    # dev correlation that couldn't be measured must not count as a 0 that
    # blocks signal — it's simply absent from the max()
    v, _ = hrv_verdict(
        Correlation(r=0.9, n=30),
        [Insufficient(have=2, needed=MIN_PAIRS), Correlation(r=0.5, n=30)],
    )
    assert v == SIGNAL


def test_report_carries_a_verdict(migrated_conn):
    rep = hrv_validation_report(migrated_conn, end="2026-03-30", window=90)
    # empty DB -> autocorr insufficient -> verdict insufficient, never a fake call
    assert rep.verdict == INSUFFICIENT
    assert rep.rationale


# ---- training-minutes probe (better-powered than strain) --------------------


def _seed_workout(conn, wid, day, source, duration_s, grp=None, strain=None):
    conn.execute(
        "INSERT INTO workout (id, user_id, source, external_id, sport_type, "
        "start_at, end_at, day_key, duration_s, strain, session_group_id, derived_at) "
        "VALUES (?,1,?,?,'walk',?,?,?,?,?,?,'2026-01-01T00:00:00+00:00')",
        (
            wid,
            source,
            wid,
            f"{day}T08:00:00+00:00",
            f"{day}T09:00:00+00:00",
            day,
            duration_s,
            strain,
            grp,
        ),
    )


def test_training_minutes_counts_a_shared_session_once(migrated_conn):
    """A session logged in MFP AND detected by the strap is ONE session's load."""
    for i in range(MIN_PAIRS + 2):
        day = f"2026-03-{i + 1:02d}"
        _seed_recovery(migrated_conn, day, hrv=50.0 + (i % 5), score=60.0)
        grp = f"grp:{day}"
        _seed_workout(migrated_conn, f"w{i}", day, "whoop_api", 1800, grp=grp, strain=10.0)
        _seed_workout(migrated_conn, f"m{i}", day, "myfitnesspal", 1800, grp=grp)
    migrated_conn.commit()

    rep = hrv_validation_report(migrated_conn, end="2026-03-30", window=90)
    # both rows are 30 min in ONE group -> 30 training minutes, not 60
    assert isinstance(rep.dev_vs_next_train_min, Correlation | Insufficient)
    total = migrated_conn.execute(
        "SELECT SUM(mins) AS v FROM (SELECT MAX(duration_s)/60.0 AS mins FROM workout "
        "WHERE day_key='2026-03-05' GROUP BY COALESCE(session_group_id, id))"
    ).fetchone()["v"]
    assert total == pytest.approx(30.0)


def test_training_probe_is_insufficient_without_workouts(migrated_conn):
    for i in range(20):
        _seed_recovery(migrated_conn, f"2026-03-{i + 1:02d}", hrv=50.0 + i, score=60.0)
    migrated_conn.commit()
    rep = hrv_validation_report(migrated_conn, end="2026-03-30", window=90)
    assert isinstance(rep.dev_vs_next_train_min, Insufficient)
