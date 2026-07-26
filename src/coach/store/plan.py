"""Persistence for the user-authored `plan` table (Phase 7, ADR-0013).

Unlike the canonical tables, a plan is primary user data — not derived from
raw_events (§2.1) — so it lives here rather than in ``canonical.py`` and has no
``raw_ref``. Plans are append-only history: :func:`insert_plan` deactivates any
prior active plan and inserts the new one, so :func:`active_plan` always returns
at most one row and the record of past targets stays intact.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PlanRow:
    id: str
    user_id: int
    created_at: str  # UTC ISO-8601
    start_day_key: str
    direction: str  # 'cut' | 'bulk' | 'maintain'
    target_rate_pct_per_week: float  # signed; the canonical driver (ADR-0013)
    start_weight_kg: float | None = None
    goal_weight_kg: float | None = None
    protein_g_per_kg: float | None = None
    note: str | None = None


def plan_id(user_id: int, created_at: str) -> str:
    return f"plan:{user_id}:{created_at}"


def insert_plan(conn: sqlite3.Connection, row: PlanRow) -> None:
    """Insert a new plan and make it the sole active one for the user.

    Deactivates any currently-active plan first (history is preserved — the old
    rows stay, only their ``is_active`` flips). Caller owns the transaction.
    """
    conn.execute(
        "UPDATE plan SET is_active = 0 WHERE user_id = ? AND is_active = 1",
        (row.user_id,),
    )
    conn.execute(
        "INSERT INTO plan (id, user_id, created_at, start_day_key, is_active, "
        "direction, target_rate_pct_per_week, start_weight_kg, goal_weight_kg, "
        "protein_g_per_kg, note) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (
            row.id,
            row.user_id,
            row.created_at,
            row.start_day_key,
            row.direction,
            row.target_rate_pct_per_week,
            row.start_weight_kg,
            row.goal_weight_kg,
            row.protein_g_per_kg,
            row.note,
        ),
    )


def active_plan(conn: sqlite3.Connection, *, user_id: int = 1) -> PlanRow | None:
    """The user's single active plan, or None if none is set."""
    r = conn.execute(
        "SELECT id, user_id, created_at, start_day_key, direction, "
        "target_rate_pct_per_week, start_weight_kg, goal_weight_kg, "
        "protein_g_per_kg, note "
        "FROM plan WHERE user_id = ? AND is_active = 1 "
        "ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if r is None:
        return None
    return PlanRow(
        id=r["id"],
        user_id=r["user_id"],
        created_at=r["created_at"],
        start_day_key=r["start_day_key"],
        direction=r["direction"],
        target_rate_pct_per_week=r["target_rate_pct_per_week"],
        start_weight_kg=r["start_weight_kg"],
        goal_weight_kg=r["goal_weight_kg"],
        protein_g_per_kg=r["protein_g_per_kg"],
        note=r["note"],
    )
