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


# ---- weight calibration: the domain that is runnable TODAY -------------------
#
# `calibration_report` had no live caller and could only read the `recovery`
# table, so the module docstring's claim that it was "usable today for any
# sibling pair (e.g. healthkit-vs-myfitnesspal weight)" was not actually
# reachable — there was no weight assembler. These cover the one added, and the
# `coach eval calibration` command that finally surfaces any of it.

from coach.compute.calibration import (  # noqa: E402
    SourceSpec,
    weight_calibration_report,
    weight_source_series,
)


def _w(conn, day, kg, *, source="healthkit", app="okok", bf=None, suffix=""):
    conn.execute(
        "INSERT INTO weight_measurement (id, user_id, day_key, source, source_app, "
        "weight_kg, body_fat_pct, raw_ref, derived_at) VALUES (?,1,?,?,?,?,?,NULL,?)",
        (f"wt:{source}:{app}:{day}{suffix}", day, source, app, kg, bf, f"{day}T00:00:00+00:00"),
    )


def test_source_spec_parses_source_and_app():
    assert SourceSpec.parse("healthkit:okok") == SourceSpec("healthkit", "okok")
    assert SourceSpec.parse("myfitnesspal") == SourceSpec("myfitnesspal", None)
    assert SourceSpec.parse(" healthkit : okok ") == SourceSpec("healthkit", "okok")


def test_source_spec_rejects_an_empty_source():
    with pytest.raises(ValueError, match="empty"):
        SourceSpec.parse(":okok")


def test_source_spec_str_round_trips():
    for spec in ("healthkit:okok", "myfitnesspal"):
        assert str(SourceSpec.parse(spec)) == spec


def test_weight_series_is_narrowed_by_source_app(migrated_conn):
    """ADR-0008: several apps write BodyMass under one source.

    Without the sub-source filter, comparing 'healthkit' against MyFitnessPal
    would pool the OKOK scale with MFP's own mirrored rows — calibrating a
    source partly against itself and manufacturing agreement.
    """
    _w(migrated_conn, "2026-01-01", 80.0, app="okok")
    _w(migrated_conn, "2026-01-01", 99.0, app="myfitnesspal", suffix=":b")
    migrated_conn.commit()
    okok = weight_source_series(
        migrated_conn, spec=SourceSpec("healthkit", "okok"), metric="weight_kg"
    )
    mirrored = weight_source_series(
        migrated_conn, spec=SourceSpec("healthkit", "myfitnesspal"), metric="weight_kg"
    )
    assert okok == {"2026-01-01": 80.0}
    assert mirrored == {"2026-01-01": 99.0}
    # unnarrowed pools both, which is exactly the mistake worth preventing
    pooled = weight_source_series(migrated_conn, spec=SourceSpec("healthkit"), metric="weight_kg")
    assert pooled == {"2026-01-01": pytest.approx(89.5)}


def test_weight_series_averages_multiple_readings_in_a_day(migrated_conn):
    """A morning and an evening weigh-in must not depend on insertion order."""
    _w(migrated_conn, "2026-01-01", 80.0)
    _w(migrated_conn, "2026-01-01", 81.0, suffix=":pm")
    migrated_conn.commit()
    got = weight_source_series(
        migrated_conn, spec=SourceSpec("healthkit", "okok"), metric="weight_kg"
    )
    assert got == {"2026-01-01": pytest.approx(80.5)}


def test_weight_series_omits_days_with_no_reading(migrated_conn):
    """A missing day is not a zero — it forms no pair (§2.7)."""
    _w(migrated_conn, "2026-01-01", 80.0)
    _w(migrated_conn, "2026-01-03", 80.2)
    migrated_conn.commit()
    got = weight_source_series(
        migrated_conn, spec=SourceSpec("healthkit", "okok"), metric="weight_kg"
    )
    assert sorted(got) == ["2026-01-01", "2026-01-03"]
    assert "2026-01-02" not in got


def test_weight_series_rejects_a_non_weight_metric(migrated_conn):
    with pytest.raises(ValueError, match="not a weight metric"):
        weight_source_series(migrated_conn, spec=SourceSpec("healthkit"), metric="hrv_rmssd_ms")


def test_weight_calibration_reports_bias_and_mae(migrated_conn):
    """B reads consistently 0.5 kg heavier than A across 20 shared days."""
    from datetime import date, timedelta

    start = date(2026, 1, 1)
    for i in range(20):
        day = (start + timedelta(days=i)).isoformat()
        _w(migrated_conn, day, 80.0 + i * 0.1, app="okok")
        _w(migrated_conn, day, 80.5 + i * 0.1, source="myfitnesspal", app=None, suffix=":m")
    migrated_conn.commit()
    report = weight_calibration_report(
        migrated_conn,
        spec_a=SourceSpec("healthkit", "okok"),
        spec_b=SourceSpec("myfitnesspal"),
    )
    got = report["weight_kg"]
    assert not isinstance(got, Insufficient)
    assert got.n == 20
    assert got.mean_bias == pytest.approx(0.5)  # B - A, systematic
    assert got.mae == pytest.approx(0.5)
    assert not isinstance(got.correlation, Insufficient)
    assert got.correlation.r == pytest.approx(1.0)  # perfectly parallel


def test_weight_calibration_is_insufficient_without_overlap(migrated_conn):
    """Two sources that never share a day cannot be compared honestly."""
    _w(migrated_conn, "2026-01-01", 80.0, app="okok")
    _w(migrated_conn, "2026-02-01", 80.0, source="myfitnesspal", app=None, suffix=":m")
    migrated_conn.commit()
    report = weight_calibration_report(
        migrated_conn,
        spec_a=SourceSpec("healthkit", "okok"),
        spec_b=SourceSpec("myfitnesspal"),
    )
    got = report["weight_kg"]
    assert isinstance(got, Insufficient)
    assert got.have == 0


def test_each_metric_degrades_independently(migrated_conn):
    """Weight can be comparable while body-fat isn't — one must not mask the other."""
    from datetime import date, timedelta

    start = date(2026, 1, 1)
    for i in range(20):
        day = (start + timedelta(days=i)).isoformat()
        _w(migrated_conn, day, 80.0, app="okok", bf=20.0)
        # B reports weight only — no body-fat readings at all
        _w(migrated_conn, day, 80.0, source="myfitnesspal", app=None, suffix=":m")
    migrated_conn.commit()
    report = weight_calibration_report(
        migrated_conn,
        spec_a=SourceSpec("healthkit", "okok"),
        spec_b=SourceSpec("myfitnesspal"),
    )
    assert not isinstance(report["weight_kg"], Insufficient)
    assert isinstance(report["body_fat_pct"], Insufficient)
