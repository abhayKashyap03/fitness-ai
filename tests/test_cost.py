"""LLM cost accounting — store + pure compute (§8.7, migration 0013).

Every expected figure below is hand-computed in the comment beside it, per §6.2.
The load-bearing behaviour is what happens when a price is UNKNOWN: unpriced must
never read as free, or the whole report becomes a fabricated number with a
currency symbol on it.
"""

from __future__ import annotations

import pytest

from coach.compute.cost import (
    TokenTotals,
    cache_hit_pct,
    call_cost,
    cost_summary,
    sum_tokens,
)
from coach.compute.trends import Insufficient
from coach.store.llm_calls import LlmCallRow, calls_in_window, record_call

PRICES = {"input": 3.0, "output": 15.0, "cached_input": 0.30, "cache_write": 3.75}


def _row(**kw) -> LlmCallRow:
    base = dict(
        id="llm:1:x:0",
        user_id=1,
        created_at="2026-07-30T00:00:00+00:00",
        day_key="2026-07-29",
        provider="grok",
        model="grok-test",
        command="ask",
        rounds=2,
        ok=True,
        input_tokens=0,
        output_tokens=0,
        cached_input_tokens=0,
        cache_write_tokens=0,
    )
    base.update(kw)
    return LlmCallRow(**base)  # type: ignore[arg-type]


# ---- call_cost -------------------------------------------------------------


def test_call_cost_is_tokens_times_rate():
    row = _row(
        input_tokens=1_000_000,
        output_tokens=500_000,
        price_input_per_mtok=3.0,
        price_output_per_mtok=15.0,
    )
    # 1.0 Mtok * $3 + 0.5 Mtok * $15 = 3.00 + 7.50 = 10.50
    assert call_cost(row) == pytest.approx(10.50)


def test_call_cost_handles_sub_million_counts():
    row = _row(input_tokens=12_000, output_tokens=3_400, **_p())
    # 0.012 * 3 = 0.036 ; 0.0034 * 15 = 0.051 ; total 0.087
    assert call_cost(row) == pytest.approx(0.087)


def _p() -> dict:
    return {
        "price_input_per_mtok": 3.0,
        "price_output_per_mtok": 15.0,
        "price_cached_input_per_mtok": 0.30,
        "price_cache_write_per_mtok": 3.75,
    }


def test_call_cost_prices_cache_buckets_separately():
    row = _row(
        input_tokens=10_000,
        cached_input_tokens=90_000,
        cache_write_tokens=10_000,
        output_tokens=1_000,
        **_p(),
    )
    # 0.01*3 = 0.03 ; 0.09*0.30 = 0.027 ; 0.01*3.75 = 0.0375 ; 0.001*15 = 0.015
    assert call_cost(row) == pytest.approx(0.03 + 0.027 + 0.0375 + 0.015)


def test_unpriced_call_is_none_not_zero():
    """The whole point: unknown price must not read as free (§2.7)."""
    row = _row(input_tokens=50_000, output_tokens=2_000)  # no rates at all
    assert call_cost(row) is None


def test_a_missing_rate_on_a_used_bucket_makes_the_call_unpriced():
    """Partial pricing presented as a total would understate cost."""
    row = _row(
        input_tokens=10_000,
        output_tokens=1_000,
        price_input_per_mtok=3.0,  # output rate absent
    )
    assert call_cost(row) is None


def test_a_missing_rate_on_an_empty_bucket_does_not_matter():
    """A provider that never reports cache writes shouldn't break pricing."""
    row = _row(
        input_tokens=10_000,
        output_tokens=1_000,
        cache_write_tokens=0,
        price_input_per_mtok=3.0,
        price_output_per_mtok=15.0,
    )
    # 0.01*3 + 0.001*15 = 0.03 + 0.015
    assert call_cost(row) == pytest.approx(0.045)


def test_a_zero_token_call_is_unpriced_not_free():
    assert call_cost(_row(**_p())) is None


# ---- token maths -----------------------------------------------------------


