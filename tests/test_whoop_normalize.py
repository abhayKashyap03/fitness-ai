"""Pure WHOOP normalizers, against synthetic fixtures + edge cases."""

from __future__ import annotations

import json
from pathlib import Path

from coach.adapters.whoop.sport_map import whoop_sport_to_canonical
from coach.normalize.whoop import parse_recovery, parse_workout

FIX = Path(__file__).parent / "fixtures" / "whoop"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


# ---- sport map -------------------------------------------------------------


def test_sport_map_known_and_unknown():
    assert whoop_sport_to_canonical(0) == "run"
    assert whoop_sport_to_canonical(45) == "strength"
    assert whoop_sport_to_canonical(999999) == "other"  # never dropped
    assert whoop_sport_to_canonical(None) == "other"


# ---- recovery --------------------------------------------------------------


def test_recovery_scored_maps_objective_and_score():
    rec = _load("recovery_page1.json")["records"][0]
    row = parse_recovery(rec, tz_offset="-04:00")
    assert row is not None
    assert row.hrv_rmssd_ms == 48.3
    assert row.resting_hr_bpm == 52
    assert row.score == 66
    assert row.is_official == 1
    assert row.score_method == "whoop_proprietary"
    assert row.day_key == "2026-07-10"
    assert row.source == "whoop_api"


def test_recovery_pending_returns_none():
    rec = _load("recovery_page1.json")["records"][1]
    assert parse_recovery(rec, tz_offset="-04:00") is None


def test_recovery_missing_offset_falls_back_to_utc():
    rec = _load("recovery_page1.json")["records"][0]
    row = parse_recovery(rec, tz_offset=None)
    assert row is not None
    assert row.tz_name == "UTC"
    assert row.day_key == "2026-07-10"  # created_at is 11:20Z


# ---- workout ---------------------------------------------------------------


def test_workout_maps_fields_and_kj_to_kcal():
    wk = _load("workout_page1.json")["records"][0]
    row = parse_workout(wk)
    assert row.sport_type == "strength"
    assert row.source_sport_raw == "Weightlifting"
    assert row.duration_s == 45 * 60
    assert row.avg_hr_bpm == 118
    assert row.strain == 9.4
    # 1673.6 kJ / 4.184 = 400.0 kcal
    assert row.kcal_total == 400.0
    assert row.day_key == "2026-07-10"
    assert row.hr_zones_json is not None


def test_workout_day_boundary_uses_local_offset():
    wk = _load("workout_page1.json")["records"][1]
    row = parse_workout(wk)
    # start 02:30Z at -05:00 => 21:30 previous local day
    assert row.day_key == "2026-07-09"
    assert row.sport_type == "run"
    assert row.distance_m == 8046.7


def test_workout_unscored_has_null_metrics():
    wk = {
        "id": "x",
        "start": "2026-07-10T12:00:00Z",
        "end": "2026-07-10T12:30:00Z",
        "timezone_offset": "-04:00",
        "sport_id": 45,
        "score_state": "PENDING_SCORE",
        "score": None,
    }
    row = parse_workout(wk)
    assert row.duration_s == 1800
    assert row.strain is None
    assert row.kcal_total is None
    assert row.sport_type == "strength"
