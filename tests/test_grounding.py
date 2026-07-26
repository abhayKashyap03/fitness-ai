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

# per-scenario predicate: is the metric the query asks about honestly ABSENT
# in the tool output the model would ground on?
_HONEST_ABSENCE = {
    "recovery_absent": lambda o: o["recovery"] is None,
    "tdee_without_food": lambda o: o["estimate"] is None and o["insufficient"] is not None,
    "food_not_logged_is_not_zero": lambda o: o["food"]["logged"] is False
    and o["food"]["kcal"] is None,
}


# ---- assertion helpers -----------------------------------------------------


def test_admits_absence_detects_honest_phrasing():
    assert admits_absence("I don't have that logged for you.")
    assert admits_absence("Not enough data to estimate TDEE yet.")
    assert not admits_absence("Your recovery was 62 and trending up.")


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
    assert scenario.name in _HONEST_ABSENCE  # every scenario has a checker


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_tool_substrate_is_honestly_absent(migrated_conn, scenario):
    scenario.seed(migrated_conn)
    out = tools.dispatch(migrated_conn, scenario.tool, scenario.tool_args)
    assert _HONEST_ABSENCE[scenario.name](out), f"{scenario.name}: tool masked the absence"


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
