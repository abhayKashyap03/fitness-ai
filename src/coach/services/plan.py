"""Setting the active plan — one implementation, every surface (ADR-0013/0016).

The CLI and the web form each grew their own copy of this: resolve the entry
style, clamp the rate, anchor the start weight, insert. They drifted — the CLI
recorded a coaching note on every plan change and the web form silently did not,
so a plan set in the browser left no trace in memory and looked, later, like it
had changed by itself.

Duplicated domain logic across presentation surfaces is the bug. This is the
single seam both call, mirroring `services/sync.py` and `services/clients.py`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime

from ..compute.plan import rate_from_deadline, resolve_target_rate
from ..store.notes import SYSTEM, add_note
from ..store.plan import PlanRow, insert_plan, plan_id


class PlanInputError(ValueError):
    """The requested plan can't be resolved — message is safe to show a user."""


@dataclass(frozen=True)
class PlanSetResult:
    row: PlanRow
    clamped: bool
    note: str | None


def _trend_on_or_before(conn: sqlite3.Connection, day: str, user_id: int) -> float | None:
    row = conn.execute(
        "SELECT trend_kg FROM weight_trend WHERE user_id = ? AND day_key <= ? "
        "AND trend_kg IS NOT NULL ORDER BY day_key DESC LIMIT 1",
        (user_id, day),
    ).fetchone()
    return row["trend_kg"] if row else None


def set_active_plan(
    conn: sqlite3.Connection,
    *,
    today: str,
    rate: float | None = None,
    goal_weight: float | None = None,
    by: str | None = None,
    maintain: bool = False,
    protein: float | None = None,
    start_date: str | None = None,
    start_weight: float | None = None,
    user_id: int = 1,
) -> PlanSetResult:
    """Resolve, clamp, insert, and record the plan change. Caller commits.

    Exactly one entry style: an explicit ``rate``, a ``goal_weight`` + ``by``
    deadline, or ``maintain``. A deadline is converted to a rate and then clamped
    to the §8.6 ceiling (ADR-0013) — the clamp is not bypassable from any surface
    because every surface comes through here.
    """
    current_trend = _trend_on_or_before(conn, today, user_id)

    note_extra: str | None = None
    if maintain:
        requested = 0.0
    elif rate is not None:
        requested = rate
    elif goal_weight is not None and by:
        if current_trend is None:
            raise PlanInputError(
                "A deadline needs a current weight trend, and there is none yet. "
                "Log some weight first, or set a rate directly."
            )
        weeks = (date.fromisoformat(by) - date.fromisoformat(today)).days / 7
        if weeks <= 0:
            raise PlanInputError(f"The deadline must be a future date (got {by}).")
        requested = rate_from_deadline(
            current_weight_kg=current_trend, goal_weight_kg=goal_weight, weeks=weeks
        )
        note_extra = f"deadline entry {goal_weight}kg by {by}"
    else:
        raise PlanInputError("Specify a rate, a goal weight plus a deadline, or maintain.")

    target = resolve_target_rate(requested)
    note = target.note
    if note_extra:
        note = f"{note_extra}; {note}" if note else note_extra

    # Start anchor: a mid-cut user backdates it so progress reads truthfully.
    start_day = start_date or today
    anchor = start_weight
    if anchor is None:
        anchor = _trend_on_or_before(conn, start_date, user_id) if start_date else current_trend
        if start_date and anchor is None:
            raise PlanInputError(
                f"No weight trend on or before {start_date}; pass an explicit "
                "start weight to anchor progress."
            )

    created_at = datetime.now(UTC).isoformat()
    row = PlanRow(
        id=plan_id(user_id, created_at),
        user_id=user_id,
        created_at=created_at,
        start_day_key=start_day,
        direction=target.direction,
        target_rate_pct_per_week=target.rate_pct_per_week,
        start_weight_kg=anchor,
        goal_weight_kg=goal_weight,
        protein_g_per_kg=protein,
        note=note,
    )
    insert_plan(conn, row)

    # Record the DECISION so memory shows what changed and when, whichever
    # surface did it (ADR-0016). Code-authored; never model-authored.
    goal_txt = f" toward {goal_weight}kg" if goal_weight is not None else ""
    clamp_txt = " (clamped by the §8.6 ceiling)" if target.clamped else ""
    add_note(
        conn,
        day_key=today,
        kind="plan",
        author=SYSTEM,
        text=(
            f"Set a {target.direction} at {target.rate_pct_per_week:+.2f}%/week"
            f"{goal_txt}{clamp_txt}."
        ),
        user_id=user_id,
    )
    return PlanSetResult(row=row, clamped=target.clamped, note=note)
