"""Product food databases: parsing, portion scaling, and logging (P12).

No network — the adapter is exercised against recorded payload shapes (§6.2).

The dominant risk in this whole area is **unknown macros silently becoming
zero**. Open Food Facts is crowd-sourced and half-filled products are normal, so
a parser that defaults missing fibre to 0.0 reports a day's intake as lower than
it was — in exactly the direction self-reporting is already biased (risk #7).
Most of what follows is about that.
"""

from __future__ import annotations

import pytest

from coach.normalize.foods import parse_openfoodfacts, scale_to_grams
from coach.services.food_log import log_food

# Shape taken from the Open Food Facts v2 response, trimmed to the fields the
# adapter requests. SYNTHETIC — not a recorded real payload.
OATS = {
    "code": "5000108351733",
    "product_name": "Porridge Oats",
    "brands": "Quaker, PepsiCo",
    "serving_size": "40 g",
    "serving_quantity": 40,
    "nutriments": {
        "energy-kcal_100g": 372,
        "proteins_100g": 11,
        "carbohydrates_100g": 60,
        "fat_100g": 8,
        "fiber_100g": 9,
    },
}


# ---- parsing ---------------------------------------------------------------


def test_a_complete_product_parses():
    item = parse_openfoodfacts(OATS)
    assert item is not None
    assert item.name == "Porridge Oats"
    assert item.brand == "Quaker"  # first of the comma-separated list
    assert item.kcal_per_100g == 372
    assert item.serving_g == 40
    assert item.display == "Porridge Oats — Quaker"


def test_missing_macros_stay_none_and_never_become_zero():
    """The failure that would quietly under-report every logged day."""
    partial = {**OATS, "nutriments": {"energy-kcal_100g": 372}}
    item = parse_openfoodfacts(partial)
    assert item is not None
    assert item.kcal_per_100g == 372
    assert item.protein_g_per_100g is None
    assert item.fiber_g_per_100g is None


def test_energy_falls_back_to_kilojoules_when_kcal_is_absent():
    """Many European products carry only kJ. Dropping them would make a large
    part of the database silently unusable."""
    kj_only = {**OATS, "nutriments": {"energy_100g": 1556}}
    item = parse_openfoodfacts(kj_only)
    assert item is not None
    assert item.kcal_per_100g == pytest.approx(371.9, abs=0.5)


def test_garbage_numeric_fields_are_unknown_not_zero():
    """Crowd-sourced data arrives as empty strings and worse."""
    messy = {
        **OATS,
        "nutriments": {"energy-kcal_100g": 372, "proteins_100g": "", "fat_100g": "n/a"},
    }
    item = parse_openfoodfacts(messy)
    assert item is not None
    assert item.protein_g_per_100g is None
    assert item.fat_g_per_100g is None


def test_negative_values_are_rejected_as_data_entry_errors():
    bad = {**OATS, "nutriments": {"energy-kcal_100g": 372, "proteins_100g": -5}}
    item = parse_openfoodfacts(bad)
    assert item is not None
    assert item.protein_g_per_100g is None


def test_a_product_with_no_nutrition_is_not_an_item():
    """Logging 'something, unknown calories' adds nothing the coach can use and
    shows up in a day's totals as a phantom entry."""
    assert parse_openfoodfacts({**OATS, "nutriments": {}}) is None


def test_a_nameless_or_codeless_product_is_rejected():
    assert parse_openfoodfacts({**OATS, "product_name": ""}) is None
    assert parse_openfoodfacts({**OATS, "code": ""}) is None


def test_a_product_with_no_brand_still_parses():
    item = parse_openfoodfacts({**OATS, "brands": ""})
    assert item is not None
    assert item.brand is None
    assert item.display == "Porridge Oats"


# ---- portion scaling -------------------------------------------------------


def test_scaling_to_a_portion():
    item = parse_openfoodfacts(OATS)
    assert item is not None
    p = scale_to_grams(item, 40)
    assert p.kcal == pytest.approx(148.8)
    assert p.protein_g == pytest.approx(4.4)


