"""Deterministic safety guardrail tests (§8.6)."""

from __future__ import annotations

from coach.compute.guardrails import (
    MIN_CALORIE_FLOOR_KCAL,
    Alert,
    TrendPoint,
    calorie_floor_alert,
    clamp_calorie_target,
    weight_loss_rate_alert,
)
from coach.compute.trends import Insufficient


def _pts(*pairs) -> list[TrendPoint]:
    return [TrendPoint(d, kg) for d, kg in pairs]


# ---- weight-loss-rate ------------------------------------------------------


def test_insufficient_with_one_point():
    r = weight_loss_rate_alert(_pts(("2026-01-01", 100.0)))
    assert isinstance(r, Insufficient)


def test_zero_span_insufficient():
    r = weight_loss_rate_alert(_pts(("2026-01-01", 100.0), ("2026-01-01", 99.0)))
    assert isinstance(r, Insufficient)


def test_safe_slow_loss_is_none():
    # 0.4 kg over 7d on ~100 kg -> ~0.4%/wk, under the ceiling
    assert weight_loss_rate_alert(_pts(("2026-01-01", 100.0), ("2026-01-08", 99.6))) is None


def test_weight_gain_is_none():
    assert weight_loss_rate_alert(_pts(("2026-01-01", 98.0), ("2026-01-08", 99.0))) is None


def test_fast_loss_warns():
    r = weight_loss_rate_alert(_pts(("2026-01-01", 100.0), ("2026-01-08", 98.8)))
    assert isinstance(r, Alert)
    assert r.level == "warn"
    assert r.code == "fast_weight_loss"
    assert r.evidence["pct_per_week"] > 1.0


def test_rapid_loss_is_critical():
    r = weight_loss_rate_alert(_pts(("2026-01-01", 100.0), ("2026-01-08", 98.0)))
    assert isinstance(r, Alert)
    assert r.level == "critical"
    assert r.code == "rapid_weight_loss"


# ---- calorie floor ---------------------------------------------------------


def test_clamp_below_floor_returns_floor():
    assert clamp_calorie_target(900) == MIN_CALORIE_FLOOR_KCAL


def test_clamp_above_floor_unchanged():
    assert clamp_calorie_target(2200) == 2200


def test_floor_alert_below_and_above():
    assert calorie_floor_alert(900).level == "critical"  # type: ignore[union-attr]
    assert calorie_floor_alert(2200) is None
