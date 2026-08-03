"""Log a food from a product database into the canonical store (ROADMAP P12).

One seam, used by the CLI and (later) the web form and the iOS app — the same
reason `services/plan.py` exists: the last time two surfaces each carried their
own copy of a write path, they drifted and a user's own change went missing from
their history.

The write is two rows, in the order §2.1 requires:

1. the **logging act** into ``raw_events`` — the product payload *and* the
   portion, day and time;
2. the **canonical** ``food_entry``, carrying ``raw_ref`` back to it.

**Why the raw event records the portion and not just the product.** The first
version of this stored only the product, and `normalize --rebuild` — which drops
every canonical table and re-derives from raw — deleted logged meals and could
not bring them back, because the grams existed nowhere in raw. Caught by running
a rebuild. §2.1's rule is that canonical is *fully* regenerable, and the raw
event therefore has to hold everything the canonical row was derived from. The
raw here is the act of logging, not the encyclopaedia entry it referenced.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..normalize.foods import FoodItem, scale_to_grams
from ..store.raw import insert_raw_event


def food_entry_id(
    source: str, external_id: str, day_key: str, grams: float, consumed_at: str | None
) -> str:
    """Deterministic canonical id for a logged portion.

    Deterministic so logging the same food twice on the same day at the same
    size is idempotent rather than silently doubling the day's intake, and so a
    rebuild reproduces the *same* row id rather than a duplicate. Two genuinely
    separate helpings differ by grams or ``consumed_at``.
    """
    return f"{source}:{external_id}:{day_key}:{grams:g}:{consumed_at or ''}"


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
    now = datetime.now(UTC).isoformat()

    # Everything the canonical row is derived from, so a rebuild can reproduce
    # it exactly (§2.1). `product` is the source payload, untouched.
    envelope = {
        "product": raw_payload,
        "grams": grams,
        "day_key": day_key,
        "consumed_at": consumed_at,
        "tz_name": tz_name,
        "logged_at": now,
    }
    raw_ref, _ = insert_raw_event(
        conn,
        source=item.source,
        record_type="food_log",
        payload=envelope,
        # Scoped to the portion, so the same food at two sizes is two raw events
        # while a repeated identical log dedupes.
        external_id=f"{item.external_id}:{day_key}:{grams:g}:{consumed_at or ''}",
        recorded_at=consumed_at or now,
        user_id=user_id,
    )

    portion = scale_to_grams(item, grams)
    entry_id = food_entry_id(item.source, item.external_id, day_key, grams, consumed_at)

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
