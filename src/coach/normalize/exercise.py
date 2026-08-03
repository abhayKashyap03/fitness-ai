"""Pure parser for hand-logged exercise (ROADMAP P12, own logging).

The counterpart to `normalize.foods.parse_food_log`. A session the user typed in
is still **raw-derived**: the raw event records what they entered, and this
function reproduces the canonical ``workout`` row from it, so
``normalize --rebuild`` regenerates hand-logged sessions exactly like
strap-detected ones (§2.1).

That distinction is easy to get wrong. ``plan`` and ``coach_note`` are genuinely
user-authored tables — no ``raw_ref``, absent from the rebuild. ``workout`` is
not: the rebuild owns it, so anything landing there needs a raw event sufficient
to rebuild from. Own-logged food shipped without that and a rebuild deleted it;
this is written the right way round from the start.

Deliberately **no calorie estimate**. A duration and a sport are not enough to
compute energy expenditure for a specific body, and ADR-0007 already establishes
that wearable "calories out" never drives anything important. An invented figure
here would flow straight into the day's balance as though it had been measured.
``kcal_active`` stays NULL unless the user states one (§2.7).
"""

from __future__ import annotations

from typing import Any

from ..adapters.whoop.sport_map import CANONICAL_SPORTS
from ..timeutil import day_key, normalize_offset, parse_instant, to_utc_iso
from .whoop import WorkoutRow


def canonical_sport(value: str | None) -> str:
    """Map user input onto the canonical enum. Unknown becomes 'other'.

    Never rejects: a session the user actually did must not be lost because we
    lack a word for it. The original text is kept in ``source_sport_raw``.
    """
    v = (value or "").strip().lower()
    if v in CANONICAL_SPORTS:
        return v
    aliases = {
        "weights": "strength",
        "lifting": "strength",
        "gym": "strength",
        "running": "run",
        "jog": "run",
        "jogging": "run",
        "cycling": "cycle",
        "bike": "cycle",
        "biking": "cycle",
        "spin": "cycle",
        "walking": "walk",
        "hike": "walk",
        "hiking": "walk",
        "swimming": "swim",
        "row": "rowing",
        "erg": "rowing",
        "crossfit": "hiit",
        "circuit": "hiit",
        "interval": "hiit",
        "pilates": "yoga",
        "stretching": "yoga",
        "mobility": "yoga",
        "tennis": "sport",
        "football": "sport",
        "soccer": "sport",
        "basketball": "sport",
        "climbing": "sport",
    }
    return aliases.get(v, "other")


def parse_exercise_log(payload: dict[str, Any], *, user_id: int = 1) -> WorkoutRow | None:
    """Raw hand-logged session -> canonical ``workout`` row, or None if unusable.

    Returns None without a start instant or a positive duration: a session with
    no time span is not a session, and storing one would put a phantom entry in
    the training log the coach reads.
    """
    started = payload.get("started_at")
    duration_s = payload.get("duration_s")
    if not started or not isinstance(duration_s, (int, float)) or duration_s <= 0:
        return None

    start_iso = to_utc_iso(parse_instant(started))
    offset = normalize_offset(payload.get("utc_offset"))
    end_dt = parse_instant(started).timestamp() + float(duration_s)
    from datetime import UTC, datetime

    end_iso = to_utc_iso(datetime.fromtimestamp(end_dt, tz=UTC))

    raw_sport = payload.get("sport")
    return WorkoutRow(
        user_id=user_id,
        source="manual",
        external_id=payload.get("external_id"),
        sport_type=canonical_sport(raw_sport),
        # Kept even when it mapped cleanly: what the user called it is the audit
        # trail for a mapping that may later prove wrong.
        source_sport_raw=str(raw_sport) if raw_sport else None,
        start_at=start_iso,
        end_at=end_iso,
        tz_name=payload.get("tz_name"),
        utc_offset=offset,
        day_key=day_key(start_iso, offset),
        duration_s=int(duration_s),
        # Only what the user stated. No estimate — see the module docstring.
        kcal_active=_num(payload.get("kcal")),
        kcal_total=None,
        avg_hr_bpm=_num(payload.get("avg_hr_bpm")),
        max_hr_bpm=None,
        # WHOOP-proprietary and not computable here (ADR-0015 keeps strain a
        # documented WHOOP-only exception).
        strain=None,
        distance_m=_num(payload.get("distance_m")),
        hr_zones_json=None,
    )


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None
