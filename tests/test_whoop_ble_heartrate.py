"""BLE Heart Rate Measurement parsing and textbook HRV (ADR-0012).

Byte-level tests against the Bluetooth SIG layout. Nothing here needs a radio —
that is the point of keeping the parser pure.

The cases that matter are the ones where a wrong answer would look plausible:
the 1/1024-second RR unit, a truncated frame, and the artefact filter. All three
produce numbers that pass eyeballing while being wrong.
"""

from __future__ import annotations

import math

import pytest

from coach.adapters.whoop_ble.heartrate import (
    MIN_RR_FOR_RMSSD,
    clean_rr,
    parse_measurement,
    rmssd_ms,
)


def _rr_ticks(ms: float) -> int:
    """Encode a millisecond interval the way the strap does."""
    return round(ms * 1024.0 / 1000.0)


# ---- the wire format -------------------------------------------------------


def test_minimal_frame_8bit_heart_rate():
    m = parse_measurement(bytes([0x00, 62]))
    assert m.bpm == 62
    assert m.rr_intervals_ms == []
    assert m.energy_expended_kj is None
    assert m.sensor_contact is None  # not supported -> unknown, not False


def test_16bit_heart_rate_is_little_endian():
    """Only matters above 255 bpm in practice, but a byte-order slip here would
    silently halve or explode every reading that uses the wide format."""
    m = parse_measurement(bytes([0x01, 0x2C, 0x01]))  # 0x012C = 300
    assert m.bpm == 300


def test_rr_intervals_are_converted_from_1024ths_not_milliseconds():
    """The single most dangerous constant in this module.

    Treating ticks as milliseconds scales every HRV figure by 2.4% and nothing
    looks broken — it just quietly disagrees with WHOOP forever.
    """
    m = parse_measurement(bytes([0x10, 60]) + _rr_ticks(1000.0).to_bytes(2, "little"))
    assert m.rr_intervals_ms == pytest.approx([1000.0], abs=1.0)


def test_multiple_rr_intervals_in_one_frame():
    payload = bytes([0x10, 60])
    for ms in (800.0, 820.0, 810.0):
        payload += _rr_ticks(ms).to_bytes(2, "little")
    m = parse_measurement(payload)
    assert m.rr_intervals_ms == pytest.approx([800.0, 820.0, 810.0], abs=1.0)


def test_energy_expended_is_skipped_correctly_before_rr():
    """Field ORDER is the trap: energy sits between the HR and the RR block, so
    mis-skipping it makes the first RR interval a garbage value built from the
    energy bytes — a plausible-looking number that is pure fiction."""
    payload = bytes([0x18, 60]) + (500).to_bytes(2, "little")
    payload += _rr_ticks(850.0).to_bytes(2, "little")
    m = parse_measurement(payload)
    assert m.energy_expended_kj == 500
    assert m.rr_intervals_ms == pytest.approx([850.0], abs=1.0)


def test_sensor_contact_is_tri_state():
    """ "Not supported" and "not touching skin" are different facts (§2.7)."""
    assert parse_measurement(bytes([0x00, 60])).sensor_contact is None  # unsupported
    assert parse_measurement(bytes([0x04, 60])).sensor_contact is False  # supported, off
    assert parse_measurement(bytes([0x06, 60])).sensor_contact is True  # supported, on


def test_a_truncated_frame_raises_rather_than_guessing():
    """A heart rate invented from a short buffer is a fabricated measurement."""
    with pytest.raises(ValueError, match="too short"):
        parse_measurement(bytes([0x00]))
    with pytest.raises(ValueError, match="16-bit"):
        parse_measurement(bytes([0x01, 0x2C]))
    with pytest.raises(ValueError, match="energy"):
        parse_measurement(bytes([0x08, 60, 0x01]))


def test_an_odd_rr_block_raises():
    with pytest.raises(ValueError, match="odd byte count"):
        parse_measurement(bytes([0x10, 60, 0x01, 0x02, 0x03]))


# ---- artefact filtering ----------------------------------------------------


def test_impossible_intervals_are_dropped():
    """A missed beat doubles an interval; a double-counted one halves it. RMSSD
    squares the differences, so a single artefact can dominate the result."""
    assert clean_rr([800.0, 250.0, 810.0, 2500.0, 805.0]) == [800.0, 810.0, 805.0]


def test_the_bounds_are_inclusive_at_plausible_extremes():
    assert clean_rr([300.0, 2000.0]) == [300.0, 2000.0]


# ---- textbook RMSSD --------------------------------------------------------


def test_rmssd_matches_the_hand_computed_value():
    """Pinned against arithmetic done by hand, not against our own output."""
    rr = [800.0, 810.0, 800.0, 810.0] * 10  # 40 intervals, diffs alternate ±10
    # every successive difference is 10 ms in magnitude -> RMSSD is exactly 10
    assert rmssd_ms(rr) == pytest.approx(10.0)


def test_rmssd_of_a_perfectly_regular_series_is_zero():
    assert rmssd_ms([800.0] * 40) == pytest.approx(0.0)


def test_rmssd_is_insufficient_rather_than_small_when_data_is_thin():
    """§2.7. A number printed beside WHOOP's official score will be read as
    comparable to it, so a 3-beat estimate must not be printed at all."""
    assert rmssd_ms([800.0, 810.0, 805.0]) is None
    assert rmssd_ms([]) is None


def test_the_threshold_is_applied_after_cleaning_not_before():
    """Otherwise a series padded with artefacts passes the count check and then
    computes RMSSD from far fewer real beats than the caller believes."""
    rr = [800.0, 810.0] * (MIN_RR_FOR_RMSSD // 2 + 1)
    assert rmssd_ms(rr) is not None
    polluted = rr[:4] + [5000.0] * 40  # plenty of values, almost all garbage
    assert rmssd_ms(polluted) is None


def test_rmssd_is_a_real_square_root_not_a_mean_difference():
    """Guards against the classic implementation slip of averaging |differences|."""
    rr = ([800.0, 850.0] * 20)[:40]  # diffs alternate +50/-50 -> RMSSD 50
    assert rmssd_ms(rr) == pytest.approx(50.0)
    assert rmssd_ms(rr) != pytest.approx(math.sqrt(50.0))
