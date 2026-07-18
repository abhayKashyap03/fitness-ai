"""Pure trend functions vs hand-computed fixtures (T3.2)."""

from __future__ import annotations

import pytest

from coach.compute.trends import (
    Baseline,
    Insufficient,
    baseline_deviation,
    ewma_series,
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
