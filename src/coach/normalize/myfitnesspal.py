"""Pure MyFitnessPal raw -> canonical parser (CLAUDE.md §2.4).

No I/O. Given a raw MFP diary record (exactly as stored in
``raw_events.payload``: ``{"date": "YYYY-MM-DD", "diary": <payload>}``), return a
list of canonical :class:`FoodEntryRow` — one per logged food item. Purity is
what makes ``--rebuild`` safe.

Field extraction here is the one place that RECONCILES ON FIRST LIVE CONTACT
(§10.2): the ``items`` / ``nutritional_contents`` shape is reconstructed from the
web client's write payload and must be confirmed against a real read. When it
differs, ONLY this function + its fixture change — downstream sees the stable
canonical shape (§2.5).

Absence is absence (§2.7): a macro MFP doesn't report is ``None``, never ``0``.
An empty diary yields NO rows (that day is "not logged", not a declared fast).
"""

from __future__ import annotations

from dataclasses import dataclass

from .healthkit import WeightPartial

KJ_PER_KCAL = 4.184

# MFP reports weight units as full words. Map to kg.
_MASS_TO_KG = {
    "pounds": 0.45359237,
    "pound": 0.45359237,
    "lb": 0.45359237,
    "lbs": 0.45359237,
    "kilograms": 1.0,
    "kilogram": 1.0,
    "kg": 1.0,
    "stones": 6.35029318,
    "stone": 6.35029318,
    "st": 6.35029318,
}


@dataclass(frozen=True)
class FoodEntryRow:
    user_id: int
    day_key: str
    source: str
    source_app: str | None
    external_id: str | None  # MFP diary-item id (or positional fallback)
    entry_type: str  # 'item' | 'daily_total' | 'fast'
    consumed_at: str | None  # UTC ISO-8601; MFP gives only a day -> None
    tz_name: str | None
    utc_offset: str | None
    description: str | None
    quantity: float | None
    unit: str | None
    kcal: float | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None
    fiber_g: float | None
    alcohol_g: float | None


def _num(value: object) -> float | None:
    """Coerce to float, or None (§2.7: absent/garbage stays absent, not 0)."""
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _energy_kcal(energy: object) -> float | None:
    """Extract calories from MFP's energy field.

    Accepts a bare number (assumed calories) or ``{"value": .., "unit": ..}``
    where unit may be calories or kilojoules.
    """
    if isinstance(energy, dict):
        value = _num(energy.get("value"))
        if value is None:
            return None
        unit = str(energy.get("unit", "calories")).strip().lower()
        if unit in {"kilojoules", "kilojoule", "kj"}:
            return round(value / KJ_PER_KCAL, 1)
        return round(value, 1)
    return _num(energy)


def _macro(nutrition: dict, *keys: str) -> float | None:
    """First present macro among ``keys`` (MFP naming varies), else None."""
    for k in keys:
        if k in nutrition:
            v = _num(nutrition[k])
            if v is not None:
                return v
    return None


def _weight_kg(value: float, unit: str | None) -> float | None:
    """Convert an MFP weight value to kg, or None on an unknown unit (§2.7)."""
    factor = _MASS_TO_KG.get((unit or "").strip().lower())
    if factor is None:
        return None
    return round(value * factor, 4)


def parse_measurement(record: dict, *, user_id: int = 1) -> WeightPartial | None:
    """Raw MFP weight measurement -> WeightPartial, or None if unusable.

    Record shape: ``{"date": "YYYY-MM-DD", "measurement": {"item": {...}}}``.
    Tolerates ``item`` (single) or ``items`` (list; first weight wins). MFP gives
    only a logged DAY, not a weigh-in instant, so ``measured_at``/``utc_offset``
    stay NULL (§2.7 — no false precision); the stamped ``date`` is the exact
    local ``day_key``. ``updated_at`` is edit metadata, deliberately not used as
    a measurement time.
    """
    day = record.get("date")
    if not day:
        return None
    payload = record.get("measurement") or {}
    item = payload.get("item")
    if item is None:
        items = payload.get("items")
        item = next((it for it in items if isinstance(it, dict)), None) if isinstance(items, list) else None
    if not isinstance(item, dict):
        return None
    if str(item.get("type", "weight")).strip().lower() != "weight":
        return None

    value = _num(item.get("value"))
    if value is None:
        return None
    weight_kg = _weight_kg(value, item.get("unit"))
    if weight_kg is None:
        return None

    return WeightPartial(
        user_id=user_id,
        source_app=None,  # MFP is single-writer for its own API
        day_key=str(day),
        measured_at=None,  # MFP gives a day, not an instant (§2.7)
        tz_name=None,
        utc_offset=None,
        weight_kg=weight_kg,
        body_fat_pct=None,
        lean_mass_kg=None,
    )


def parse_diary(record: dict, *, user_id: int = 1) -> list[FoodEntryRow]:
    """Raw MFP diary record -> one FoodEntryRow per logged MEAL.

    LIVE-RECONCILED against a real payload: MFP returns per-MEAL aggregates
    (``type='diary_meal'``, meal name in ``diary_meal``, summed macros in
    ``nutritional_contents``) — NOT per food item. The same ``items`` list also
    carries ``exercise_entry`` / ``steps_aggregate`` rows, which are NOT food and
    are skipped (folding them in would fabricate calories). The ``food_daily``
    view SUMs the meal rows into the day total.
    """
    day = record.get("date")
    if not day:
        return []
    payload = record.get("diary") or {}
    items = payload.get("items")
    if not isinstance(items, list):
        return []

    rows: list[FoodEntryRow] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or item.get("type") != "diary_meal":
            continue  # skip exercise_entry, steps_aggregate, anything non-food
        nutrition = item.get("nutritional_contents") or {}
        meal = item.get("diary_meal")
        # one meal per day -> stable id from (day, meal); positional fallback
        external_id = f"{day}:{meal}" if meal else f"{day}:{i}"

        rows.append(
            FoodEntryRow(
                user_id=user_id,
                day_key=str(day),  # MFP's date IS the local physiological day
                source="myfitnesspal",
                source_app=None,  # MFP is single-writer for its own API
                external_id=external_id,
                entry_type="item",  # a meal's rollup; SUMs to the day total
                consumed_at=None,  # per-meal aggregate carries no timestamp
                tz_name=None,
                utc_offset=None,
                description=meal,  # meal name is the only label MFP gives here
                quantity=None,  # per-meal aggregate has no serving size
                unit=None,
                kcal=_energy_kcal(nutrition.get("energy", nutrition.get("calories"))),
                protein_g=_macro(nutrition, "protein"),
                carbs_g=_macro(nutrition, "carbohydrates", "carbs"),
                fat_g=_macro(nutrition, "fat"),
                fiber_g=_macro(nutrition, "fiber", "fibre"),
                alcohol_g=_macro(nutrition, "alcohol"),
            )
        )
    return rows
