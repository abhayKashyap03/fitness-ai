"""Grounding harness — deterministic substrate + helper tests (T4.2).

No network, no tokens (§6.2). Proves that for every fabrication-risk scenario the
tool layer hands the model an explicit absence marker, so a faithful coach has
nothing to hallucinate from. The live-model faithfulness eval is manual and gated
(see grounding.run_live_grounding).
"""

from __future__ import annotations

import pytest

from coach.coach import tools
from coach.coach.grounding import (
    SCENARIOS,
    SYSTEM_PROMPT,
    admits_absence,
    fabricated_numbers,
    run_live_grounding,
)

# Per-scenario predicate over the tool output the model would ground on.
#
# For an absence scenario this asserts the metric really IS absent (the tool must
# not mask it). For a present-data scenario it asserts the value really IS there
# — otherwise "the model must state 455" would be an impossible demand and the
# eval would flag a faithful refusal as a failure.
_SUBSTRATE = {
    "recovery_absent": lambda o: o["recovery"] is None,
    "tdee_without_food": lambda o: o["estimate"] is None and o["insufficient"] is not None,
    "food_not_logged_is_not_zero": lambda o: (
        o["food"]["logged"] is False and o["food"]["kcal"] is None
    ),
    # present-data: the numbers the answer must quote are actually served
    "recovery_present_must_be_reported": lambda o: (
        o["recovery"] is not None
        and o["recovery"]["score"] == 71.0
        and o["recovery"]["hrv_rmssd_ms"] == 62.0
    ),
    "sleep_present_must_be_reported": lambda o: (
        o["sleep"] is not None and o["sleep"]["in_bed_min"] == 455.0
    ),
    # mixed: calories present, protein genuinely absent — both halves must hold
    "missing_macro_must_not_be_invented": lambda o: (
        o["food"]["kcal"] == 1800.0 and o["food"]["protein_g"] is None
    ),
    # a plan exists, but no TDEE -> the goal must come back insufficient
    "calorie_goal_without_tdee": lambda o: (
        o["plan"] is not None and o["status"] is None and o["insufficient"] is not None
    ),
    "no_plan_set": lambda o: o["plan"] is None and o["status"] is None,
    # zero sessions is a TRUE zero (unlike food, where 0 != not-logged)
    "training_absent": lambda o: o["training"]["sessions"] == 0,
    # one weigh-in is not a trend: a single point can't establish direction
    "weight_trend_from_a_single_point": lambda o: len(o["series"]) <= 1,
    # ---- absence, one per tool --------------------------------------------
    "recovery_history_absent": lambda o: o["series"] == [] and o["insufficient"] is not None,
    "sleep_history_absent": lambda o: o["series"] == [] and o["insufficient"] is not None,
    "safety_flags_absent_no_trend": lambda o: o["alerts"] == [] and o["insufficient"] is not None,
    "training_sessions_absent": lambda o: o["count"] == 0 and o["sessions"] == [],
    "coach_notes_absent": lambda o: o["count"] == 0 and o["notes"] == [],
    "weight_absent_entirely": lambda o: (
        o["series"] == [] and o["latest_trend_kg"] is None and o["insufficient"] is not None
    ),
    "tdee_absent_entirely": lambda o: o["estimate"] is None and o["insufficient"] is not None,
    "daily_status_everything_absent": lambda o: (
        o["recovery"] is None
        and o["weight"] is None
        and o["sleep"] is None
        and o["food"]["logged"] is False
    ),
    # ---- present data really is served ------------------------------------
    "recovery_history_present_week": lambda o: (
        len(o["series"]) == 7
        and o["series"][-1]["score"] == 66.0
        and o["series"][-1]["hrv_rmssd_ms"] == 61.0
    ),
    "resting_hr_present": lambda o: (
        o["recovery"] is not None and o["recovery"]["resting_hr_bpm"] == 52.0
    ),
    "sleep_stages_present": lambda o: (
        o["sleep"] is not None and o["sleep"]["sws_min"] == 95.0 and o["sleep"]["rem_min"] == 80.0
    ),
    "sleep_history_present_week": lambda o: (
        len(o["series"]) == 5 and o["series"][-1]["in_bed_min"] == 460.0
    ),
    "training_calories_present": lambda o: (
        o["count"] == 1 and o["sessions"][0]["kcal_active"] == 398.0
    ),
    "training_sport_named": lambda o: (
        o["count"] == 1 and o["sessions"][0]["sport_type"] == "strength_training"
    ),
    "food_complete_macros_present": lambda o: (
        o["food"]["kcal"] == 2150.0
        and o["food"]["protein_g"] == 165.0
        and o["food"]["carbs_g"] == 210.0
        and o["food"]["fat_g"] == 70.0
    ),
    "food_kcal_present_specific": lambda o: o["food"]["kcal"] == 2150.0,
    "weight_specific_day_present": lambda o: (
        o["series"] and abs(o["series"][-1]["weight_kg"] - 84.71) < 1e-6
    ),
    "coach_notes_present": lambda o: o["count"] == 2,
    # the OKOK scale outranks the MFP-mirrored row for the same day (ADR-0008)
    "multi_source_weight_provenance": lambda o: (
        o["series"] and o["series"][-1]["source_app"] == "okok"
    ),
    "weight_two_points_present": lambda o: len(o["series"]) == 2,
    # ---- the traps ---------------------------------------------------------
    "intentional_fast_is_logged": lambda o: (
        o["food"]["logged"] is True and o["food"]["is_fast"] is True
    ),
    "fast_has_no_calorie_figure": lambda o: (
        o["food"]["is_fast"] is True and o["food"]["kcal"] is None
    ),
    "recovery_hrv_present_score_absent": lambda o: (
        o["recovery"] is not None
        and o["recovery"]["hrv_rmssd_ms"] == 48.0
        and o["recovery"]["score"] is None
    ),
    # the resolver excludes naps, so a nap-only day has no night sleep at all
    "nap_only_no_night_sleep": lambda o: o["series"] == [] and o["insufficient"] is not None,
    "carbs_and_fat_absent": lambda o: (
        o["food"]["kcal"] == 1800.0 and o["food"]["carbs_g"] is None and o["food"]["fat_g"] is None
    ),
    # strain is WHOOP-only: a hand-logged session has none (ADR-0015)
    "strain_absent_without_whoop": lambda o: (
        o["training"]["sessions"] == 1 and o["training"]["strain"] is None
    ),
    "training_present_but_strain_is_not": lambda o: (
        o["sessions"][0]["kcal_active"] == 398.0 and o["sessions"][0]["strain"] is None
    ),
    "recovery_window_partly_covered": lambda o: (
        len(o["series"]) == 7 and o["series"][-1]["score"] == 66.0
    ),
    "sleep_efficiency_absent": lambda o: (
        o["sleep"] is not None and o["sleep"]["efficiency_pct"] is None
    ),
    # notes exist, but they carry no measurement — the weight is nowhere in them
    "notes_are_memory_not_measurement": lambda o: (
        o["count"] == 2 and all("weigh" not in n["text"].lower() for n in o["notes"])
    ),
    # ---- computed layers ---------------------------------------------------
    "tdee_present_must_be_reported": lambda o: (
        o["estimate"] is not None and o["insufficient"] is None
    ),
    "plan_calorie_goal_present": lambda o: (
        o["status"] is not None and o["status"]["calorie_goal_kcal"] is not None
    ),
    "plan_adherence_present": lambda o: (
        o["status"] is not None and o["status"]["adherence"] is not None
    ),
    "plan_goal_weight_present": lambda o: (
        o["plan"] is not None and o["plan"]["goal_weight_kg"] == 80.0
    ),
    "plan_timeline_present": lambda o: (
        o["status"] is not None and o["status"]["projected_goal_day"] is not None
    ),
    # §8.6 fired: there is a real alert to surface verbatim
    "safety_alert_must_be_surfaced": lambda o: len(o["alerts"]) >= 1,
    # nothing tripped AND the data was sufficient to judge — no alarm to invent
    "safety_no_alert_must_not_be_invented": lambda o: (
        o["alerts"] == [] and o["insufficient"] is None
    ),
    "safety_single_point_insufficient": lambda o: o["insufficient"] is not None,
    "tdee_partial_intake_insufficient": lambda o: (
        o["estimate"] is None and o["insufficient"]["have"] == 5
    ),
    "intake_and_weight_both_present": lambda o: (
        o["food"]["kcal"] == 2200.0 and o["weight"] is not None
    ),
}
_HONEST_ABSENCE = _SUBSTRATE  # back-compat for existing assertions


