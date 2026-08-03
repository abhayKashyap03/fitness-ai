"""Parser for the standard BLE Heart Rate Measurement characteristic (0x2A37).

The MG strap exposes this (confirmed 2026-08-03, ADR-0012), and it is worth more
than it first appears. It is a **Bluetooth SIG standard**, not WHOOP's protocol:
the layout below is from the public specification, so nothing here is reverse
engineered and a firmware push cannot quietly change it without breaking every
generic heart-rate app in existence. That makes this the *durable* local read —
the `fd4b` drain is richer, but this is the one that will still work in a year.

**RR intervals are the reason this matters.** When present they are beat-to-beat
timings, and beat-to-beat timings are what HRV is computed from. That gives us
`hrv_rmssd_ms` — §5's "objective measurement, comparable across sources" — from
a completely independent instrument, which is exactly the calibration currency
ADR-0012 needs and the whole point of the sibling-row design.

Wire format (SIG Heart Rate Measurement):

    byte 0  flags
              bit 0    0 = 8-bit HR value, 1 = 16-bit
              bit 1    sensor contact detected   (meaningful only if bit 2)
              bit 2    sensor contact supported
              bit 3    Energy Expended field present
              bit 4    RR-Interval field(s) present
    then    heart rate           uint8 or uint16 LE, bpm
    then    energy expended      uint16 LE, kilojoules   (only if bit 3)
    then    RR intervals         uint16 LE each, units of 1/1024 s (only if bit 4)

Pure functions only — no I/O, no bleak. Same rule as every other normalizer in
this repo: purity is what keeps `--rebuild` honest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# RR intervals arrive in units of 1/1024 second, not milliseconds. Getting this
# constant wrong scales every HRV number by 2.4% and nothing would look broken.
_RR_TICKS_PER_SECOND = 1024.0


@dataclass(frozen=True)
class HeartRateMeasurement:
    """One notification from 0x2A37."""

    bpm: int
    rr_intervals_ms: list[float] = field(default_factory=list)
    energy_expended_kj: int | None = None
    # Tri-state on purpose (§2.7): True/False when the sensor reports contact,
    # None when it does not support reporting at all. "Not supported" and "not
    # touching your skin" are different facts and must not collapse.
    sensor_contact: bool | None = None


def parse_measurement(data: bytes) -> HeartRateMeasurement:
    """Decode one 0x2A37 notification payload.

    Raises ``ValueError`` on a truncated or malformed frame rather than
    returning a partial reading — a heart rate invented from a short buffer is
    exactly the kind of fabricated number this project refuses to produce.
    """
    if len(data) < 2:
        raise ValueError(f"heart-rate frame too short: {len(data)} byte(s)")

    flags = data[0]
    hr_is_16bit = bool(flags & 0x01)
    contact_supported = bool(flags & 0x04)
    contact_detected = bool(flags & 0x02)
    has_energy = bool(flags & 0x08)
    has_rr = bool(flags & 0x10)

    offset = 1
    if hr_is_16bit:
        if len(data) < offset + 2:
            raise ValueError("frame claims a 16-bit heart rate but is too short")
        bpm = int.from_bytes(data[offset : offset + 2], "little")
        offset += 2
    else:
        bpm = data[offset]
        offset += 1

    energy: int | None = None
    if has_energy:
        if len(data) < offset + 2:
            raise ValueError("frame claims energy expended but is too short")
        energy = int.from_bytes(data[offset : offset + 2], "little")
        offset += 2

    rr: list[float] = []
    if has_rr:
        remaining = len(data) - offset
        if remaining % 2 != 0:
            raise ValueError(f"RR-interval block has an odd byte count ({remaining})")
        while offset + 1 < len(data):
            ticks = int.from_bytes(data[offset : offset + 2], "little")
            rr.append(ticks * 1000.0 / _RR_TICKS_PER_SECOND)
            offset += 2

    return HeartRateMeasurement(
        bpm=bpm,
        rr_intervals_ms=rr,
        energy_expended_kj=energy,
        sensor_contact=contact_detected if contact_supported else None,
    )


# ---- HRV from beat-to-beat intervals ---------------------------------------

# Below this many intervals, RMSSD is a number with no meaning. 20 successive
# differences is a conventional floor for an ultra-short-term reading; fewer is
# reported as insufficient rather than as a small-sample estimate, because a
# figure printed next to WHOOP's official score will be read as comparable to it.
MIN_RR_FOR_RMSSD = 21


def rmssd_ms(rr_intervals_ms: list[float]) -> float | None:
    """Root mean square of successive differences, in ms. None if insufficient.

    The textbook HRV formula, and deliberately the textbook one: §5 requires our
    computed metrics to be honestly *different* from WHOOP's proprietary score,
    not a guess at reproducing it. This is `score_method='textbook'` territory.

    Returns None rather than a small number when there is not enough data — an
    insufficient result must be visible as insufficient (§2.7).
    """
    clean = clean_rr(rr_intervals_ms)
    if len(clean) < MIN_RR_FOR_RMSSD:
        return None
    diffs = [clean[i + 1] - clean[i] for i in range(len(clean) - 1)]
    return math.sqrt(sum(d * d for d in diffs) / len(diffs))


# Physiologically plausible beat-to-beat range: 300 ms is 200 bpm, 2000 ms is
# 30 bpm. Anything outside is an artefact — a missed or double-counted beat —
# and artefacts inflate RMSSD dramatically because the metric squares the
# differences. Filtering them is standard practice, not data massaging.
_RR_MIN_MS = 300.0
_RR_MAX_MS = 2000.0


def clean_rr(rr_intervals_ms: list[float]) -> list[float]:
    """Drop physiologically impossible intervals.

    Kept as its own function, and tested, because this filter is the difference
    between an HRV number that means something and one that tracks how often the
    strap lost contact with skin.
    """
    return [rr for rr in rr_intervals_ms if _RR_MIN_MS <= rr <= _RR_MAX_MS]
