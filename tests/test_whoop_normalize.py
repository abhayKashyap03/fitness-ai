"""Pure WHOOP normalizers, against REAL (scrubbed) fixtures + edge cases."""

from __future__ import annotations

import json
from pathlib import Path

from coach.adapters.whoop.sport_map import whoop_sport_to_canonical
from coach.normalize.whoop import parse_recovery, parse_workout

FIX = Path(__file__).parent / "fixtures" / "whoop"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


# ---- sport map -------------------------------------------------------------


def test_sport_map_prefers_name():
    # names are self-describing in real payloads; case-insensitive
    assert whoop_sport_to_canonical(sport_name="swimming") == "swim"
    assert whoop_sport_to_canonical(sport_name="walking") == "walk"
    assert whoop_sport_to_canonical(sport_name="running") == "run"
    assert whoop_sport_to_canonical(sport_name="Weightlifting") == "strength"
    assert whoop_sport_to_canonical(sport_name="activity") == "other"
    assert whoop_sport_to_canonical(sport_name="paragliding") == "other"  # unknown


def test_sport_map_id_fallback():
    # used only when a name is absent; ids confirmed from real data
    assert whoop_sport_to_canonical(33) == "swim"
    assert whoop_sport_to_canonical(63) == "walk"
    assert whoop_sport_to_canonical(999999) == "other"  # never dropped
    assert whoop_sport_to_canonical(None) == "other"


# ---- recovery --------------------------------------------------------------


def test_recovery_scored_maps_objective_and_score():
    rec = _load("recovery_page1.json")["records"][0]
    row = parse_recovery(rec, tz_offset="-10:00")  # cycle offset (Hawaii)
    assert row is not None
    assert row.hrv_rmssd_ms == 62.482277
    assert row.resting_hr_bpm == 68.0
    assert row.score == 37.0
    assert row.is_official == 1
    assert row.score_method == "whoop_proprietary"
    assert row.day_key == "2026-06-01"
    assert row.source == "whoop_api"


def test_recovery_pending_returns_none():
    rec = _load("recovery_page1.json")["records"][1]  # the synthetic PENDING record
    assert parse_recovery(rec, tz_offset="-10:00") is None


def test_recovery_offset_stored_separately_from_tz_name():
    rec = _load("recovery_page1.json")["records"][0]
    row = parse_recovery(rec, tz_offset="-10:00")
    assert row is not None
    assert row.utc_offset == "-10:00"  # offset in its own column
    assert row.tz_name is None  # tz_name is IANA-only; WHOOP gives none (§2.6)


def test_recovery_missing_offset_is_null_not_utc():
    rec = _load("recovery_page1.json")["records"][0]
    row = parse_recovery(rec, tz_offset=None)
    assert row is not None
    assert row.utc_offset is None  # absence is absence (§2.7), not "UTC"
    assert row.tz_name is None
    assert row.day_key == "2026-06-01"  # created_at is 13:24Z; day still exact


# ---- workout ---------------------------------------------------------------


def test_workout_maps_fields_and_kj_to_kcal():
    wk = _load("workout_page1.json")["records"][0]  # real walking, Hawaii
    row = parse_workout(wk)
    assert row.sport_type == "walk"
    assert row.source_sport_raw == "walking"
    assert row.avg_hr_bpm == 105
    # 172.39502 kJ / 4.184 = 41.2 kcal
    assert row.kcal_total == 41.2
    assert row.utc_offset == "-10:00"  # offset column, not tz_name
    assert row.tz_name is None  # IANA-only; WHOOP gives none (§2.6)
    assert row.hr_zones_json is not None  # real field is "zone_durations" (plural)


def test_workout_swimming_maps_from_name():
    wk = _load("workout_page1.json")["records"][1]  # sport_id 33 = swimming
    row = parse_workout(wk)
    assert row.sport_type == "swim"  # id 33 was previously mis-mapped to 'other'
    assert row.source_sport_raw == "swimming"
    assert row.kcal_total == 530.0  # 2217.4487 kJ / 4.184
    assert row.day_key == "2026-06-02"


def test_workout_day_boundary_regression_timezone_jump():
    # REAL Hawaii walk: starts 06:11Z at -10:00 => local 2026-05-31 (prev day).
    wk = _load("workout_timezone_jump.json")
    row = parse_workout(wk)
    assert row.day_key == "2026-05-31"  # follows local offset, NOT UTC (2026-06-01)
    assert row.utc_offset == "-10:00"
    assert row.sport_type == "walk"


def test_workout_unscored_has_null_metrics():
    wk = {
        "id": "x",
        "start": "2026-07-10T12:00:00Z",
        "end": "2026-07-10T12:30:00Z",
        "timezone_offset": "-04:00",
        "sport_id": 33,
        "sport_name": "swimming",
        "score_state": "PENDING_SCORE",
        "score": None,
    }
    row = parse_workout(wk)
    assert row.duration_s == 1800
    assert row.strain is None
    assert row.kcal_total is None
    assert row.sport_type == "swim"
