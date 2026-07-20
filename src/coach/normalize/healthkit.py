"""Pure HealthKit raw -> canonical parsers (CLAUDE.md §2.4).

No I/O. Given a HealthKit body-composition record (as stored in
``raw_events.payload`` by the healthkit adapter), return a partial weight row —
or ``None`` when the record is not one we canonicalize (e.g. BMI, which we
derive, or a malformed value). Purity is what makes ``--rebuild`` safe.

Scope: **body composition only** (T5.4). Dietary records are out of scope here —
HealthKit is our weight/body source; food comes from the MFP CSV adapter
(Phase 6). See docs/healthkit-export-notes.md for the observed format.

Design notes:
  * Weight/lean mass arrive in **pounds** (`lb`) and are converted to kg.
    BodyFat is a **percent** (0-100) and is stored as-is.
  * Each HealthKit ``<Record>`` carries exactly ONE metric, so one record maps
    to a *partial* row (one of weight/bf/lean set). The impure runner merges
    partials sharing an identity (source_app + instant) into one
    ``weight_measurement`` row.
  * Body records carry **no ``HKTimeZone``** (recon T5.1) -> ``tz_name`` is NULL
    (§2.6: absence is absence). ``utc_offset`` and ``day_key`` come from the
    ``startDate`` offset, which for at-home weigh-ins is the correct local zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..timeutil import day_key, normalize_offset, to_utc_iso

LB_TO_KG = 0.45359237

_BODY_MASS = "HKQuantityTypeIdentifierBodyMass"
_BODY_FAT = "HKQuantityTypeIdentifierBodyFatPercentage"
_LEAN_MASS = "HKQuantityTypeIdentifierLeanBodyMass"

# sourceName -> stable app slug (source_app). Fallback slugifies the raw name.
_APP_SLUGS = {
    "OKOK·International Version": "okok",
    "MyFitnessPal": "myfitnesspal",
    "Foodnoms": "foodnoms",
    "Cronometer": "cronometer",
    "Health": "apple_health",
}


@dataclass(frozen=True)
class WeightPartial:
    """One HealthKit body record -> at most one populated metric.

    ``instant`` is a UTC ISO-8601 string (the merge key alongside ``source_app``).
    The runner folds partials with the same ``(user_id, source_app, instant)``
    into a single ``weight_measurement`` row.
    """

    user_id: int
    source_app: str | None
    day_key: str
    measured_at: str | None
    tz_name: str | None  # always NULL for body records (no HKTimeZone)
    utc_offset: str | None
    weight_kg: float | None
    body_fat_pct: float | None
    lean_mass_kg: float | None


def app_slug(source_name: str | None) -> str | None:
    """Map an Apple Health ``sourceName`` to a stable ``source_app`` slug."""
    if not source_name:
        return None
    if source_name in _APP_SLUGS:
        return _APP_SLUGS[source_name]
    # deterministic fallback: lowercase, spaces/punct -> single underscores
    slug = "".join(c.lower() if c.isalnum() else "_" for c in source_name)
    slug = "_".join(part for part in slug.split("_") if part)
    return slug or None


def parse_hk_datetime(raw: str | None) -> tuple[str | None, str | None]:
    """Parse an Apple Health date ``YYYY-MM-DD HH:MM:SS ±HHMM``.

    Returns ``(utc_iso, offset)`` where ``offset`` is ``±HH:MM`` (or None). Apple
    separates the offset with a space and omits the colon, so
    ``datetime.fromisoformat`` cannot take it directly. Returns ``(None, None)``
    on anything unparseable — the caller decides what a missing instant means.
    """
    if not raw:
        return None, None
    s = raw.strip()
    offset: str | None = None
    # split trailing " ±HHMM"
    if len(s) >= 6 and s[-5] in "+-" and s[-6] == " ":
        body, off = s[:-6], s[-5:]
        offset = f"{off[0]}{off[1:3]}:{off[3:5]}"
        iso = f"{body}{offset}"
    else:
        iso = s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None, None
    return to_utc_iso(dt), offset


def parse_body_record(payload: dict, *, user_id: int = 1) -> WeightPartial | None:
    """HealthKit body record -> ``WeightPartial``. None if not canonicalizable.

    Handles BodyMass (lb->kg), BodyFatPercentage (% as-is), LeanBodyMass
    (lb->kg). BMI is intentionally skipped (derivable; no column). A missing
    numeric value (§2.7) yields None rather than a fabricated 0.
    """
    rtype = payload.get("type")
    value = payload.get("value")
    if value is None:
        return None

    weight_kg = body_fat_pct = lean_mass_kg = None
    if rtype == _BODY_MASS:
        weight_kg = round(float(value) * LB_TO_KG, 4)
    elif rtype == _BODY_FAT:
        body_fat_pct = round(float(value), 4)
    elif rtype == _LEAN_MASS:
        lean_mass_kg = round(float(value) * LB_TO_KG, 4)
    else:
        return None  # BMI / anything else: not stored

    measured_at, offset = parse_hk_datetime(payload.get("start_date"))
    return WeightPartial(
        user_id=user_id,
        source_app=app_slug(payload.get("source_name")),
        day_key=day_key(measured_at, offset) if measured_at else "",
        measured_at=measured_at,
        tz_name=None,  # body records have no HKTimeZone (§2.6)
        utc_offset=normalize_offset(offset),
        weight_kg=weight_kg,
        body_fat_pct=body_fat_pct,
        lean_mass_kg=lean_mass_kg,
    )