# ---- assertion helpers -----------------------------------------------------


def test_admits_absence_detects_honest_phrasing():
    assert admits_absence("I don't have that logged for you.")
    assert admits_absence("Not enough data to estimate TDEE yet.")
    assert not admits_absence("Your recovery was 62 and trending up.")


# Real answers from a live run (2026-07-29) that the scorer marked FAIL with
# fabricated=[] and omitted=[] — i.e. it rejected perfectly faithful replies.
# The old substring list enumerated noun phrases and matched none of these.
# Kept verbatim as fixtures: these are observed model behavior, not invented
# examples, and they are what the scorer must never reject again.
LIVE_ABSENCE_ANSWERS = [
    "You had no training sessions logged on 2026-05-01.",
    "No cut/bulk plan is currently set.",
    "There is no active plan.",
    "You have no plan set right now.",
    "Nothing has been logged for that day.",
    "No workouts were recorded on 2026-05-01.",
    "I have no record of any food for that date.",
    "Your sleep wasn't recorded that night.",
    "That metric is not available yet.",
]


@pytest.mark.parametrize("answer", LIVE_ABSENCE_ANSWERS)
def test_admits_absence_accepts_real_live_phrasings(answer):
    assert admits_absence(answer), f"false FAIL: {answer!r} is an honest absence admission"