def test_sum_tokens_adds_every_bucket():
    rows = [
        _row(input_tokens=100, output_tokens=10, cached_input_tokens=5),
        _row(input_tokens=200, output_tokens=20, cache_write_tokens=7),
    ]
    got = sum_tokens(rows)
    assert got == TokenTotals(300, 30, 5, 7)
    assert got.total == 342  # 300 + 30 + 5 + 7
    assert got.billable_input == 305  # 300 fresh + 5 cached


def test_cache_hit_pct_is_share_of_billable_input():
    # 750 cached of (250 fresh + 750 cached) = 75%
    assert cache_hit_pct(TokenTotals(250, 0, 750, 0)) == pytest.approx(75.0)


def test_cache_hit_pct_is_none_without_input():
    """No input tokens is not a 0% hit rate — there was nothing to hit."""
    assert cache_hit_pct(TokenTotals(0, 500, 0, 0)) is None


# ---- summary ---------------------------------------------------------------


def test_empty_window_is_insufficient_not_zero_dollars():
    """ "$0.00 spent" and "nothing recorded" are different facts (§2.7)."""
    got = cost_summary([], start="2026-07-01", end="2026-07-30")
    assert isinstance(got, Insufficient)
    assert got.have == 0


def test_summary_totals_and_splits_by_command():
    rows = [
        _row(command="ask", input_tokens=10_000, output_tokens=1_000, **_p()),
        _row(command="ask", input_tokens=20_000, output_tokens=2_000, **_p()),
        _row(command="eval_grounding", input_tokens=100_000, output_tokens=10_000, **_p()),
    ]
    s = cost_summary(rows, start="2026-07-01", end="2026-07-30")
    assert not isinstance(s, Insufficient)
    assert s.calls == 3
    assert s.priced_calls == 3
    assert s.unpriced_calls == 0
    assert s.fully_priced
    # ask: (0.01+0.02)*3 = 0.09 ; (0.001+0.002)*15 = 0.045 -> 0.135
    # eval: 0.1*3 = 0.30 ; 0.01*15 = 0.15 -> 0.45
    assert s.usd == pytest.approx(0.135 + 0.45)
    by = {c.command: c for c in s.by_command}
    assert by["ask"].calls == 2
    assert by["ask"].usd == pytest.approx(0.135)
    assert by["eval_grounding"].usd == pytest.approx(0.45)


def test_mixed_priced_and_unpriced_reports_both():
    """A partly-priced window must not present its priced half as the total."""
    rows = [
        _row(input_tokens=10_000, output_tokens=1_000, **_p()),
        _row(input_tokens=999_999, output_tokens=999_999),  # unpriced
    ]
    s = cost_summary(rows, start="2026-07-01", end="2026-07-30")
    assert not isinstance(s, Insufficient)
    assert s.priced_calls == 1
    assert s.unpriced_calls == 1
    assert not s.fully_priced  # the caller must disclose the gap
    assert s.usd == pytest.approx(0.045)  # only the priced call
    # the unpriced call's tokens still count — they were really spent
    assert s.tokens.input_tokens == 1_009_999


def test_wholly_unpriced_window_has_no_dollar_figure():
    rows = [_row(input_tokens=10_000, output_tokens=1_000)]
    s = cost_summary(rows, start="2026-07-01", end="2026-07-30")
    assert not isinstance(s, Insufficient)
    assert s.usd is None
    assert s.tokens.input_tokens == 10_000


def test_failed_calls_are_counted_but_still_billed():
    """A refused or errored call can still consume tokens."""
    rows = [_row(ok=False, input_tokens=5_000, output_tokens=0, **_p())]
    s = cost_summary(rows, start="2026-07-01", end="2026-07-30")
    assert not isinstance(s, Insufficient)
    assert s.failed_calls == 1
    assert s.usd == pytest.approx(0.015)  # 0.005 Mtok * $3


# ---- store round-trip ------------------------------------------------------


