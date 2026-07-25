"""Pure MFP normalizer tests — no I/O, no network.

Diary: proves the live-reconciled per-meal shape (`diary_meal` rows), that
non-food entries (exercise/steps) are skipped, macro extraction, honest absence
(§2.7), rebuild-stable ids. Weight: unit conversion + honest absence.
"""

from __future__ import annotations

import json
from pathlib import Path

from coach.normalize.myfitnesspal import _energy_kcal, parse_diary, parse_measurement

FIX = Path(__file__).parent / "fixtures" / "myfitnesspal" / "diary_sample.json"
WEIGHT_FIX = Path(__file__).parent / "fixtures" / "myfitnesspal" / "weight_sample.json"
DAY = "2026-06-15"


def _record() -> dict:
    return {"date": DAY, "diary": json.loads(FIX.read_text())}


def _weight_record() -> dict:
    return {"date": DAY, "measurement": json.loads(WEIGHT_FIX.read_text())}


# ---- diary (per-meal aggregates) -------------------------------------------


def test_parses_only_meal_rows_skipping_non_food():
    rows = parse_diary(_record())
    # fixture has 2 diary_meal rows + exercise_entry + steps_aggregate
    assert len(rows) == 2
    assert {r.description for r in rows} == {"Breakfast", "Dinner"}
    assert all(r.source == "myfitnesspal" for r in rows)
    assert all(r.day_key == DAY and r.entry_type == "item" for r in rows)
    # the exercise (1 kcal) / steps rows must NOT become food
    assert all(r.kcal not in (1, 1.0) for r in rows)


def test_energy_and_macros_with_honest_absence():
    by_meal = {r.description: r for r in parse_diary(_record())}
    b = by_meal["Breakfast"]
    assert b.kcal == 200.0
    assert (b.protein_g, b.carbs_g, b.fat_g, b.fiber_g) == (10.0, 30.0, 5.0, 4.0)
    d = by_meal["Dinner"]
    assert d.kcal == 600.0
    assert d.fiber_g is None  # Dinner reports no fiber -> None, not 0 (§2.7)
    assert all(r.alcohol_g is None for r in by_meal.values())  # ethanol not reported
    # per-meal aggregates carry no serving
    assert all(r.quantity is None and r.unit is None for r in by_meal.values())


def test_meal_external_id_is_stable_from_day_and_meal():
    b = next(r for r in parse_diary(_record()) if r.description == "Breakfast")
    assert b.external_id == f"{DAY}:Breakfast"


def test_empty_or_dayless_diary_yields_no_rows():
    assert parse_diary({"date": DAY, "diary": {"items": []}}) == []
    assert parse_diary({"date": DAY, "diary": {}}) == []
    assert parse_diary({"date": "", "diary": {"items": []}}) == []


def test_energy_kcal_handles_bare_number_and_kilojoules():
    # real MFP uses {unit,value}; keep the helper robust to other shapes anyway
    assert _energy_kcal(248.0) == 248.0
    assert _energy_kcal({"unit": "calories", "value": 150.0}) == 150.0
    assert _energy_kcal({"unit": "kilojoules", "value": 837.0}) == 200.0  # 837/4.184
    assert _energy_kcal(None) is None


def test_parse_is_pure_and_deterministic():
    a, b = parse_diary(_record()), parse_diary(_record())
    assert [r.external_id for r in a] == [r.external_id for r in b]
    assert [r.kcal for r in a] == [r.kcal for r in b]


# ---- weight measurement ----------------------------------------------------


def test_parse_measurement_converts_pounds_and_keeps_day_key():
    row = parse_measurement(_weight_record())
    assert row is not None
    assert row.day_key == DAY  # MFP's date is the exact local day
    assert row.weight_kg == round(183.5 * 0.45359237, 4)  # pounds -> kg
    # MFP gives only a day, not a weigh-in instant (§2.7)
    assert row.measured_at is None
    assert row.utc_offset is None
    assert row.source_app is None


def test_parse_measurement_unknown_unit_is_none():
    rec = {"date": DAY, "measurement": {"item": {"type": "weight", "value": 80, "unit": "furlongs"}}}
    assert parse_measurement(rec) is None


def test_parse_measurement_empty_or_non_weight_is_none():
    assert parse_measurement({"date": DAY, "measurement": {}}) is None
    assert parse_measurement({"date": "", "measurement": {"item": {"value": 80, "unit": "kg"}}}) is None
    non_weight = {"date": DAY, "measurement": {"item": {"type": "waist", "value": 32, "unit": "inches"}}}
    assert parse_measurement(non_weight) is None


def test_parse_measurement_accepts_kilograms():
    rec = {"date": DAY, "measurement": {"item": {"type": "weight", "value": 83.2, "unit": "kilograms"}}}
    row = parse_measurement(rec)
    assert row is not None and row.weight_kg == 83.2
