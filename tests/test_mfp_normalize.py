"""Pure MFP diary normalizer tests — no I/O, no network.

Proves the raw->canonical mapping: macro extraction, energy-unit handling,
honest absence (§2.7), and rebuild-stable ids.
"""

from __future__ import annotations

import json
from pathlib import Path

from coach.normalize.myfitnesspal import parse_diary

FIX = Path(__file__).parent / "fixtures" / "myfitnesspal" / "diary_sample.json"
DAY = "2026-06-15"


def _record() -> dict:
    return {"date": DAY, "diary": json.loads(FIX.read_text())}


def test_parses_all_logged_items():
    rows = parse_diary(_record())
    assert len(rows) == 4
    assert all(r.source == "myfitnesspal" for r in rows)
    assert all(r.day_key == DAY for r in rows)
    assert all(r.entry_type == "item" for r in rows)
    assert all(r.source_app is None for r in rows)


def test_energy_object_and_bare_number_and_kilojoules():
    rows = parse_diary(_record())
    by_desc = {r.description: r for r in rows}
    # energy as {value, unit: calories}
    assert by_desc["Oatmeal, rolled"].kcal == 150.0
    # energy as a bare number (assumed calories)
    assert by_desc["Chicken breast, grilled"].kcal == 248.0
    # energy in kilojoules -> converted (837 / 4.184 ≈ 200.0)
    assert by_desc["Protein bar"].kcal == 200.0


def test_macros_extracted_and_absence_is_none_not_zero():
    rows = parse_diary(_record())
    by_desc = {r.description: r for r in rows}
    oat = by_desc["Oatmeal, rolled"]
    assert (oat.protein_g, oat.carbs_g, oat.fat_g, oat.fiber_g) == (5.0, 27.0, 3.0, 4.0)
    # salad reports no fat and no fiber -> None, never 0 (§2.7)
    salad = by_desc["Mixed salad"]
    assert salad.fat_g is None
    assert salad.fiber_g is None
    assert salad.carbs_g == 12.0
    # nobody logged alcohol -> None everywhere
    assert all(r.alcohol_g is None for r in rows)


def test_missing_item_id_gets_stable_positional_fallback():
    rows = parse_diary(_record())
    salad = next(r for r in rows if r.description == "Mixed salad")
    # 4th item (index 3), no id -> deterministic "<day>:<index>"
    assert salad.external_id == f"{DAY}:3"


def test_empty_diary_yields_no_rows_not_a_fast():
    # no items => that day is NOT LOGGED, so zero canonical rows (§2.7)
    assert parse_diary({"date": DAY, "diary": {"items": []}}) == []
    assert parse_diary({"date": DAY, "diary": {}}) == []
    assert parse_diary({"date": "", "diary": {"items": []}}) == []


def test_parse_is_pure_and_deterministic():
    a = parse_diary(_record())
    b = parse_diary(_record())
    assert [r.external_id for r in a] == [r.external_id for r in b]
    assert [r.kcal for r in a] == [r.kcal for r in b]
