"""Cross-source calibration stats — hand-computed fixtures (ADR-0012 prep)."""

from __future__ import annotations

import pytest

from coach.compute.calibration import (
    OBJECTIVE_METRICS,
    Agreement,
    calibration_report,
    compare_series,
    recovery_source_series,
)
from coach.compute.hrv_validation import MIN_PAIRS, Correlation
from coach.compute.trends import Insufficient


def _days(values: list[float], start: int = 1) -> dict[str, float]:
    return {f"2026-03-{start + i:02d}": v for i, v in enumerate(values)}


def test_compare_series_hand_calc():
    n = MIN_PAIRS
    a = _days([50.0 + i for i in range(n)])
    b = {d: v + 2.0 for d, v in a.items()}  # constant +2 offset, perfect tracking
    r = compare_series(a, b)
    assert isinstance(r, Agreement)
    assert r.n == n
    assert r.mean_bias == pytest.approx(2.0)
    assert r.mae == pytest.approx(2.0)
    assert isinstance(r.correlation, Correlation)
    assert r.correlation.r == pytest.approx(1.0)


def test_compare_series_only_shared_days_count():
    n = MIN_PAIRS
    a = _days([50.0 + i for i in range(n + 5)])          # days 1..n+5
    b = {d: v - 1.0 for d, v in _days([50.0 + i for i in range(n)]).items()}  # days 1..n
    r = compare_series(a, b)
    assert isinstance(r, Agreement)
    assert r.n == n  # the 5 unshared days are simply not pairs (§2.7)
    assert r.mean_bias == pytest.approx(-1.0)


def test_compare_series_insufficient_overlap():
    a = _days([1.0, 2.0, 3.0])
    b = _days([1.0, 2.0], start=2)  # overlap = 2 days
    r = compare_series(a, b)
    assert isinstance(r, Insufficient)
    assert r.have == 2 and r.needed == MIN_PAIRS


def test_recovery_series_rejects_non_objective_metric(migrated_conn):
    with pytest.raises(ValueError, match="objective"):
        recovery_source_series(migrated_conn, source="whoop_api", metric="score")


def _seed(conn, source: str, day: str, hrv: float) -> None:
    conn.execute(
        "INSERT INTO recovery (id, user_id, day_key, source, score_method, is_official, "
        "hrv_rmssd_ms, raw_ref, derived_at) VALUES (?,1,?,?,?,?,?,NULL,'2026-01-01T00:00:00+00:00')",
        (f"rec:{source}:{day}", day, source, "m", int(source == "whoop_api"), hrv),
    )


def test_calibration_report_pairs_sibling_sources(migrated_conn):
    # official + recomputed sibling rows for the same days (§2.3), hrv only
    for i in range(MIN_PAIRS):
        day = f"2026-03-{i + 1:02d}"
        _seed(migrated_conn, "whoop_api", day, 50.0 + i)
        _seed(migrated_conn, "whoop_ble", day, 50.0 + i + 3.0)  # +3 ms bias
    migrated_conn.commit()
    rep = calibration_report(migrated_conn, source_a="whoop_api", source_b="whoop_ble")
    assert set(rep) == set(OBJECTIVE_METRICS)
    hrv = rep["hrv_rmssd_ms"]
    assert isinstance(hrv, Agreement)
    assert hrv.mean_bias == pytest.approx(3.0)
    assert hrv.mae == pytest.approx(3.0)
    # metrics neither source reported degrade independently, never fake zeros
    assert isinstance(rep["spo2_pct"], Insufficient)
