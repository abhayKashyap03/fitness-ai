"""WHOOP sport_id -> our canonical ``sport_type`` enum.

Vendor mapping lives in the adapter (§2.5). The original id/name is preserved in
``source_sport_raw`` so an unmapped or re-mapped sport is never lost.

Canonical enum: strength | run | cycle | walk | hiit | swim | rowing |
                yoga | sport | other

WHOOP's numeric sport ids are stable but long; only the common ones are mapped
explicitly. Anything unmapped falls through to ``other`` (never dropped).
"""

from __future__ import annotations

CANONICAL_SPORTS = frozenset(
    {"strength", "run", "cycle", "walk", "hiit", "swim", "rowing", "yoga", "sport", "other"}
)

# WHOOP sport_id -> canonical. Subset of WHOOP's catalog; extend as needed.
_WHOOP_SPORT_ID: dict[int, str] = {
    -1: "other",  # "Activity" / unspecified
    0: "run",
    1: "cycle",
    16: "swim",
    18: "rowing",
    43: "sport",  # generic
    44: "strength",  # Functional Fitness
    45: "strength",  # Weightlifting / Strength Trainer variants
    48: "hiit",
    52: "walk",  # Hiking / Rucking family
    63: "walk",
    66: "yoga",
    71: "run",  # Treadmill
    96: "hiit",
}


def whoop_sport_to_canonical(sport_id: int | None) -> str:
    """Map a WHOOP sport id to a canonical sport_type; unknown -> 'other'."""
    if sport_id is None:
        return "other"
    return _WHOOP_SPORT_ID.get(int(sport_id), "other")
