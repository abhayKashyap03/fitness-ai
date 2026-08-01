"""One seam for recording model spend (§8.7).

Three surfaces run the agent — the CLI (`coach ask`), the web chat, and the
grounding eval — and each one bills the same API. When `plan set` grew a second
copy of its logic in the web handler the two silently drifted (a plan set in the
browser wrote no audit note), so spend recording gets one function from the
start rather than three near-copies.

Nothing here decides prices. It stamps whatever rates configuration supplied,
and records the call unpriced when there are none (§2.7).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..coach.llm.base import Usage
from ..store.llm_calls import LlmCallRow, record_call


def _now(home_tz: str) -> tuple[str, str]:
    """(UTC instant, local day_key) — never the host machine's zone (§2.6)."""
    local = datetime.now(ZoneInfo(home_tz))
    return local.astimezone(UTC).isoformat(), local.date().isoformat()


def record_usage(
    conn: sqlite3.Connection,
    *,
    provider: str,
    model: str,
    command: str,
    usage: Usage,
    rounds: int = 0,
    ok: bool = True,
    prices: dict[str, float | None] | None = None,
    home_tz: str = "UTC",
    user_id: int = 1,
) -> LlmCallRow:
    """Record one unit of spend and commit it.

    Committed here on purpose: a spend record that is lost because the caller
    raised before committing understates cost, and an understated cost is worse
    than a noisy one — the whole point is to stop guessing what this costs.
    """
    created_at, day = _now(home_tz)
    row = record_call(
        conn,
        provider=provider,
        model=model,
        command=command,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        rounds=rounds,
        ok=ok,
        prices=prices,
        created_at=created_at,
        day_key=day,
        user_id=user_id,
    )
    conn.commit()
    return row


def record_agent_result(
    conn: sqlite3.Connection,
    provider: object,
    result: object,
    *,
    command: str,
    prices: dict[str, float | None] | None = None,
    home_tz: str = "UTC",
    user_id: int = 1,
) -> LlmCallRow:
    """Convenience wrapper over an ``AgentResult`` from :func:`coach.agent.ask`.

    Takes the provider and result loosely typed to avoid importing the agent
    module here — this seam is about persistence, not about the agent loop.
    """
    return record_usage(
        conn,
        provider=getattr(provider, "name", "unknown"),
        model=getattr(provider, "model", "unknown"),
        command=command,
        usage=getattr(result, "usage", Usage()),
        rounds=getattr(result, "rounds", 0),
        prices=prices,
        home_tz=home_tz,
        user_id=user_id,
    )
