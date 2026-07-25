"""Pure WHOOP raw -> canonical parsers (CLAUDE.md §2.4).

No I/O. Given a raw WHOOP record (exactly as stored in ``raw_events.payload``),
return a canonical row dataclass — or ``None`` when there is nothing to record
(e.g. an unscored recovery). Purity is what makes ``--rebuild`` safe: the same
raw always yields the same canonical row.

Vendor field names stay behind this boundary; downstream code sees only our
canonical shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..adapters.whoop.sport_map import whoop_sport_to_canonical
from ..timeutil import day_key, normalize_offset, parse_instant, to_utc_iso

KJ_PER_KCAL = 4.184


@dataclass(frozen=True)
class RecoveryRow:
    user_id: int
    day_key: str
    source: str
    measured_at: str | None
    tz_name: str | None  # strictly IANA, NULL when unknown (§2.6)
    utc_offset: str | None  # e.g. '-05:00'
    hrv_rmssd_ms: float | None
    resting_hr_bpm: float | None
    spo2_pct: float | None
    skin_temp_c: float | None
    resp_rate_bpm: float | None
    score: float | None
    score_scale: str | None
    score_method: str | None
    is_official: int


@dataclass(frozen=True)
class WorkoutRow:
    user_id: int
    source: str
    external_id: str | None
    sport_type: str
    source_sport_raw: str | None
    start_at: str
    end_at: str
    tz_name: str | None  # strictly IANA, NULL when unknown (§2.6)
    utc_offset: str | None  # e.g. '-05:00'
    day_key: str
    duration_s: int | None
    kcal_active: float | None
    kcal_total: float | None
    avg_hr_bpm: float | None
    max_hr_bpm: float | None
    strain: float | None
    distance_m: float | None
    hr_zones_json: str | None


@dataclass(frozen=True)
class SleepRow:
    user_id: int
    day_key: str  # local day the sleep ENDED (the physiological day it serves)
    source: str
    external_id: str
    is_nap: int
    start_at: str
    end_at: str
    tz_name: str | None
    utc_offset: str | None
    in_bed_min: float | None
    awake_min: float | None
    light_min: float | None
    sws_min: float | None
    rem_min: float | None
    no_data_min: float | None
    sleep_cycle_count: int | None
    disturbance_count: int | None
    respiratory_rate: float | None
    need_baseline_min: float | None
    need_from_debt_min: float | None
    need_from_strain_min: float | None
    need_from_nap_min: float | None
    performance_pct: float | None
    consistency_pct: float | None
    efficiency_pct: float | None
    score_method: str | None
    is_official: int


def _milli_to_min(milli: object) -> float | None:
    """Milliseconds -> minutes (1 dp); absent stays absent (§2.7)."""
    if not isinstance(milli, int | float) or isinstance(milli, bool):
        return None
    return round(milli / 60_000, 1)


def parse_sleep(payload: dict, *, user_id: int = 1) -> SleepRow | None:
    """WHOOP sleep record -> SleepRow. Returns None if unscored or id-less.

    Self-contained: the record carries its own ``timezone_offset``. ``day_key``
    is derived from the END instant — the night of the 14th/15th belongs to the
    15th, the day the sleep serves. Missing sub-objects leave NULLs.
    """
    if payload.get("score_state") != "SCORED":
        return None
    ext = payload.get("id")
    start, end = payload.get("start"), payload.get("end")
    if ext is None or not start or not end:
        return None
    offset = payload.get("timezone_offset")
    score = payload.get("score") or {}
    stages = score.get("stage_summary") or {}
    need = score.get("sleep_needed") or {}

    return SleepRow(
        user_id=user_id,
        day_key=day_key(end, offset),
        source="whoop_api",
        external_id=str(ext),
        is_nap=int(bool(payload.get("nap"))),
        start_at=to_utc_iso(parse_instant(start)),
        end_at=to_utc_iso(parse_instant(end)),
        tz_name=None,  # WHOOP is offset-only; IANA unknown (§2.6)
        utc_offset=normalize_offset(offset),
        in_bed_min=_milli_to_min(stages.get("total_in_bed_time_milli")),
        awake_min=_milli_to_min(stages.get("total_awake_time_milli")),
        light_min=_milli_to_min(stages.get("total_light_sleep_time_milli")),
        sws_min=_milli_to_min(stages.get("total_slow_wave_sleep_time_milli")),
        rem_min=_milli_to_min(stages.get("total_rem_sleep_time_milli")),
        no_data_min=_milli_to_min(stages.get("total_no_data_time_milli")),
        sleep_cycle_count=stages.get("sleep_cycle_count"),
        disturbance_count=stages.get("disturbance_count"),
        respiratory_rate=score.get("respiratory_rate"),
        need_baseline_min=_milli_to_min(need.get("baseline_milli")),
        need_from_debt_min=_milli_to_min(need.get("need_from_sleep_debt_milli")),
        need_from_strain_min=_milli_to_min(need.get("need_from_recent_strain_milli")),
        need_from_nap_min=_milli_to_min(need.get("need_from_recent_nap_milli")),
        performance_pct=score.get("sleep_performance_percentage"),
        consistency_pct=score.get("sleep_consistency_percentage"),
        efficiency_pct=score.get("sleep_efficiency_percentage"),
        score_method="whoop_proprietary",
        is_official=1,
    )


def parse_recovery(
    payload: dict,
    *,
    tz_offset: str | None = None,
    resp_rate: float | None = None,
    user_id: int = 1,
) -> RecoveryRow | None:
    """WHOOP recovery record -> RecoveryRow. Returns None if unscored.

    ``tz_offset`` comes from the associated cycle (the recovery record itself
    carries no offset — see ADR-0006). When absent, the day is computed in UTC
    and ``utc_offset`` is NULL. ``tz_name`` is always NULL for WHOOP (offset-only
    source); it stays reserved for IANA-capable sources like HealthKit.

    ``resp_rate`` comes from the associated sleep record (WHOOP reports
    respiratory rate there, not on recovery) — joined by ``sleep_id`` in the
    runner. Still pure: same raw inputs, same row. It widens the objective
    calibration currency (§5) ahead of the BLE adapter.
    """
    if payload.get("score_state") != "SCORED":
        return None
    score = payload.get("score") or {}
    if not score:
        return None

    created = payload.get("created_at")
    return RecoveryRow(
        user_id=user_id,
        day_key=day_key(created, tz_offset) if created else "",
        source="whoop_api",
        measured_at=to_utc_iso(parse_instant(created)) if created else None,
        tz_name=None,  # WHOOP is offset-only; IANA unknown (§2.6)
        utc_offset=normalize_offset(tz_offset),
        hrv_rmssd_ms=score.get("hrv_rmssd_milli"),
        resting_hr_bpm=score.get("resting_heart_rate"),
        spo2_pct=score.get("spo2_percentage"),
        skin_temp_c=score.get("skin_temp_celsius"),
        resp_rate_bpm=resp_rate,  # from the linked sleep record (see docstring)
        score=score.get("recovery_score"),
        score_scale="whoop_0_100",
        score_method="whoop_proprietary",
        is_official=1,
    )


def _kj_to_kcal(kj: float | None) -> float | None:
    if kj is None:
        return None
    return round(kj / KJ_PER_KCAL, 1)


def parse_workout(payload: dict, *, user_id: int = 1) -> WorkoutRow:
    """WHOOP workout record -> WorkoutRow. Self-contained (carries its offset)."""
    offset = payload.get("timezone_offset")
    start = payload["start"]
    end = payload["end"]
    duration_s = int((parse_instant(end) - parse_instant(start)).total_seconds())

    score = payload.get("score") or {}
    sport_id = payload.get("sport_id")
    sport_name = payload.get("sport_name")
    # real WHOOP v2 uses "zone_durations" (plural); tolerate the singular too
    zones = score.get("zone_durations", score.get("zone_duration"))

    return WorkoutRow(
        user_id=user_id,
        source="whoop_api",
        external_id=str(payload.get("id")) if payload.get("id") is not None else None,
        sport_type=whoop_sport_to_canonical(sport_id, sport_name),
        source_sport_raw=(
            payload.get("sport_name")
            if payload.get("sport_name") is not None
            else (str(sport_id) if sport_id is not None else None)
        ),
        start_at=to_utc_iso(parse_instant(start)),
        end_at=to_utc_iso(parse_instant(end)),
        tz_name=None,  # WHOOP is offset-only; IANA unknown (§2.6)
        utc_offset=normalize_offset(offset),
        day_key=day_key(start, offset),
        duration_s=duration_s,
        kcal_active=None,  # WHOOP does not separate active vs total
        kcal_total=_kj_to_kcal(score.get("kilojoule")),
        avg_hr_bpm=score.get("average_heart_rate"),
        max_hr_bpm=score.get("max_heart_rate"),
        strain=score.get("strain"),
        distance_m=score.get("distance_meter"),
        hr_zones_json=json.dumps(zones) if zones is not None else None,
    )
