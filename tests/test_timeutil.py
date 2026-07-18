"""Day-boundary / timezone handling — a known bug class (§2.6)."""

from __future__ import annotations

from coach.timeutil import day_key, offset_tz_name, parse_instant, parse_offset


def test_parse_instant_handles_z_and_offset():
    assert parse_instant("2026-07-10T12:00:00.000Z").isoformat() == "2026-07-10T12:00:00+00:00"
    assert parse_instant("2026-07-10T08:00:00-04:00").isoformat() == "2026-07-10T12:00:00+00:00"


def test_day_key_same_day_utc():
    assert day_key("2026-07-10T12:00:00Z", "-04:00") == "2026-07-10"


def test_day_key_crosses_midnight_backwards():
    # 02:30 UTC at -05:00 is 21:30 the previous local day
    assert day_key("2026-07-10T02:30:00Z", "-05:00") == "2026-07-09"


def test_day_key_crosses_midnight_forwards():
    # 23:30 UTC at +02:00 is 01:30 the next local day
    assert day_key("2026-07-10T23:30:00Z", "+02:00") == "2026-07-11"


def test_day_key_none_offset_is_utc():
    assert day_key("2026-07-10T23:30:00Z", None) == "2026-07-10"


def test_parse_offset_variants():
    assert parse_offset("Z").utcoffset(None).total_seconds() == 0
    assert parse_offset("-05:00").utcoffset(None).total_seconds() == -5 * 3600
    assert parse_offset("+01:30").utcoffset(None).total_seconds() == 5400


def test_offset_tz_name_fallback():
    assert offset_tz_name(None) == "UTC"
    assert offset_tz_name("-04:00") == "-04:00"
