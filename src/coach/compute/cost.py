"""Deterministic LLM cost math (§8.7). Pure — no I/O, no model calls.

§11 forbids optimizing before a measured problem exists, and §8.7 names the API
as the project's only meaningful running cost. This module is the measurement:
tokens in, an honest summary out.

Two rules shape every function here:

* **Unpriced is not free (§2.7).** A call whose rates were unknown at the time
  contributes its tokens and is counted as UNPRICED. It never contributes 0.00
  to a dollar total, because a total that quietly treats unknown as zero is a
  fabricated number wearing a currency symbol.
* **Code computes (§2.2).** Nothing here is estimated or interpolated; every
  figure is arithmetic over recorded token counts and recorded rates.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ..store.llm_calls import LlmCallRow
from .trends import Insufficient

_PER_MTOK = 1_000_000.0


@dataclass(frozen=True)
class TokenTotals:
    """Summed token counts. Zero here is a true zero — the provider reported it."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def billable_input(self) -> int:
        """Fresh + cached input. See :func:`cache_hit_pct` for the convention."""
        return self.input_tokens + self.cached_input_tokens

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cached_input_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True)
class CommandCost:
    """Spend attributed to one command (``ask``, ``eval_grounding``, ...)."""

    command: str
    calls: int
    tokens: TokenTotals
    usd: float | None  # None when NO call in this bucket was priced
    unpriced_calls: int


@dataclass(frozen=True)
class CostSummary:
    """What the recorded window actually cost."""

    start: str
    end: str
    calls: int
    tokens: TokenTotals
    usd: float | None  # None when nothing in the window was priced
    priced_calls: int
    unpriced_calls: int
    failed_calls: int
    cache_hit_pct: float | None
    by_command: list[CommandCost] = field(default_factory=list)

    @property
    def fully_priced(self) -> bool:
        return self.unpriced_calls == 0 and self.calls > 0


def call_cost(row: LlmCallRow) -> float | None:
    """USD for one call, or None when it cannot be priced honestly.

    A rate is only *needed* for a bucket that actually has tokens: a provider
    that never reports cache writes doesn't make the call unpriceable. But if
    any non-empty bucket lacks a rate, the answer is None rather than a partial
    total presented as a whole one.
    """
    buckets = (
        (row.input_tokens, row.price_input_per_mtok),
        (row.output_tokens, row.price_output_per_mtok),
        (row.cached_input_tokens, row.price_cached_input_per_mtok),
        (row.cache_write_tokens, row.price_cache_write_per_mtok),
    )
    total = 0.0
    priced_any = False
    for tokens, rate in buckets:
        if not tokens:
            continue
        if rate is None:
            return None  # a needed rate is missing -> unpriced, not partial
        total += tokens / _PER_MTOK * rate
        priced_any = True
    return total if priced_any else None


def sum_tokens(rows: Iterable[LlmCallRow]) -> TokenTotals:
    """Add up token counts across calls."""
    i = o = c = w = 0
    for r in rows:
        i += r.input_tokens
        o += r.output_tokens
        c += r.cached_input_tokens
        w += r.cache_write_tokens
    return TokenTotals(i, o, c, w)


def cache_hit_pct(tokens: TokenTotals) -> float | None:
    """Share of input tokens served from cache, or None when there is no input.

    Convention: ``cached_input_tokens`` sits *alongside* fresh input, not inside
    it, so billable input is the sum. All three shipped adapters normalize to
    this — Anthropic reports ``cache_read_input_tokens`` separately already, and
    the Grok and Google adapters subtract cached from their all-inclusive prompt
    count. A future adapter that bundles them would read as 0% here: absence
    showing as absence, not a claim that caching failed.

    This is the §8.7 lever worth watching. The system prompt is stable and
    should be cache-hitting; a rate that collapses means caching regressed.
    """
    billable = tokens.billable_input
    if billable <= 0:
        return None
    return tokens.cached_input_tokens / billable * 100.0


def _bucket_usd(rows: Sequence[LlmCallRow]) -> tuple[float | None, int]:
    """(total USD or None, count of unpriced calls) for a set of calls."""
    total = 0.0
    priced = 0
    unpriced = 0
    for r in rows:
        c = call_cost(r)
        if c is None:
            unpriced += 1
        else:
            total += c
            priced += 1
    return (total if priced else None), unpriced


def cost_summary(rows: Sequence[LlmCallRow], *, start: str, end: str) -> CostSummary | Insufficient:
    """Summarize spend over a window, or say there is nothing recorded.

    Returns :class:`Insufficient` on an empty window rather than a zeroed
    summary: "you have spent $0.00" and "nothing has been recorded" are
    different facts (§2.7), and only one of them is true before the first call.
    """
    if not rows:
        return Insufficient(have=0, needed=1)

    tokens = sum_tokens(rows)
    usd, unpriced = _bucket_usd(rows)

    commands: dict[str, list[LlmCallRow]] = {}
    for r in rows:
        commands.setdefault(r.command, []).append(r)
    by_command = []
    for name in sorted(commands):
        bucket = commands[name]
        b_usd, b_unpriced = _bucket_usd(bucket)
        by_command.append(
            CommandCost(
                command=name,
                calls=len(bucket),
                tokens=sum_tokens(bucket),
                usd=b_usd,
                unpriced_calls=b_unpriced,
            )
        )

    return CostSummary(
        start=start,
        end=end,
        calls=len(rows),
        tokens=tokens,
        usd=usd,
        priced_calls=len(rows) - unpriced,
        unpriced_calls=unpriced,
        failed_calls=sum(1 for r in rows if not r.ok),
        cache_hit_pct=cache_hit_pct(tokens),
        by_command=by_command,
    )
