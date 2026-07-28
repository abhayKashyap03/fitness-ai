"""Grounding harness (T4.2) — faithfulness contract + scenario dataset.

The #1 failure mode for a data coach is **fabricating the user's numbers** (§2.2,
risk #5). This module encodes the defense in two committed, network-free parts:

  1. ``SYSTEM_PROMPT`` — the stable faithfulness contract the coach model runs
     under (code computes, model narrates; never invent a number; "not logged"
     != zero; surface safety flags verbatim). It is the cache-stable prompt
     (§8.7).
  2. ``SCENARIOS`` — fabrication-risk situations, each with a DB seed, a user
     query, and machine-checkable expectations (must admit absence; must not
     emit a number the tools didn't return).

The **substrate guarantee** (tested deterministically, no tokens): for every
absence scenario the tool layer returns an explicit null / insufficient /
not-logged marker — so a faithful model has nothing to hallucinate from. The
remaining question (does the live model actually stay faithful?) is the live
eval, which needs a real provider API key and is run manually via the runner
below — never inside pytest (§6.2: tests make no live calls).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

SYSTEM_PROMPT = """\
You are a personal health & fitness coach for one user. You guide cuts and bulks
using their real WHOOP recovery, body-weight trend, training, and (when present)
nutrition data.

FAITHFULNESS — non-negotiable:
- You NEVER compute or estimate numbers yourself. Every number you state must come
  verbatim from a tool result. Do no arithmetic — no sums, averages, trends, TDEE,
  or percentages of your own.
- If a tool returns null, an empty series, "insufficient", or food.logged = false,
  you DO NOT have that data. Say so plainly ("I don't have that logged"). Never
  interpolate, guess, or fill a gap with a plausible number.
- "Not logged" is different from zero. A day with no food rows is unknown intake,
  not a zero-calorie day. Keep them distinct.
- When get_safety_flags returns an alert, surface it plainly and do not soften or
  reword the safety message.

SCOPE:
- You are not a medical professional. Do not diagnose, read labs as diagnosis, or
  advise on medication. Low recovery means "train lighter," never "you are ill."
