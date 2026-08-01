"""The one recording seam every surface uses (§8.7).

`plan set` grew a second copy of its logic in the web handler and the two
drifted, so a plan set in the browser wrote no audit note (PR #24). Spend
recording has one function from the start, and these tests assert the CLI path
and the web path are indistinguishable in the ledger.
"""

from __future__ import annotations

import pytest

from coach.coach.llm.base import Usage
from coach.services.llm_usage import record_agent_result, record_usage
from coach.store.llm_calls import calls_in_window


class _FakeProvider:
    name = "grok"
    model = "grok-test"


class _FakeResult:
    def __init__(self, usage: Usage, rounds: int = 3):
        self.usage = usage
        self.rounds = rounds


PRICES = {"input": 3.0, "output": 15.0, "cached_input": 0.3, "cache_write": None}


def test_record_usage_persists_and_commits(migrated_conn):
    row = record_usage(
        migrated_conn,
        provider="grok",
        model="m",
        command="ask",
        usage=Usage(input_tokens=100, output_tokens=20, cached_input_tokens=5),
        rounds=2,
        prices=PRICES,
        home_tz="UTC",
    )
    # committed by the seam: a spend record lost to an uncommitted transaction
    # understates cost, which is the one thing this ledger exists to prevent
    (read,) = calls_in_window(migrated_conn, start=row.day_key, end=row.day_key)
    assert read.id == row.id
    assert (read.input_tokens, read.output_tokens, read.cached_input_tokens) == (100, 20, 5)
    assert read.rounds == 2


def test_agent_result_and_raw_usage_record_identically(migrated_conn):
    """The wrapper must not diverge from the primitive it wraps."""
    usage = Usage(input_tokens=7, output_tokens=3, cached_input_tokens=1)
    a = record_agent_result(
        migrated_conn,
        _FakeProvider(),
        _FakeResult(usage, rounds=4),
        command="web_ask",
        prices=PRICES,
        home_tz="UTC",
    )
    b = record_usage(
        migrated_conn,
        provider="grok",
        model="grok-test",
        command="web_ask",
        usage=usage,
        rounds=4,
        prices=PRICES,
        home_tz="UTC",
    )
    fields = (
        "provider",
        "model",
        "command",
        "rounds",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "price_input_per_mtok",
    )
    for f in fields:
        assert getattr(a, f) == getattr(b, f), f
    assert a.id != b.id  # two distinct spends, not one overwritten


def test_day_key_follows_home_tz_not_the_host_clock(migrated_conn):
    """§2.6: never derive a day boundary from the machine's local zone."""
    row = record_usage(
        migrated_conn,
        provider="grok",
        model="m",
        command="ask",
        usage=Usage(input_tokens=1, output_tokens=1),
        home_tz="Pacific/Kiritimati",  # UTC+14, the earliest zone on earth
    )
    other = record_usage(
        migrated_conn,
        provider="grok",
        model="m",
        command="ask",
        usage=Usage(input_tokens=1, output_tokens=1),
        home_tz="Pacific/Niue",  # UTC-11, among the latest
    )
    # The two zones are 25 hours apart, so their local dates ALWAYS differ by at
    # least one day, whatever instant this runs at. If day_key came from the host
    # clock both rows would land on the same day and this would fail.
    assert row.day_key > other.day_key


def test_unpriced_when_no_prices_given(migrated_conn):
    row = record_usage(
        migrated_conn,
        provider="google",
        model="free-tier",
        command="ask",
        usage=Usage(input_tokens=500, output_tokens=50),
        home_tz="UTC",
    )
    assert row.price_input_per_mtok is None
    assert row.input_tokens == 500  # tokens are still the truth


def test_a_none_rate_in_the_price_map_stays_none(migrated_conn):
    """A provider that doesn't bill cache writes must not get a fabricated rate."""
    row = record_usage(
        migrated_conn,
        provider="grok",
        model="m",
        command="ask",
        usage=Usage(input_tokens=1, output_tokens=1),
        prices=PRICES,
        home_tz="UTC",
    )
    assert row.price_cache_write_per_mtok is None
    assert row.price_input_per_mtok == 3.0


# ---- config -----------------------------------------------------------------


def _env(**over) -> dict[str, str]:
    base = {"COACH_DB_PATH": "./data/x.db"}
    base.update(over)
    return base


def test_prices_default_to_absent():
    from coach.config import load_settings

    s = load_settings(_env(), load_dotenv_file=False)
    assert s.price_input_per_mtok is None
    assert s.llm_prices == {
        "input": None,
        "output": None,
        "cached_input": None,
        "cache_write": None,
    }


def test_prices_are_read_from_the_environment():
    from coach.config import load_settings

    s = load_settings(
        _env(COACH_PRICE_INPUT_PER_MTOK="3", COACH_PRICE_OUTPUT_PER_MTOK="15.5"),
        load_dotenv_file=False,
    )
    assert s.price_input_per_mtok == 3.0
    assert s.price_output_per_mtok == 15.5
    assert s.llm_prices["cached_input"] is None


def test_a_malformed_price_is_an_error_not_a_silent_none():
    """Reading '12.oo' as unpriced would hide a typo behind a plausible report."""
    from coach.config import ConfigError, load_settings

    with pytest.raises(ConfigError, match="COACH_PRICE_INPUT_PER_MTOK"):
        load_settings(_env(COACH_PRICE_INPUT_PER_MTOK="12.oo"), load_dotenv_file=False)


def test_a_negative_price_is_rejected():
    from coach.config import ConfigError, load_settings

    with pytest.raises(ConfigError, match="negative"):
        load_settings(_env(COACH_PRICE_OUTPUT_PER_MTOK="-1"), load_dotenv_file=False)