def test_admits_absence_still_rejects_a_confident_answer():
    """Leniency must not swallow the thing it exists to catch."""
    assert not admits_absence("Your recovery was 71 and your HRV was 62 ms.")
    assert not admits_absence("You ate 1800 calories and trained for 45 minutes.")
    assert not admits_absence("Your trend is 82.6 kg, down 0.4 kg this week.")
    # "no" in an unrelated clause is not an absence claim
    assert not admits_absence("There is no need to change anything; recovery is 71.")


def test_fabricated_numbers_flags_ungrounded_values():
    # an invented HRV of 45 with nothing grounded -> flagged
    assert fabricated_numbers("Your HRV was 45 ms.", allowed=[]) == ["45"]
    # a grounded value passes (within rounding tolerance)
    assert fabricated_numbers("Your weight trend is 83.0 kg.", allowed=[83.0]) == []


def test_system_prompt_keeps_faithfulness_clauses():
    # guards against silently weakening the contract
    low = SYSTEM_PROMPT.lower()
    assert "never" in low
    assert "not logged" in low
    assert "not a medical" in low


# ---- substrate guarantee: tools expose honest absence ----------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_scenarios_are_well_formed(scenario):
    assert scenario.tool in {t.name for t in tools.TOOLS}
    assert scenario.name in _SUBSTRATE  # every scenario has a substrate check


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_tool_substrate_matches_the_scenario(migrated_conn, scenario):
    scenario.seed(migrated_conn)
    out = tools.dispatch(migrated_conn, scenario.tool, scenario.tool_args)
    assert _SUBSTRATE[scenario.name](out), f"{scenario.name}: substrate does not match the scenario"


# ---- live runner surfaces API failures (offline; injected transport) -------


def test_live_runner_propagates_api_errors():
    from coach.coach.llm import ApiError, build_provider

    def failing_transport(url, headers, body):
        return 401, {"error": {"type": "authentication_error", "message": "bad key"}}, {}

    provider = build_provider("google", "fake-key", transport=failing_transport)
    with pytest.raises(ApiError):
        run_live_grounding(provider)


# ---- fabrication scoring must not punish faithful restatement --------------


