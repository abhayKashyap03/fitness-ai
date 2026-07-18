"""Workout dedup: assign ``session_group_id`` so the SAME real workout from
multiple sources is counted once (§5, T2.6).

Rule (see docs/adr/0004-workout-dedup.md), tolerance configurable:
  two workouts join the same group when they share ``(user_id, sport_type)``,
  their start times are within ``tolerance_s`` (default 300 s), AND their
  time intervals overlap. Distinct back-to-back sessions (gap > tolerance, no
  overlap) stay separate; a run and a lift at the same time stay separate
  (different sport).

Pure and deterministic: group ids derive from the anchor (earliest) member, so
``--rebuild`` reproduces the same grouping byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..timeutil import parse_instant

DEFAULT_TOLERANCE_S = 300


@dataclass(frozen=True)
class WkSlot:
    id: str
    user_id: int
    sport_type: str
    start_at: str
    end_at: str


def _overlaps(a: WkSlot, b: WkSlot) -> bool:
    a0, a1 = parse_instant(a.start_at), parse_instant(a.end_at)
    b0, b1 = parse_instant(b.start_at), parse_instant(b.end_at)
    return max(a0, b0) < min(a1, b1)


def _within(a: WkSlot, b: WkSlot, tolerance_s: int) -> bool:
    delta = abs((parse_instant(a.start_at) - parse_instant(b.start_at)).total_seconds())
    return delta <= tolerance_s


def assign_session_groups(
    slots: list[WkSlot], tolerance_s: int = DEFAULT_TOLERANCE_S
) -> dict[str, str]:
    """Map each workout id -> its session_group_id.

    Greedy single pass over start-ordered slots: a slot joins the first existing
    group whose anchor matches (same user+sport, start within tolerance, and
    overlapping interval); otherwise it starts a new group.
    """
    ordered = sorted(slots, key=lambda s: (s.user_id, s.sport_type, s.start_at))
    groups: list[list[WkSlot]] = []
    out: dict[str, str] = {}

    for slot in ordered:
        placed = False
        for group in groups:
            anchor = group[0]
            if (
                anchor.user_id == slot.user_id
                and anchor.sport_type == slot.sport_type
                and _within(anchor, slot, tolerance_s)
                and _overlaps(anchor, slot)
            ):
                group.append(slot)
                placed = True
                break
        if not placed:
            groups.append([slot])

    for group in groups:
        anchor = group[0]
        gid = f"grp:{anchor.user_id}:{anchor.sport_type}:{anchor.start_at}"
        for slot in group:
            out[slot.id] = gid
    return out
