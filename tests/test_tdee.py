"""Adaptive TDEE: convergence on a synthetic known-TDEE dataset (T3.3)."""

from __future__ import annotations

from coach.compute.tdee import (
    KCAL_PER_KG,
    DayPoint,
    TDEEEstimate,
    build_window,
    estimate_tdee,
)
from coach.compute.trends import Insufficient


def _synthetic(true_tdee: float, intake: float, n_days: int) -> list[DayPoint]:
    """Weight trend that falls exactly with the intake deficit, no noise."""
    daily_balance = intake - true_tdee  # negative => weight loss
    start_kg = 85.0
    days = []
    for i in range(n_days):
        # cumulative energy balance -> trend weight
        trend = start_kg + (daily_balance * i) / KCAL_PER_KG
        day = f"2026-07-{i + 1:02d}"
        days.append(DayPoint(day_key=day, intake_kcal=intake, trend_kg=round(trend, 6)))
    return days


def test_converges_to_known_tdee_within_tolerance():
    days = _synthetic(true_tdee=2500.0, intake=2000.0, n_days=21)
    est = estimate_tdee(days)
    assert isinstance(est, TDEEEstimate)
    # exact linear trend => estimate recovers true TDEE within 1%
    assert abs(est.tdee_kcal - 2500.0) <= 25.0


def test_surplus_direction():
    # eating above maintenance -> weight rises -> TDEE < intake
    days = _synthetic(true_tdee=2200.0, intake=2800.0, n_days=21)
    est = estimate_tdee(days)
    assert isinstance(est, TDEEEstimate)
    assert est.trend_delta_kg > 0
    assert est.tdee_kcal < est.mean_intake_kcal
    assert abs(est.tdee_kcal - 2200.0) <= 25.0


def test_insufficient_intake_days():
    days = _synthetic(2500, 2000, 21)
    # blank out intake on most days -> below min_intake_days
    sparse = [
        DayPoint(d.day_key, d.intake_kcal if i < 5 else None, d.trend_kg)
        for i, d in enumerate(days)
    ]
    r = estimate_tdee(sparse, min_intake_days=10)
    assert isinstance(r, Insufficient)


def test_insufficient_weight_span():
    days = [DayPoint(f"2026-07-{i + 1:02d}", 2000.0, None) for i in range(15)]
    r = estimate_tdee(days)
    assert isinstance(r, Insufficient)


def test_build_window_from_db_then_estimate(migrated_conn):
    conn = migrated_conn
    intake = 2000.0
    true_tdee = 2500.0
    daily_balance = intake - true_tdee
    start_kg = 85.0
    for i in range(14):
        day = f"2026-07-{i + 1:02d}"
        conn.execute(
            "INSERT INTO food_entry (id,user_id,day_key,source,entry_type,kcal,derived_at) "
            "VALUES (?,1,?,'manual','item',?,'t')",
            (f"f{i}", day, intake),
        )
        # weight moves exactly with the balance so the EWMA trend tracks it
        kg = start_kg + (daily_balance * i) / KCAL_PER_KG
        conn.execute(
            "INSERT INTO weight_measurement (id,user_id,day_key,source,measured_at,"
            "weight_kg,derived_at) VALUES (?,1,?,'withings',?,?,'t')",
            (f"w{i}", day, f"{day}T06:00:00Z", round(kg, 6)),
        )
    conn.commit()

    window = build_window(conn, "2026-07-14", 14)
    assert len(window) == 14
    assert window[0].intake_kcal == intake
    est = estimate_tdee(window)
    assert isinstance(est, TDEEEstimate)
    # EWMA lag makes it approximate, not exact; direction + ballpark must hold
    assert est.tdee_kcal > est.mean_intake_kcal  # deficit => TDEE above intake
