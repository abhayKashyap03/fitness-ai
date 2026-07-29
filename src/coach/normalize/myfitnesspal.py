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
from .whoop import WorkoutRow

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


def _instant(value: object) -> str | None:
    """MFP timestamp -> UTC ISO-8601 instant, or None when absent/unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    from ..timeutil import parse_instant, to_utc_iso

    try:
        return to_utc_iso(parse_instant(value.strip()))
    except ValueError, TypeError:
        return None


def _shift_instant(instant: str, seconds: int) -> str:
    """``instant`` advanced by ``seconds`` (used to derive a session's end)."""
    from datetime import timedelta

    from ..timeutil import parse_instant, to_utc_iso

    return to_utc_iso(parse_instant(instant) + timedelta(seconds=seconds))


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
        item = (
            next((it for it in items if isinstance(it, dict)), None)
            if isinstance(items, list)
            else None
        )
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


# ---- exercise -> workout ----------------------------------------------------

# MFP gives a free-text exercise description plus a coarse `exercise.type`
# ("cardio" / "strength"). Map into the SAME canonical vocabulary the WHOOP
# adapter uses, so one real session logged in both sources can be grouped by
# session_group_id instead of counted twice (§5, T2.6).
_DESCRIPTION_SPORTS: tuple[tuple[str, str], ...] = (
    ("walk", "walk"),
    ("hik", "walk"),
    ("ruck", "walk"),
    ("run", "run"),
    ("jog", "run"),
    ("treadmill", "run"),
    ("cycl", "cycle"),
    ("bike", "cycle"),
    ("biking", "cycle"),
    ("spin", "cycle"),
    ("swim", "swim"),
    ("row", "rowing"),
    ("yoga", "yoga"),
    ("pilates", "yoga"),
    ("hiit", "hiit"),
    ("interval", "hiit"),
    ("circuit", "hiit"),
    ("strength", "strength"),
    ("weight", "strength"),
    ("lifting", "strength"),
    ("resistance", "strength"),
    ("calisthenic", "strength"),
)

_MILES_TO_M = 1609.344
_KM_TO_M = 1000.0
_DISTANCE_TO_M = {
    "miles": _MILES_TO_M,
    "mile": _MILES_TO_M,
    "mi": _MILES_TO_M,
    "kilometers": _KM_TO_M,
    "kilometres": _KM_TO_M,
    "kilometer": _KM_TO_M,
    "km": _KM_TO_M,
    "meters": 1.0,
    "metres": 1.0,
    "m": 1.0,
}


def _sport_from_exercise(description: str | None, coarse_type: str | None) -> str:
    """Canonical sport_type from MFP's description, falling back to its type.

    Description first because it's specific ("Walking, 3.0 mph") where
    `exercise.type` is only cardio/strength. Unknown -> 'other', never a guess.
    """
    text = (description or "").strip().lower()
    for needle, sport in _DESCRIPTION_SPORTS:
        if needle in text:
            return sport
    coarse = (coarse_type or "").strip().lower()
    if coarse == "strength":
        return "strength"
    if coarse == "cardio":
        return "other"  # cardio of unknown kind — honest, not a fabricated 'run'
    return "other"


def _distance_m(distance: object) -> float | None:
    """MFP distance ({'value','unit'}) in metres, or None (§2.7)."""
    if not isinstance(distance, dict):
        return None
    value = _num(distance.get("value"))
    if value is None:
        return None
    factor = _DISTANCE_TO_M.get(str(distance.get("unit", "")).strip().lower())
    if factor is None:
        return None
    return round(value * factor, 2)


def parse_exercise(record: dict, *, user_id: int = 1) -> list[WorkoutRow]:
    """Derive canonical workouts from a raw MFP diary record.

    MFP diaries carry `exercise_entry` items beside the food rows; these are real
    logged sessions and belong in `workout`, not in food (folding their energy
    into intake would corrupt the calorie balance — which is why `parse_diary`
    filters them out).

    Two entries are deliberately NOT workouts:

    * ``is_calorie_adjustment`` rows — synthetic pseudo-entries MFP creates when
      a device (Apple Health, Fitbit) reports extra burn. They are an adjustment,
      not a session; treating them as workouts would invent training that never
      happened.
    * entries with no usable duration — a session with no length can't inform
      anything downstream, and a fabricated one would be worse than absent.
    """
    day = record.get("date")
    diary = record.get("diary")
    items = (diary or {}).get("items") if isinstance(diary, dict) else None
    if not day or not isinstance(items, list):
        return []

    rows: list[WorkoutRow] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or item.get("type") != "exercise_entry":
            continue
        if item.get("is_calorie_adjustment"):
            continue  # a device burn adjustment, not a session

        duration_s = _num(item.get("duration"))
        if duration_s is None or duration_s <= 0:
            continue

        raw_exercise = item.get("exercise")
        exercise: dict = raw_exercise if isinstance(raw_exercise, dict) else {}
        description = exercise.get("description")
        sport = _sport_from_exercise(description, exercise.get("type"))

        start_at = _instant(item.get("start_time"))
        if start_at is None:
            # No timestamp: anchor to the logged day's start so ordering stays
            # sane. day_key (what compute uses) is exact either way.
            start_at = f"{day}T00:00:00+00:00"
        end_at = _shift_instant(start_at, int(duration_s))

        rows.append(
            WorkoutRow(
                user_id=user_id,
                source="myfitnesspal",
                external_id=str(item.get("id") or f"{day}:{i}"),
                sport_type=sport,
                source_sport_raw=str(description) if description else None,
                start_at=start_at,
                end_at=end_at,
                tz_name=None,  # MFP gives no zone; strictly IANA or NULL (§2.6)
                utc_offset=None,
                day_key=str(day),  # MFP's date IS the local physiological day
                duration_s=int(duration_s),
                kcal_active=_energy_kcal(item.get("energy")),
                kcal_total=None,  # MFP reports the session burn only
                avg_hr_bpm=_num(item.get("avg_heart_rate")),
                max_hr_bpm=_num(item.get("max_heart_rate")),
                strain=None,  # WHOOP-proprietary; not comparable (§5)
                distance_m=_distance_m(item.get("distance")),
                hr_zones_json=None,
            )
        )
    return rows