def test_scaling_preserves_unknown_as_unknown():
    """The single most important line in this module: None * anything is None,
    never 0.0."""
    item = parse_openfoodfacts({**OATS, "nutriments": {"energy-kcal_100g": 372}})
    assert item is not None
    p = scale_to_grams(item, 40)
    assert p.kcal == pytest.approx(148.8)
    assert p.protein_g is None
    assert p.fiber_g is None


def test_a_zero_or_negative_portion_is_refused():
    item = parse_openfoodfacts(OATS)
    assert item is not None
    with pytest.raises(ValueError):
        scale_to_grams(item, 0)
    with pytest.raises(ValueError):
        scale_to_grams(item, -10)


# ---- logging ---------------------------------------------------------------


def test_logging_writes_raw_and_canonical_with_a_link(migrated_conn):
    """§2.1: the canonical row must be re-derivable from the raw payload."""
    item = parse_openfoodfacts(OATS)
    assert item is not None
    logged = log_food(migrated_conn, item, grams=40, day_key="2026-08-03", raw_payload=OATS)
    migrated_conn.commit()

    raw = migrated_conn.execute(
        "SELECT source, record_type FROM raw_events WHERE id=?", (logged.raw_ref,)
    ).fetchone()
    assert raw["source"] == "openfoodfacts"
    assert raw["record_type"] == "food_product"

    row = migrated_conn.execute(
        "SELECT * FROM food_entry WHERE id=?", (logged.entry_id,)
    ).fetchone()
    assert row["raw_ref"] == logged.raw_ref
    assert row["day_key"] == "2026-08-03"
    assert row["kcal"] == pytest.approx(148.8)
    assert row["unit"] == "g"
    assert row["entry_type"] == "item"


def test_logging_the_same_portion_twice_does_not_double_the_day(migrated_conn):
    """A double-tap on a log button must not silently inflate intake."""
    item = parse_openfoodfacts(OATS)
    assert item is not None
    for _ in range(2):
        log_food(migrated_conn, item, grams=40, day_key="2026-08-03", raw_payload=OATS)
    migrated_conn.commit()
    n = migrated_conn.execute(
        "SELECT COUNT(*) AS n FROM food_entry WHERE day_key='2026-08-03'"
    ).fetchone()["n"]
    assert n == 1


def test_two_different_portions_are_two_entries(migrated_conn):
    """Idempotency must not collapse a second, genuinely different helping."""
    item = parse_openfoodfacts(OATS)
    assert item is not None
    log_food(migrated_conn, item, grams=40, day_key="2026-08-03", raw_payload=OATS)
    log_food(migrated_conn, item, grams=60, day_key="2026-08-03", raw_payload=OATS)
    migrated_conn.commit()
    n = migrated_conn.execute(
        "SELECT COUNT(*) AS n FROM food_entry WHERE day_key='2026-08-03'"
    ).fetchone()["n"]
    assert n == 2


def test_unknown_macros_are_stored_as_null_not_zero(migrated_conn):
    """End-to-end version of the rule, at the column level."""
    item = parse_openfoodfacts({**OATS, "nutriments": {"energy-kcal_100g": 372}})
    assert item is not None
    logged = log_food(migrated_conn, item, grams=100, day_key="2026-08-03", raw_payload=OATS)
    migrated_conn.commit()
    row = migrated_conn.execute(
        "SELECT protein_g, fiber_g FROM food_entry WHERE id=?", (logged.entry_id,)
    ).fetchone()
    assert row["protein_g"] is None
    assert row["fiber_g"] is None


def test_a_logged_food_reaches_the_day_total_view(migrated_conn):
    """The point of the whole slice: the coach reads food_daily, so an entry
    logged this way has to show up there beside anything from MyFitnessPal."""
    item = parse_openfoodfacts(OATS)
    assert item is not None
    log_food(migrated_conn, item, grams=100, day_key="2026-08-03", raw_payload=OATS)
    migrated_conn.commit()
    row = migrated_conn.execute(
        "SELECT kcal_total FROM food_daily WHERE day_key='2026-08-03' AND user_id=1"
    ).fetchone()
    assert row is not None
    assert row["kcal_total"] == pytest.approx(372)
