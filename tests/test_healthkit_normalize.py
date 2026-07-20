"""Pure HealthKit weight normalizer tests (T5.4/T5.5)."""

from __future__ import annotations

import pytest

from coach.normalize.healthkit import (
    LB_TO_KG,
    app_slug,
    parse_body_record,
    parse_hk_datetime,
)


def _rec(
    rtype: str,
    value,
    source="OKOK·International Version",
    start="2026-01-02 07:00:00 -0500",
    unit="lb",
):
    return {
        "type": rtype,
        "value": value,
        "source_name": source,
        "start_date": start,
        "unit": unit,
    }


# ---- app_slug --------------------------------------------------------------


def test_app_slug_known_sources():
    assert app_slug("OKOK·International Version") == "okok"
    assert app_slug("MyFitnessPal") == "myfitnesspal"
    assert app_slug("Foodnoms") == "foodnoms"


def test_app_slug_unknown_falls_back_to_slug():
    assert app_slug("Some New Scale 2.0") == "some_new_scale_2_0"


def test_app_slug_none():
    assert app_slug(None) is None
    assert app_slug("") is None


# ---- parse_hk_datetime -----------------------------------------------------


def test_parse_hk_datetime_offset_split():
    utc, off = parse_hk_datetime("2026-01-02 07:00:00 -0500")
    assert utc == "2026-01-02T12:00:00+00:00"
    assert off == "-05:00"


def test_parse_hk_datetime_positive_offset():
    utc, off = parse_hk_datetime("2026-01-03 08:00:00 +0900")
    assert utc == "2026-01-02T23:00:00+00:00"
    assert off == "+09:00"


def test_parse_hk_datetime_none_and_garbage():
    assert parse_hk_datetime(None) == (None, None)
    assert parse_hk_datetime("not a date") == (None, None)


# ---- parse_body_record -----------------------------------------------------


def test_body_mass_lb_to_kg():
    row = parse_body_record(_rec("HKQuantityTypeIdentifierBodyMass", 183.5))
    assert row is not None
    assert row.weight_kg == pytest.approx(183.5 * LB_TO_KG, abs=1e-4)
    assert row.body_fat_pct is None
    assert row.lean_mass_kg is None
    assert row.source_app == "okok"


def test_body_fat_percent_as_is():
    row = parse_body_record(_rec("HKQuantityTypeIdentifierBodyFatPercentage", 18.5))
    assert row is not None
    assert row.body_fat_pct == pytest.approx(18.5)
    assert row.weight_kg is None


def test_lean_mass_lb_to_kg():
    row = parse_body_record(_rec("HKQuantityTypeIdentifierLeanBodyMass", 150.0))
    assert row is not None
    assert row.lean_mass_kg == pytest.approx(150.0 * LB_TO_KG, abs=1e-4)
    assert row.weight_kg is None


def test_bmi_is_skipped():
    assert parse_body_record(_rec("HKQuantityTypeIdentifierBodyMassIndex", 24.1)) is None


# ---- unit awareness (a kg record must NOT be lb-converted) -----------------


def test_kg_unit_passes_through_unconverted():
    row = parse_body_record(_rec("HKQuantityTypeIdentifierBodyMass", 83.2, unit="kg"))
    assert row is not None
    assert row.weight_kg == pytest.approx(83.2)


def test_gram_unit_scales():
    row = parse_body_record(_rec("HKQuantityTypeIdentifierBodyMass", 83200, unit="g"))
    assert row is not None
    assert row.weight_kg == pytest.approx(83.2)


def test_unknown_mass_unit_skipped_not_garbage():
    # §2.7: better absent than silently mis-converted
    assert parse_body_record(_rec("HKQuantityTypeIdentifierBodyMass", 83.2, unit="furlong")) is None
    assert parse_body_record(_rec("HKQuantityTypeIdentifierBodyMass", 83.2, unit=None)) is None


def test_body_fat_fraction_normalized_to_percent():
    # some writers export 0-1 fractions; 18.5% must come out either way
    row = parse_body_record(_rec("HKQuantityTypeIdentifierBodyFatPercentage", 0.185, unit="%"))
    assert row is not None
    assert row.body_fat_pct == pytest.approx(18.5)


def test_missing_value_yields_none_not_zero():
    # §2.7 — absence is absence, never a fabricated 0
    assert parse_body_record(_rec("HKQuantityTypeIdentifierBodyMass", None)) is None


def test_day_key_from_offset_and_tz_null():
    row = parse_body_record(_rec("HKQuantityTypeIdentifierBodyMass", 183.5))
    assert row is not None
    assert row.day_key == "2026-01-02"  # 07:00 -0500 -> local day
    assert row.tz_name is None  # body records carry no HKTimeZone (§2.6)
    assert row.utc_offset == "-05:00"
    assert row.measured_at == "2026-01-02T12:00:00+00:00"
