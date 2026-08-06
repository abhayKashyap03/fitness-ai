"""Raw BLE session -> canonical recovery sibling row (ADR-0012, §5).

This is the row the whole calibration design was built for, so the tests are
mostly about what it must NOT claim: no composite score, no fabricated timezone,
no zero standing in for a measurement that was never taken.
"""

from __future__ import annotations

import pytest

from coach.normalize.whoop_ble import parse_ble_session, summarize_session


def _session(rr_ms: list[float] | None = None, bpms: list[int] | None = None, **kw) -> dict:
    bpms = bpms if bpms is not None else [60] * (len(rr_ms or []) or 1)
    rr = rr_ms if rr_ms is not None else []
    samples = []
    for i, bpm in enumerate(bpms):
        samples.append({"at": f"2026-08-03T12:00:{i:02d}+00:00", "bpm": bpm, "rr_ms": []})
    if samples and rr:
        samples[0]["rr_ms"] = rr
    return {
        "started_at": "2026-08-03T12:00:00+00:00",
        "utc_offset": "-04:00",
        "samples": samples,
        **kw,
    }


def test_a_session_with_rr_yields_a_textbook_hrv_row():
    row = parse_ble_session(_session(rr_ms=[800.0, 810.0] * 20))
    assert row is not None
    assert row.source == "whoop_ble"
    assert row.hrv_rmssd_ms == pytest.approx(10.0)
    assert row.score_method == "textbook"
    assert row.is_official == 0


def test_no_composite_score_is_ever_emitted():
    """The load-bearing refusal (§5).

    WHOOP's recovery percentage comes from proprietary weighting we do not have.
    A 'textbook recovery score' in the same column would look like WHOOP's number
    and would not be it — the objective measurements are the honest currency.
    """
    row = parse_ble_session(_session(rr_ms=[800.0, 810.0] * 20))
    assert row is not None
    assert row.score is None
    assert row.score_scale is None


def test_unmeasurable_channels_are_absent_not_zero():
    """§2.7. The standard HR characteristic cannot report these at all."""
    row = parse_ble_session(_session(rr_ms=[800.0, 810.0] * 20))
    assert row is not None
    assert row.spo2_pct is None
    assert row.skin_temp_c is None
    assert row.resp_rate_bpm is None


def test_the_day_key_comes_from_the_offset_and_tz_name_stays_null():
    """ADR-0006: never fabricate an IANA zone from an offset."""
    row = parse_ble_session(_session(rr_ms=[800.0, 810.0] * 20))
    assert row is not None
    assert row.tz_name is None
    assert row.utc_offset == "-04:00"
    assert row.day_key == "2026-08-03"


def test_a_session_that_measured_nothing_produces_no_row():
    """An empty row asserts 'we measured you and found nothing', which is a
    different claim from 'we did not measure' (§2.7)."""
    payload = {"started_at": "2026-08-03T12:00:00+00:00", "samples": []}
    assert parse_ble_session(payload) is None


def test_a_session_with_no_start_instant_produces_no_row():
    assert parse_ble_session({"samples": []}) is None


def test_heart_rate_without_rr_still_records_a_row_but_no_hrv():
    """Common case: a strap streaming HR with the RR bit clear. That is a real
    resting-HR measurement and losing it would be wrong — but HRV must be
    absent, not estimated."""
    row = parse_ble_session(_session(bpms=[70, 62, 65, 68]))
    assert row is not None
    assert row.hrv_rmssd_ms is None
    assert row.resting_hr_bpm == 62.0


def test_thin_rr_data_gives_no_hrv_rather_than_a_small_number():
    row = parse_ble_session(_session(rr_ms=[800.0, 810.0, 805.0], bpms=[60, 61]))
    assert row is not None
    assert row.hrv_rmssd_ms is None
    assert row.resting_hr_bpm == 60.0  # the HR half is still real


def test_summarize_reports_how_much_rr_survived_cleaning():
    """Transparency about sample quality: an HRV figure from 22 clean intervals
    out of 400 recorded is a different thing from one built on all 400."""
    stats = summarize_session(_session(rr_ms=[800.0, 810.0] * 20 + [5000.0] * 10))
    assert stats.rr_used == 40
    assert stats.hrv_rmssd_ms is not None


def test_zero_bpm_samples_are_ignored_not_treated_as_a_resting_low():
    """A dropout reads as 0 and would otherwise become the 'resting' heart rate
    — a physiologically impossible number presented as a measurement."""
    stats = summarize_session(_session(bpms=[0, 65, 70]))
    assert stats.resting_hr_bpm == 65.0
