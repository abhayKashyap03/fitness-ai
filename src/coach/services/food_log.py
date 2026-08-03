"""Log a food from a product database into the canonical store (ROADMAP P12).

One seam, used by the CLI and (later) the web form and the iOS app — the same
reason `services/plan.py` exists: the last time two surfaces each carried their
own copy of a write path, they drifted and a user's own change went missing from
their history.

The write is two rows, in the order §2.1 requires:

1. the **verbatim product payload** into ``raw_events``, so the entry can be
   re-derived if the parser improves;
2. the **canonical** ``food_entry``, carrying ``raw_ref`` back to it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..normalize.foods import FoodItem, scale_to_grams
from ..store.raw import insert_raw_event


@dataclass(frozen=True)
class LoggedFood:
    entry_id: str
    raw_ref: str
    day_key: str
    description: str
    grams: float
    kcal: float | None


def log_food(
    conn: sqlite3.Connection,
    item: FoodItem,
    *,
    grams: float,
    day_key: str,
    raw_payload: dict[str, Any],
    consumed_at: str | None = None,
    tz_name: str | None = None,
    user_id: int = 1,
) -> LoggedFood:
    """Record one portion of ``item`` on ``day_key``.

    ``raw_payload`` is the product exactly as the source returned it. It is
    stored unmodified — the canonical row below is derived from it and could be
    rebuilt from it (§2.1).
    """
    portion = scale_to_grams(item, grams)
    now = datetime.now(UTC).isoformat()

    raw_ref, _ = insert_raw_event(
        conn,
        source=item.source,
        record_type="food_product",
        payload=raw_payload,
        external_id=item.external_id,
        recorded_at=now,
        user_id=user_id,
    )

    # Deterministic id, so logging the same food twice on the same day at the
    # same portion is idempotent rather than silently doubling the day's intake.
    # Two genuinely separate helpings are distinguished by consumed_at.
    entry_id = f"{item.source}:{item.external_id}:{day_key}:{grams:g}:{consumed_at or ''}"

    conn.execute(
        "INSERT OR REPLACE INTO food_entry (id, user_id, day_key, source, entry_type, "
        "consumed_at, tz_name, description, quantity, unit, kcal, protein_g, carbs_g, "
        "fat_g, fiber_g, alcohol_g, raw_ref, derived_at) "
        "VALUES (?,?,?,?,'item',?,?,?,?,'g',?,?,?,?,?,NULL,?,?)",
        (
            entry_id,
            user_id,
            day_key,
            item.source,
            consumed_at,
            tz_name,
            item.display,
            grams,
            portion.kcal,
            portion.protein_g,
            portion.carbs_g,
            portion.fat_g,
            portion.fiber_g,
            raw_ref,
            now,
        ),
    )
    return LoggedFood(
        entry_id=entry_id,
        raw_ref=raw_ref,
        day_key=day_key,
        description=item.display,
        grams=grams,
        kcal=portion.kcal,
    )