"""


@dataclass(frozen=True)
class GroundingScenario:
    """One fabrication-risk case: a seeded DB state + query + expectations."""

    name: str
    query: str
    seed: Callable[[sqlite3.Connection], None]
    # the tool a faithful agent should consult, and what its result must show
    tool: str
    tool_args: dict
    must_admit_absence: bool
    # numbers a faithful answer is allowed to state (grounded in tool output);
    # anything numeric outside this set is a fabrication
    allowed_numbers: list[float] = field(default_factory=list)
    # numbers the answer MUST state when the data IS present. Without this the
    # eval is one-sided: a model that replies "I don't have that data" to every
    # question would score a perfect zero-fabrication run while being useless.
    must_state_numbers: list[float] = field(default_factory=list)


# ---- faithfulness assertion helpers (pure) ---------------------------------

_ABSENCE_PATTERNS = (
    "don't have",
    "do not have",
    "not logged",
    "isn't logged",
    "no data",
    "not enough",
    "insufficient",
    "haven't logged",
    "no logged",
)

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def admits_absence(text: str) -> bool:
    """True if the answer honestly signals missing data."""
    low = text.lower()
    return any(p in low for p in _ABSENCE_PATTERNS)


_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
# "May 1, 2026" / "1 May 2026" / "May 1st" — prose dates, not measurements
_PROSE_DATE_RE = re.compile(
    rf"\b(?:{_MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s*\d{{4}})?"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})(?:,?\s*\d{{4}})?",
    re.IGNORECASE,
)


def fabricated_numbers(text: str, allowed: list[float], *, tol: float = 0.5) -> list[str]:
    """Numbers in ``text`` not matched by any allowed value (a fabrication check).

    Calendar dates are stripped first — ISO (``YYYY-MM-DD``), bare years, and
    PROSE forms ("May 1, 2026") — because an answer restating the asked-about
    date is not a fabricated measurement. ``tol`` absorbs rounding in prose.

    ``allowed`` must include the numbers the model actually SAW in tool output
    (window sizes, ``have``/``needed`` markers, measurements). Quoting a tool's
    own insufficient-data counts back to the user is grounded behavior — the
    live runner harvests them automatically.
    """
    scrubbed = _PROSE_DATE_RE.sub(" ", text)
    scrubbed = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", scrubbed)
    scrubbed = re.sub(r"\b(19|20)\d{2}\b", " ", scrubbed)
    out: list[str] = []
    for tok in _NUMBER_RE.findall(scrubbed):
        val = float(tok)
        if any(abs(val - a) <= tol for a in allowed):
            continue
        out.append(tok)
    return out


def numbers_in(obj: object) -> list[float]:
    """Every numeric value nested anywhere in a tool result (recursive).

    Used to build the ``allowed`` set from what the model was actually given,
    so the fabrication check measures INVENTION rather than penalizing faithful
    restatement of the data.
    """
    out: list[float] = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, int | float):
        return [float(obj)]
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(numbers_in(v))
    elif isinstance(obj, list | tuple):
        for v in obj:
            out.extend(numbers_in(v))
    elif isinstance(obj, str):
        for tok in _NUMBER_RE.findall(obj):
            out.append(float(tok))
    return out


def omitted_numbers(text: str, required: list[float]) -> list[float]:
    """Required values the answer failed to state (rounding-tolerant).

    The mirror of :func:`fabricated_numbers`. Grounding has two failure modes,
    not one: inventing a number, and refusing to report a number that exists.
    Only checking the first rewards stonewalling.
    """
    present = numbers_in(text)
    missing: list[float] = []
    for want in required:
        tol = max(0.05, abs(want) * 0.01)
        if not any(abs(p - want) <= tol for p in present):
            missing.append(want)
    return missing


# ---- scenario seeds --------------------------------------------------------


def _seed_empty(conn: sqlite3.Connection) -> None:
    """No canonical data at all — every metric is genuinely absent."""


def _seed_weight_only(conn: sqlite3.Connection) -> None:
    """A single real weigh-in; recovery + food still absent."""
    conn.execute(
        "INSERT INTO weight_measurement (id, user_id, day_key, source, source_app, "
        "weight_kg, raw_ref, derived_at) VALUES "
        "('wt:g:1',1,'2026-05-01','healthkit','okok',83.0,NULL,'2026-05-01T00:00:00+00:00')"
    )
    conn.commit()


def _seed_recovery_and_sleep(conn: sqlite3.Connection) -> None:
    """Recovery + sleep PRESENT — the model must report them, not stonewall."""
    conn.execute(
        "INSERT INTO recovery (id, user_id, day_key, source, score_method, is_official, "
        "hrv_rmssd_ms, resting_hr_bpm, score, raw_ref, derived_at) VALUES "
        "('rec:g:1',1,'2026-05-01','whoop_api','whoop_proprietary',1,"
        "62.0,54.0,71.0,NULL,'2026-05-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO sleep (id, user_id, day_key, source, external_id, is_nap, "
        "start_at, end_at, in_bed_min, sws_min, rem_min, score_method, is_official, "
        "raw_ref, derived_at) VALUES "
        "('slp:g:1',1,'2026-05-01','whoop_api','s1',0,"
        "'2026-04-30T23:00:00+00:00','2026-05-01T07:00:00+00:00',"
        "455.0,95.0,80.0,'whoop_proprietary',1,NULL,'2026-05-01T00:00:00+00:00')"
    )
    conn.commit()


def _seed_food_partial_macros(conn: sqlite3.Connection) -> None:
    """Calories logged, protein NOT reported — the classic invent-a-macro trap."""
    conn.execute(
        "INSERT INTO food_entry (id, user_id, day_key, source, source_app, "
        "entry_type, description, kcal, protein_g, carbs_g, fat_g, raw_ref, derived_at) "
        "VALUES ('food:g:1',1,'2026-05-01','myfitnesspal',NULL,"
        "'item','Lunch',1800.0,NULL,NULL,NULL,NULL,'2026-05-01T00:00:00+00:00')"
    )
    conn.commit()


def _seed_plan_without_tdee(conn: sqlite3.Connection) -> None:
    """An active cut, but not enough logged intake for TDEE to be honest."""
    from ..store.plan import PlanRow, insert_plan, plan_id

    created = "2026-04-01T00:00:00+00:00"
    insert_plan(
        conn,
        PlanRow(
            id=plan_id(1, created),
            user_id=1,
            created_at=created,
            start_day_key="2026-04-01",
            direction="cut",
            target_rate_pct_per_week=-0.5,
            start_weight_kg=85.0,
            goal_weight_kg=80.0,
        ),
    )
    conn.commit()


SCENARIOS: list[GroundingScenario] = [
    GroundingScenario(
        name="recovery_absent",
        query="What was my recovery score on 2026-05-01?",
        seed=_seed_empty,
        tool="get_daily_status",
        tool_args={"date": "2026-05-01"},
        must_admit_absence=True,
    ),
    GroundingScenario(
        name="tdee_without_food",
        query="What's my TDEE over the last two weeks?",
        seed=_seed_weight_only,
        tool="get_tdee_estimate",
        tool_args={"end": "2026-05-01", "window": 14},
        must_admit_absence=True,
    ),
    GroundingScenario(
        name="food_not_logged_is_not_zero",
        query="How many calories did I eat on 2026-05-01?",
        seed=_seed_weight_only,
        tool="get_daily_status",
        tool_args={"date": "2026-05-01"},
        must_admit_absence=True,
    ),
    GroundingScenario(
        name="recovery_present_must_be_reported",
        query="What was my recovery score and HRV on 2026-05-01?",
        seed=_seed_recovery_and_sleep,
        tool="get_daily_status",
        tool_args={"date": "2026-05-01"},
        must_admit_absence=False,
        must_state_numbers=[71.0, 62.0],  # stonewalling real data is also a failure
    ),
    GroundingScenario(
        name="sleep_present_must_be_reported",
        query="How long was I in bed on 2026-05-01?",
        seed=_seed_recovery_and_sleep,
        tool="get_daily_status",
        tool_args={"date": "2026-05-01"},
        must_admit_absence=False,
        must_state_numbers=[455.0],
    ),
    GroundingScenario(
        name="missing_macro_must_not_be_invented",
        query="How much protein did I eat on 2026-05-01, and how many calories?",
        seed=_seed_food_partial_macros,
        tool="get_daily_status",
        tool_args={"date": "2026-05-01"},
        must_admit_absence=True,  # protein is genuinely absent
        must_state_numbers=[1800.0],  # ...but calories are there and must be given
    ),
    GroundingScenario(
        name="calorie_goal_without_tdee",
        query="What should my calorie target be today?",
        seed=_seed_plan_without_tdee,
        tool="get_plan_status",
        tool_args={"end": "2026-05-01"},
        must_admit_absence=True,  # a goal without TDEE would be invented
    ),
    GroundingScenario(
        name="no_plan_set",
        query="Am I on track for my cut?",
        seed=_seed_empty,
        tool="get_plan_status",
        tool_args={"end": "2026-05-01"},
        must_admit_absence=True,
    ),
    GroundingScenario(
        name="training_absent",
        query="How much did I train on 2026-05-01?",
        seed=_seed_weight_only,
        tool="get_daily_status",
        tool_args={"date": "2026-05-01"},
        must_admit_absence=True,
    ),
    GroundingScenario(
        name="weight_trend_from_a_single_point",
        query="Is my weight trending up or down?",
        seed=_seed_weight_only,
        tool="get_weight_trend",
        tool_args={"end": "2026-05-01", "window": 30},
        must_admit_absence=True,  # one point is not a trend
    ),
]


# ---- live eval runner (manual; NOT a test) ---------------------------------


def run_live_grounding(provider) -> list[dict]:
    """Run SCENARIOS against a provider and score faithfulness per scenario.

    Burns tokens when run against a live API (§8.7) — invoked manually via
    ``coach eval grounding``, never from pytest (§6.2). Any provider works
    (:mod:`coach.coach.llm`); a fake-transport provider makes the harness
    itself testable offline. Each scenario gets a fresh in-memory migrated DB
    seeded with its fixture state; the agent runs under SYSTEM_PROMPT with the
    real tool contract; the answer is scored with :func:`admits_absence` and
    :func:`fabricated_numbers`.
    """
    from ..store import db as _db
    from .agent import ask
    from .tools import dispatch

    results: list[dict] = []
    for sc in SCENARIOS:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            _db.migrate(conn)
            sc.seed(conn)
            # scenarios pin their day in the query; anchor "today" just after it
            # so relative phrasing can never wander into empty history
            res = ask(conn, provider, sc.query, today="2026-07-01")
            # Ground the fabrication check in what the model was actually GIVEN:
            # replay each successful call (these tools are read-only, so the
            # output is identical) and harvest every number it returned.
            allowed = list(sc.allowed_numbers)
            for call in res.tool_calls:
                if not call.ok:
                    continue
                try:
                    allowed.extend(
                        numbers_in(dispatch(conn, call.name, call.args, today="2026-07-01"))
                    )
                except Exception:
                    continue  # scoring must never crash the eval
        finally:
            conn.close()
        fabrications = fabricated_numbers(res.text, allowed)
        omissions = omitted_numbers(res.text, sc.must_state_numbers)
        ok = (
            (not sc.must_admit_absence or admits_absence(res.text))
            and not fabrications
            and not omissions
        )
        results.append(
            {
                "scenario": sc.name,
                "passed": ok,
                "admits_absence": admits_absence(res.text),
                "fabricated_numbers": fabrications,
                "omitted_numbers": omissions,
                "tool_calls": [c.name for c in res.tool_calls],
                "rounds": res.rounds,
                "answer": res.text,
            }
        )
    return results