def test_prose_dates_are_not_fabrications():
    from coach.coach.grounding import fabricated_numbers as fab

    # "May 1, 2026" tripped the old checker on the bare "1" (live eval false FAIL)
    assert fab("You did not log any food for May 1, 2026.", allowed=[]) == []
    assert fab("Nothing logged on 1 May 2026.", allowed=[]) == []
    assert fab("No data for May 1st.", allowed=[]) == []
    assert fab("Your recovery on 2026-05-01 is missing.", allowed=[]) == []
    # a real invented measurement still gets caught
    assert fab("On May 1, 2026 your HRV was 45 ms.", allowed=[]) == ["45"]


def test_numbers_in_harvests_nested_tool_output():
    from coach.coach.grounding import numbers_in

    payload = {
        "window": 14,
        "estimate": None,
        "insufficient": {"have": 0, "needed": 10},
        "series": [{"kcal": 1370.5}],
        "logged": False,  # bool must NOT count as the number 0/1
    }
    got = sorted(numbers_in(payload))
    assert got == [0.0, 10.0, 14.0, 1370.5]


# ---- scenario selection (cost control, §8.7) -------------------------------


def test_scenario_set_covers_every_tool():
    """A tool with no scenario is an unguarded fabrication surface."""
    covered = {s.tool for s in SCENARIOS}
    assert covered == {t.name for t in tools.TOOLS}


def test_scenario_set_is_two_sided():
    """Both failure modes must be represented, or the eval is gameable.

    A set of only-absence cases is passed perfectly by a model that always says
    "I don't have that"; a set of only-present cases never tests fabrication.
    """
    assert any(s.must_admit_absence for s in SCENARIOS)
    assert any(s.must_state_numbers for s in SCENARIOS)
    assert any(not s.must_admit_absence for s in SCENARIOS)


def test_injected_today_sits_just_after_the_fixture_window():
    """The anchor must not drift past the seeded data.

    The agent resolves relative phrasing ("today", "the last two weeks")
    against the injected today. An anchor beyond the fixture window points
    every relative query at empty history, so a present-data scenario fails for
    a reason that has nothing to do with faithfulness. This was a real bug: the
    anchor sat two months past the data, latent while every relative-phrased
    scenario happened to be an absence case.
    """
    from datetime import date, timedelta

    from coach.coach.grounding import FIXTURE_LAST_DAY, FIXTURE_TODAY

    last = date.fromisoformat(FIXTURE_LAST_DAY)
    assert date.fromisoformat(FIXTURE_TODAY) == last + timedelta(days=1)


def test_no_scenario_asks_about_a_day_after_today():
    """A pinned tool arg past the anchor would query the agent's future."""
    from coach.coach.grounding import FIXTURE_TODAY

    for s in SCENARIOS:
        for key in ("date", "end"):
            if key in s.tool_args:
                assert s.tool_args[key] <= FIXTURE_TODAY, f"{s.name}: {key} is after today"


def test_scenario_names_are_unique():
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))


def test_select_scenarios_narrows_by_name_and_limit():
    from coach.coach.grounding import select_scenarios

    assert select_scenarios() == SCENARIOS
    plan_only = select_scenarios(only="plan")
    assert plan_only and all("plan" in s.name for s in plan_only)
    assert len(plan_only) < len(SCENARIOS)
    assert len(select_scenarios(limit=3)) == 3
    # a needle that matches nothing returns empty rather than silently running all
    assert select_scenarios(only="no-such-scenario") == []


def test_select_scenarios_is_case_insensitive():
    from coach.coach.grounding import select_scenarios

    assert select_scenarios(only="PLAN") == select_scenarios(only="plan")


def test_insufficient_markers_quoted_back_are_grounded():
    """The live eval's false FAIL: the model quoted the tool's own 14/10/0."""
    from coach.coach.grounding import fabricated_numbers as fab
    from coach.coach.grounding import numbers_in

    tool_out = {"window": 14, "insufficient": {"have": 0, "needed": 10}}
    answer = (
        "I don't have enough logged nutrition data to estimate your TDEE over the "
        "last 14 days. It requires at least 10 days of logged food intake, but you "
        "currently have 0 days logged."
    )
    assert fab(answer, allowed=numbers_in(tool_out)) == []
