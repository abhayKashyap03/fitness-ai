"""Pure raw -> canonical for locally-read BLE sessions (Adapter B, ADR-0012).

This is the **sibling row** the whole calibration design exists for. A
`whoop_ble` recovery row and a `whoop_api` recovery row describe the same day
from two independent instruments, and §5's split is what makes them comparable:

* **Objective measurements** — `hrv_rmssd_ms`, `resting_hr_bpm` — are physical
  quantities and ARE comparable across sources. This is the honest calibration
  currency.
* **The composite score** is NOT comparable, so this normalizer does not emit
  one at all. WHOOP's recovery percentage comes from proprietary weighting we
  do not have; inventing a "textbook recovery score" here and putting it in the
  same column would manufacture a number that looks like WHOOP's and is not.
  `score` stays NULL, and `score_method='textbook'` / `is_official=0` mark what
  these figures are.

Pure, like every other normalizer — the same raw always yields the same row, so
`normalize --rebuild` stays byte-identical (§2.1).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..adapters.whoop_ble.heartrate import clean_rr, rmssd_ms
from ..normalize.whoop import RecoveryRow
from ..timeutil import day_key, normalize_offset, parse_instant, to_utc_iso


@dataclass(frozen=True)
class BleSessionStats:
    """What one recorded session yields, before it becomes a canonical row."""

    beats: int
    rr_used: int
    hrv_rmssd_ms: float | None
    resting_hr_bpm: float | None
    mean_hr_bpm: float | None


def summarize_session(payload: dict) -> BleSessionStats:
    """Compute the objective measurements from a recorded BLE session.

    ``payload`` is the raw event exactly as stored: the session envelope written
    by ``adapters.whoop_ble.record``.
    """
    samples = payload.get("samples") or []
    bpms = [s["bpm"] for s in samples if isinstance(s.get("bpm"), int) and s["bpm"] > 0]
    rr: list[float] = []
    for s in samples:
        rr.extend(s.get("rr_ms") or [])
    usable = clean_rr(rr)

    return BleSessionStats(
        beats=len(bpms),
        rr_used=len(usable),
        hrv_rmssd_ms=rmssd_ms(rr),
        # "Resting" here means the quietest sustained heart rate seen in the
        # window, approximated by the minimum. It is NOT WHOOP's resting HR,
        # which is derived from sleep — a short waking recording cannot produce
        # that, and the honest thing is a differently-derived number in a
        # clearly-marked sibling row rather than a pretend equivalent.
        resting_hr_bpm=float(min(bpms)) if bpms else None,
        mean_hr_bpm=(sum(bpms) / len(bpms)) if bpms else None,
    )


def parse_ble_session(payload: dict, *, user_id: int = 1) -> RecoveryRow | None:
    """Raw BLE session -> a canonical ``recovery`` row, or None if it says nothing.

    Returns None when the session produced neither an HRV figure nor a heart
    rate. An empty row would assert "we measured you and found nothing", which
    is a different claim from "we did not measure" (§2.7).
    """
    started = payload.get("started_at")
    if not started:
        return None

    stats = summarize_session(payload)
    if stats.hrv_rmssd_ms is None and stats.resting_hr_bpm is None:
        return None

    instant_iso = to_utc_iso(parse_instant(started))
    offset = normalize_offset(payload.get("utc_offset"))

    return RecoveryRow(
        user_id=user_id,
        day_key=day_key(instant_iso, offset),
        source="whoop_ble",
        measured_at=instant_iso,
        # The strap reports no zone name and we will not fabricate one from an
        # offset (§2.6, ADR-0006).
        tz_name=None,
        utc_offset=offset,
        hrv_rmssd_ms=stats.hrv_rmssd_ms,
        resting_hr_bpm=stats.resting_hr_bpm,
        # Not measurable from the standard Heart Rate characteristic. Absent,
        # not zero — the fd4b family may supply these later.
        spo2_pct=None,
        skin_temp_c=None,
        resp_rate_bpm=None,
        # No composite score, deliberately. See the module docstring.
        score=None,
        score_scale=None,
        score_method="textbook",
        is_official=0,
    )
