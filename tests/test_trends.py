"""Pure trend functions vs hand-computed fixtures (T3.2)."""

from __future__ import annotations

import pytest

from coach.compute.trends import (
    Baseline,
    Insufficient,
    baseline_deviation,
    ewma_series,
    gap_aware_ewma,
    latest_ewma,
    rolling_mean,
)


def test_ewma_series_hand_calc():
    # alpha=0.5: 10; 0.5*20+0.5*10=15; 0.5*30+0.5*15=22.5
    assert ewma_series([10, 20, 30], alpha=0.5) == [10, 15.0, 22.5]


def test_ewma_empty_and_single():
    assert ewma_series([]) == []
    assert ewma_series([42.0]) == [42.0]


def test_ewma_bad_alpha_raises():
    with pytest.raises(ValueError):
        ewma_series([1, 2], alpha=0)
    with pytest.raises(ValueError):
        ewma_series([1, 2], alpha=1.5)


def test_latest_ewma_insufficient_when_empty():
    r = latest_ewma([])
    assert isinstance(r, Insufficient)
    assert not r  # falsy


def test_rolling_mean_hand_calc():
    assert rolling_mean([1, 2, 3, 4], window=3) == 3.0  # mean(2,3,4)


def test_rolling_mean_insufficient():
    r = rolling_mean([1, 2], window=3)
    assert isinstance(r, Insufficient)
    assert r.have == 2 and r.needed == 3


def test_baseline_deviation_hand_calc():
    # window=3, values prior=[50,52,48] baseline=50, latest=60 => dev +10 (+20%)
    r = baseline_deviation([50, 52, 48, 60], window=3)
    assert isinstance(r, Baseline)
    assert r.baseline == 50.0
    assert r.deviation == 10.0
    assert r.deviation_pct == pytest.approx(20.0)


def test_baseline_needs_window_plus_one():
    r = baseline_deviation([50, 52, 48], window=3)  # only 3, need 4
    assert isinstance(r, Insufficient)
    assert r.needed == 4


# ---- gap-aware EWMA (ADR-0011) ---------------------------------------------


def test_gap_aware_matches_plain_ewma_on_consecutive_days():
    # evenly-sampled data must be IDENTICAL to the row-based EWMA (g=1 -> alpha)
    days = [f"2026-03-{d:02d}" for d in range(1, 6)]
    values = [80.0, 81.0, 79.5, 80.5, 80.0]
    plain = ewma_series(values, alpha=0.1)
    gap = gap_aware_ewma(list(zip(days, values, strict=True)), alpha=0.1)
    assert [v for _, v in gap] == pytest.approx(plain)


def test_gap_aware_long_gap_trusts_new_reading_more():
    # 14-day gap: eff = 1 - 0.9^14 ~= 0.7712 — the new reading pulls hard,
    # exactly as if the daily EWMA had run 14 times toward it
    pts = [("2026-03-01", 80.0), ("2026-03-15", 78.0)]
    (_, t0), (_, t1) = gap_aware_ewma(pts, alpha=0.1)
    eff = 1 - 0.9**14
    assert t0 == 80.0
    assert t1 == pytest.approx(eff * 78.0 + (1 - eff) * 80.0)
    # sanity: far closer to the new reading than a row-based step would land
    row_based = 0.1 * 78.0 + 0.9 * 80.0  # 79.8
    assert t1 < 78.6 < row_based


def test_gap_aware_rejects_disorder_and_duplicates():
    with pytest.raises(ValueError, match="ascending"):
        gap_aware_ewma([("2026-03-02", 80.0), ("2026-03-01", 81.0)])
    with pytest.raises(ValueError, match="ascending"):
        gap_aware_ewma([("2026-03-01", 80.0), ("2026-03-01", 81.0)])
    assert gap_aware_ewma([]) == []


def test_sql_view_matches_python_ground_truth(migrated_conn):
    """Cross-validation: the weight_trend VIEW must reproduce gap_aware_ewma
    exactly (rounded to the view's 4 decimals) on gapped, irregular data."""
    pts = [
        ("2026-03-01", 84.2),
        ("2026-03-02", 84.6),   # consecutive
        ("2026-03-05", 83.9),   # 3-day gap
        ("2026-03-19", 82.1),   # 14-day gap
        ("2026-03-20", 82.4),
    ]
    for day, kg in pts:
        migrated_conn.execute(
            "INSERT INTO weight_measurement (id, user_id, day_key, source, weight_kg, derived_at) "
            "VALUES (?, 1, ?, 'manual', ?, '2026-01-01T00:00:00+00:00')",
            (f"wt:t:{day}", day, kg),
        )
    migrated_conn.commit()
    view = migrated_conn.execute(
        "SELECT day_key, trend_kg FROM weight_trend ORDER BY day_key"
    ).fetchall()
    truth = gap_aware_ewma(pts, alpha=0.1)
    assert [(r["day_key"], r["trend_kg"]) for r in view] == [
        (d, pytest.approx(round(v, 4))) for d, v in truth
    ]
