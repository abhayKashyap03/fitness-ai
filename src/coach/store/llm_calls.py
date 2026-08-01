"""LLM token + cost accounting (§8.7, migration 0013).

Append-only, like `plan` (ADR-0013) and `coach_note` (ADR-0016). Code-authored
primary data: no `raw_ref`, and no part in the normalize rebuild or fingerprint.

The unit rates in effect at call time are stored **on the row**. Prices change,
and recomputing an old call's cost against a current price table would silently
rewrite history — so a July call keeps its July rates forever (§2.3).

A NULL rate means **unpriced, not free** (§2.7). Nothing here invents a price:
tokens are the measurement, currency is an optional overlay the human supplies.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from ..timeutil import day_key as day_key_for


@dataclass(frozen=True)
class LlmCallRow:
    """One recorded model spend (an agent loop's worth, not one HTTP request)."""

    id: str
    user_id: int
    created_at: str  # UTC ISO-8601
    day_key: str  # local day it belongs to
    provider: str
    model: str
    command: str  # 'ask' | 'eval_grounding' | 'web_ask' | ...
    rounds: int
    ok: bool
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    # USD per 1M tokens as of the call; None = unpriced
    price_input_per_mtok: float | None = None
    price_output_per_mtok: float | None = None
    price_cached_input_per_mtok: float | None = None
    price_cache_write_per_mtok: float | None = None


def llm_call_id(user_id: int, created_at: str, seq: int) -> str:
    return f"llm:{user_id}:{created_at}:{seq}"


def _next_seq(conn: sqlite3.Connection, user_id: int, created_at: str) -> int:
    """Disambiguate calls landing in the same second (an eval run does dozens)."""
    prefix = f"llm:{user_id}:{created_at}:"
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM llm_call WHERE id LIKE ? ESCAPE '\\'",
        (prefix.replace("_", r"\_") + "%",),
    ).fetchone()
    return int(row["n"] if isinstance(row, sqlite3.Row) else row[0])


def record_call(
    conn: sqlite3.Connection,
    *,
    provider: str,
    model: str,
    command: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    rounds: int = 0,
    ok: bool = True,
    prices: dict[str, float | None] | None = None,
    created_at: str | None = None,
    utc_offset: str | None = None,
    day_key: str | None = None,
    user_id: int = 1,
) -> LlmCallRow:
    """Append one spend record. Caller owns the transaction.

    ``prices`` carries the per-1M-token rates in effect; omit or pass None
    values to record the call as unpriced.

    ``day_key`` is the local day the spend belongs to (§2.6). Pass it directly
    when the caller knows the home timezone — that is exact. Otherwise it is
    derived from ``utc_offset``, falling back to UTC when even that is unknown.
    Never derived from the host clock's zone.
    """
    created_at = created_at or datetime.now(UTC).isoformat()
    p = prices or {}
    row = LlmCallRow(
        id=llm_call_id(user_id, created_at, _next_seq(conn, user_id, created_at)),
        user_id=user_id,
        created_at=created_at,
        day_key=day_key or day_key_for(created_at, utc_offset),
        provider=provider,
        model=model,
        command=command,
        rounds=rounds,
        ok=ok,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        price_input_per_mtok=p.get("input"),
        price_output_per_mtok=p.get("output"),
        price_cached_input_per_mtok=p.get("cached_input"),
        price_cache_write_per_mtok=p.get("cache_write"),
    )
    conn.execute(
        "INSERT INTO llm_call (id, user_id, created_at, day_key, provider, model, command, "
        "rounds, ok, input_tokens, output_tokens, cached_input_tokens, cache_write_tokens, "
        "price_input_per_mtok, price_output_per_mtok, price_cached_input_per_mtok, "
        "price_cache_write_per_mtok) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row.id,
            row.user_id,
            row.created_at,
            row.day_key,
            row.provider,
            row.model,
            row.command,
            row.rounds,
            int(row.ok),
            row.input_tokens,
            row.output_tokens,
            row.cached_input_tokens,
            row.cache_write_tokens,
            row.price_input_per_mtok,
            row.price_output_per_mtok,
            row.price_cached_input_per_mtok,
            row.price_cache_write_per_mtok,
        ),
    )
    return row


def _to_row(r: sqlite3.Row) -> LlmCallRow:
    return LlmCallRow(
        id=r["id"],
        user_id=r["user_id"],
        created_at=r["created_at"],
        day_key=r["day_key"],
        provider=r["provider"],
        model=r["model"],
        command=r["command"],
        rounds=r["rounds"],
        ok=bool(r["ok"]),
        input_tokens=r["input_tokens"],
        output_tokens=r["output_tokens"],
        cached_input_tokens=r["cached_input_tokens"],
        cache_write_tokens=r["cache_write_tokens"],
        price_input_per_mtok=r["price_input_per_mtok"],
        price_output_per_mtok=r["price_output_per_mtok"],
        price_cached_input_per_mtok=r["price_cached_input_per_mtok"],
        price_cache_write_per_mtok=r["price_cache_write_per_mtok"],
    )


def calls_in_window(
    conn: sqlite3.Connection, *, start: str, end: str, user_id: int = 1
) -> list[LlmCallRow]:
    """Recorded calls whose ``day_key`` falls in ``[start, end]``, oldest first."""
    rows = conn.execute(
        "SELECT * FROM llm_call WHERE user_id = ? AND day_key BETWEEN ? AND ? "
        "ORDER BY day_key, created_at, id",
        (user_id, start, end),
    ).fetchall()
    return [_to_row(r) for r in rows]