def test_record_and_read_back(migrated_conn):
    record_call(
        migrated_conn,
        provider="grok",
        model="grok-test",
        command="ask",
        input_tokens=1_234,
        output_tokens=567,
        cached_input_tokens=89,
        rounds=2,
        prices=PRICES,
        created_at="2026-07-30T00:00:00+00:00",
        day_key="2026-07-29",
    )
    migrated_conn.commit()
    rows = calls_in_window(migrated_conn, start="2026-07-29", end="2026-07-29")
    assert len(rows) == 1
    r = rows[0]
    assert (r.provider, r.model, r.command) == ("grok", "grok-test", "ask")
    assert (r.input_tokens, r.output_tokens, r.cached_input_tokens) == (1_234, 567, 89)
    assert r.price_input_per_mtok == 3.0
    assert r.ok is True


def test_rates_are_stored_on_the_row_so_history_cannot_be_rewritten(migrated_conn):
    """A July call keeps July's prices even if the config changes later (§2.3)."""
    record_call(
        migrated_conn,
        provider="grok",
        model="m",
        command="ask",
        input_tokens=1_000_000,
        output_tokens=0,
        prices={"input": 3.0, "output": None, "cached_input": None, "cache_write": None},
        created_at="2026-07-30T00:00:00+00:00",
        day_key="2026-07-29",
    )
    migrated_conn.commit()
    (row,) = calls_in_window(migrated_conn, start="2026-07-29", end="2026-07-29")
    assert call_cost(row) == pytest.approx(3.0)
    # a later, cheaper price does not retroactively change what this call cost
    assert row.price_input_per_mtok == 3.0


def test_unpriced_recording_keeps_tokens(migrated_conn):
    record_call(
        migrated_conn,
        provider="google",
        model="free",
        command="ask",
        input_tokens=4_242,
        output_tokens=99,
        created_at="2026-07-30T00:00:00+00:00",
        day_key="2026-07-29",
    )
    migrated_conn.commit()
    (row,) = calls_in_window(migrated_conn, start="2026-07-29", end="2026-07-29")
    assert row.input_tokens == 4_242
    assert row.price_input_per_mtok is None
    assert call_cost(row) is None


def test_many_calls_in_the_same_second_get_distinct_ids(migrated_conn):
    """An eval run records dozens of calls, potentially within one second."""
    for _ in range(5):
        record_call(
            migrated_conn,
            provider="grok",
            model="m",
            command="eval_grounding",
            input_tokens=1,
            output_tokens=1,
            created_at="2026-07-30T00:00:00+00:00",
            day_key="2026-07-29",
        )
    migrated_conn.commit()
    rows = calls_in_window(migrated_conn, start="2026-07-29", end="2026-07-29")
    assert len(rows) == 5
    assert len({r.id for r in rows}) == 5


def test_window_excludes_days_outside_the_range(migrated_conn):
    for day in ("2026-07-01", "2026-07-15", "2026-07-30"):
        record_call(
            migrated_conn,
            provider="grok",
            model="m",
            command="ask",
            input_tokens=1,
            output_tokens=1,
            created_at=f"{day}T00:00:00+00:00",
            day_key=day,
        )
    migrated_conn.commit()
    rows = calls_in_window(migrated_conn, start="2026-07-10", end="2026-07-20")
    assert [r.day_key for r in rows] == ["2026-07-15"]


def test_llm_call_is_absent_from_the_canonical_fingerprint(migrated_conn):
    """Code-authored primary data, like `plan` and `coach_note` — not raw-derived.

    If spend rows entered the fingerprint, asking the coach a question would make
    `normalize --rebuild` look non-reproducible.
    """
    from coach.store.canonical import canonical_fingerprint

    before = canonical_fingerprint(migrated_conn)
    record_call(
        migrated_conn,
        provider="grok",
        model="m",
        command="ask",
        input_tokens=10,
        output_tokens=10,
        created_at="2026-07-30T00:00:00+00:00",
        day_key="2026-07-29",
    )
    migrated_conn.commit()
    assert canonical_fingerprint(migrated_conn) == before
