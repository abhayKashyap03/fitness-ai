"""WHOOP sport -> our canonical ``sport_type`` enum.

Vendor mapping lives in the adapter (§2.5). The original id/name is preserved in
``source_sport_raw`` so an unmapped or re-mapped sport is never lost.

Canonical enum: strength | run | cycle | walk | hiit | swim | rowing |
                yoga | sport | other

**Prefer ``sport_name``** — real WHOOP v2 payloads always include a
self-describing name (e.g. "swimming", "walking", "activity"), which is far more
reliable than the numeric id. First contact with real data corrected earlier
id guesses (sport_id 33 is *swimming*, not the guessed 16). The id map is only a
fallback for records that somehow lack a name. Anything unknown -> ``other``.
"""

from __future__ import annotations

CANONICAL_SPORTS = frozenset(
    {"strength", "run", "cycle", "walk", "hiit", "swim", "rowing", "yoga", "sport", "other"}
)

# Primary: WHOOP sport_name (lower-cased) -> canonical.
_WHOOP_SPORT_NAME: dict[str, str] = {
    "running": "run",
    "cycling": "cycle",
    "walking": "walk",  # confirmed from real data
    "hiking": "walk",
    "rucking": "walk",
    "swimming": "swim",  # confirmed from real data (sport_id 33)
    "rowing": "rowing",
    "functional fitness": "strength",
    "weightlifting": "strength",
    "strength trainer": "strength",
    "powerlifting": "strength",
    "hiit": "hiit",
    "yoga": "yoga",
    "pilates": "yoga",
    "activity": "other",  # confirmed from real data (sport_id -1)
    "other": "other",
}

# Fallback: WHOOP sport_id -> canonical. Only ids confirmed from real payloads or
# WHOOP docs; used when sport_name is absent. Unknown ids -> 'other'.
_WHOOP_SPORT_ID: dict[int, str] = {
    -1: "other",  # activity / unspecified (confirmed)
    33: "swim",  # swimming (confirmed)
    63: "walk",  # walking (confirmed)
}


def whoop_sport_to_canonical(sport_id: int | None = None, sport_name: str | None = None) -> str:
    """Map a WHOOP sport to a canonical sport_type.

    Prefers ``sport_name`` (reliable, always present in real v2 data); falls back
    to the numeric id. Unknown -> 'other' (never dropped; raw name kept in
    ``source_sport_raw``).
    """
    if sport_name:
        return _WHOOP_SPORT_NAME.get(sport_name.strip().lower(), "other")
    if sport_id is not None:
        return _WHOOP_SPORT_ID.get(int(sport_id), "other")
    return "other"
