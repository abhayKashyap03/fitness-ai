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

KJ_PER_KCAL = 4.184


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


def parse_diary(record: dict, *, user_id: int = 1) -> list[FoodEntryRow]:
    """Raw MFP diary record -> list of FoodEntryRow (one per logged item)."""
    day = record.get("date")
    if not day:
        return []
    payload = record.get("diary") or {}
    items = payload.get("items")
    if not isinstance(items, list):
        return []

    rows: list[FoodEntryRow] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        nutrition = item.get("nutritional_contents") or item.get("nutrition") or {}
        food = item.get("food") or {}
        description = item.get("description") or food.get("description") or food.get("name")
        serving = item.get("serving_size") or {}

        ext = item.get("id")
        # stable positional fallback keeps rebuild deterministic if id is absent
        external_id = str(ext) if ext is not None else f"{day}:{i}"

        rows.append(
            FoodEntryRow(
                user_id=user_id,
                day_key=str(day),  # MFP's date IS the local physiological day
                source="myfitnesspal",
                source_app=None,  # MFP is single-writer for its own API
                external_id=external_id,
                entry_type="item",
                consumed_at=None,  # MFP diary carries no per-item timestamp
                tz_name=None,
                utc_offset=None,
                description=description,
                quantity=_num(item.get("servings") if "servings" in item else serving.get("value")),
                unit=serving.get("unit"),
                kcal=_energy_kcal(nutrition.get("energy", nutrition.get("calories"))),
                protein_g=_macro(nutrition, "protein"),
                carbs_g=_macro(nutrition, "carbohydrates", "carbs"),
                fat_g=_macro(nutrition, "fat"),
                fiber_g=_macro(nutrition, "fiber", "fibre"),
                alcohol_g=_macro(nutrition, "alcohol"),
            )
        )
    return rows
