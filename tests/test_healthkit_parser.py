"""Streaming HealthKit parser (T5.2), against a synthetic export fixture."""

from __future__ import annotations

import zipfile
from pathlib import Path

from coach.adapters.healthkit.parser import HKRecord, iter_records

FIX = Path(__file__).parent / "fixtures" / "healthkit" / "export_sample.xml"


def _records() -> list[HKRecord]:
    return list(iter_records(FIX))


def test_yields_only_nutrition_and_body_skips_others():
    recs = _records()
    types = {r.type for r in recs}
    # HeartRate must be filtered out
    assert not any("HeartRate" in t for t in types)
    # 7 in-scope records: energy(MFP), protein(MFP), energy(Foodnoms),
    # fatTotal(MFP malformed), BodyMass(OKOK), BodyFat(OKOK), BodyMass(MFP)
    assert len(recs) == 7


def test_source_name_preserved():
    sources = {r.source_name for r in _records()}
    assert "MyFitnessPal" in sources
    assert "Foodnoms" in sources
    assert any(s.startswith("OKOK") for s in sources)


def test_dietary_metadata_and_value():
    energy = next(
        r
        for r in _records()
        if r.type.endswith("DietaryEnergyConsumed") and r.source_name == "MyFitnessPal"
    )
    assert energy.value == 600.0
    assert energy.unit == "Cal"
    assert energy.metadata["meal"] == "Lunch"
    assert energy.metadata["HKTimeZone"] == "America/New_York"
    assert energy.metadata["HKFoodType"] == "Sandwich"


def test_iana_timezone_on_travel_record():
    foodnoms = next(r for r in _records() if r.source_name == "Foodnoms")
    assert foodnoms.metadata["HKTimeZone"] == "Asia/Tokyo"


def test_body_mass_in_pounds_multi_source():
    body = [r for r in _records() if r.type.endswith("BodyMass")]
    assert len(body) == 2  # OKOK + MyFitnessPal (siblings)
    assert all(r.unit == "lb" for r in body)
    assert {r.source_name for r in body} == {"OKOK·International Version", "MyFitnessPal"}


def test_malformed_value_yields_none_not_crash():
    fat = next(r for r in _records() if r.type.endswith("DietaryFatTotal"))
    assert fat.value is None  # "not-a-number" -> None, record still present


def test_reads_from_zip(tmp_path):
    zpath = tmp_path / "export.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(FIX, arcname="apple_health_export/export.xml")
    recs = list(iter_records(zpath))
    assert len(recs) == 7
