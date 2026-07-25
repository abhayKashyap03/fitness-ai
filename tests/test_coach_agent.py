"""Provider-agnostic LLM layer + agent loop — fake transport, zero network (§6.2).

The load-bearing test here is ``test_ask_*`` parametrized across BOTH providers:
the agent loop must behave identically whichever vendor is configured, which is
the whole point of the canonical shape (§2.5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coach.coach.agent import ask
from coach.coach.grounding import fabricated_numbers, run_live_grounding
from coach.coach.llm import AnthropicProvider, ApiError, GoogleProvider, build_provider

WEIGH_DAY = "2026-01-02"


# ---- fake transport --------------------------------------------------------


class FakeTransport:
    """Scripted (status, body) responses; records every request body."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.urls: list[str] = []
        self.headers: list[dict] = []

    def __call__(self, url, headers, body):
        self.urls.append(url)
        self.headers.append(headers)
        self.requests.append(json.loads(body))
        status, payload = self.responses.pop(0)
        return status, payload, {}


# ---- per-provider response scripts (same intent, different wire shape) -----


class AnthropicScript:
    """Builds Anthropic-shaped responses and providers."""

    provider_cls = AnthropicProvider

    @staticmethod
    def build(transport):
        return AnthropicProvider("k", transport=transport, sleep=lambda _s: None)

    @staticmethod
    def text(t):
        return (
            200,
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": t}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    @staticmethod
    def tool(name, args):
        return (
            200,
            {
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "id": "t1", "name": name, "input": args}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    @staticmethod
    def refusal():
        return (200, {"stop_reason": "refusal", "content": [], "usage": {}})

    @staticmethod
    def truncated(t):
        return (
            200,
            {
                "stop_reason": "max_tokens",
                "content": [{"type": "text", "text": t}],
                "usage": {},
            },
        )


class GoogleScript:
    """Builds Gemini-shaped responses and providers."""

    provider_cls = GoogleProvider

    @staticmethod
    def build(transport):
        return GoogleProvider("k", transport=transport, sleep=lambda _s: None)

    @staticmethod
    def _resp(finish, parts, usage=None):
        return (
            200,
            {
                "candidates": [
                    {"content": {"role": "model", "parts": parts}, "finishReason": finish}
                ],
                "usageMetadata": usage
                or {"promptTokenCount": 10, "candidatesTokenCount": 5},
            },
        )

    @classmethod
    def text(cls, t):
        return cls._resp("STOP", [{"text": t}])

    @classmethod
    def tool(cls, name, args):
        return cls._resp("STOP", [{"functionCall": {"name": name, "args": args}}])

    @classmethod
    def refusal(cls):
        return cls._resp("SAFETY", [])

    @classmethod
    def truncated(cls, t):
        return cls._resp("MAX_TOKENS", [{"text": t}])


SCRIPTS = [AnthropicScript, GoogleScript]
SCRIPT_IDS = ["anthropic", "google"]


@pytest.fixture
def seeded_conn(migrated_conn):
    from coach.adapters.healthkit.ingest import ingest_healthkit
    from coach.normalize.runner import normalize_all

    fix = Path(__file__).parent / "fixtures" / "healthkit" / "export_sample.xml"
    ingest_healthkit(migrated_conn, fix)
    normalize_all(migrated_conn)
    return migrated_conn


# ---- factory ---------------------------------------------------------------


def test_build_provider_defaults():
    assert build_provider("google", "k").model == "gemini-2.5-flash"
    assert build_provider("anthropic", "k").model == "claude-opus-4-8"


def test_build_provider_model_override():
    assert build_provider("google", "k", model="gemini-2.5-pro").model == "gemini-2.5-pro"


def test_build_provider_unknown_raises():
    with pytest.raises(ValueError, match="unknown LLM provider"):
        build_provider("openai", "k")


# ---- retry / error semantics (shared plumbing, per-provider statuses) ------


@pytest.mark.parametrize("script", SCRIPTS, ids=SCRIPT_IDS)
def test_retries_429_then_succeeds(script):
    tr = FakeTransport([(429, {"error": {"message": "slow down"}}), script.text("ok")])
    resp = script.build(tr).complete(system="s", turns=[], tools=[])
    assert resp.stop_reason == "end_turn"
    assert len(tr.requests) == 2


@pytest.mark.parametrize("script", SCRIPTS, ids=SCRIPT_IDS)
def test_400_raises_without_retry(script):
    tr = FakeTransport([(400, {"error": {"type": "invalid_request_error", "message": "bad"}})])
    with pytest.raises(ApiError) as exc:
        script.build(tr).complete(system="s", turns=[], tools=[])
    assert exc.value.status == 400
    assert len(tr.requests) == 1


@pytest.mark.parametrize("script", SCRIPTS, ids=SCRIPT_IDS)
def test_retries_exhausted_raises(script):
    tr = FakeTransport([(429, {"error": {"message": "x"}})] * 4)
    with pytest.raises(ApiError):
        script.build(tr).complete(system="s", turns=[], tools=[])


def test_error_message_never_contains_the_key():
    tr = FakeTransport([(401, {"error": {"type": "auth", "message": "bad key"}})])
    provider = AnthropicProvider("SUPER-SECRET-KEY", transport=tr, sleep=lambda _s: None)
    with pytest.raises(ApiError) as exc:
        provider.complete(system="s", turns=[], tools=[])
    assert "SUPER-SECRET-KEY" not in str(exc.value)  # §8.4


# ---- anthropic wire shape --------------------------------------------------


def test_anthropic_sends_cache_control():
    tr = FakeTransport([AnthropicScript.text("hi")])
    AnthropicScript.build(tr).complete(system="SYS", turns=[], tools=[])
    sys_block = tr.requests[0]["system"][0]
    assert sys_block["cache_control"] == {"type": "ephemeral"}
    assert sys_block["text"] == "SYS"


def test_anthropic_key_in_header_not_url():
    tr = FakeTransport([AnthropicScript.text("hi")])
    AnthropicProvider("secret", transport=tr).complete(system="s", turns=[], tools=[])
    assert tr.headers[0]["x-api-key"] == "secret"
    assert "secret" not in tr.urls[0]


# ---- google wire shape -----------------------------------------------------


def test_google_key_in_header_not_url():
    tr = FakeTransport([GoogleScript.text("hi")])
    GoogleProvider("secret", transport=tr).complete(system="s", turns=[], tools=[])
    assert tr.headers[0]["x-goog-api-key"] == "secret"
    assert "secret" not in tr.urls[0]  # never a query param
    assert "gemini-2.5-flash:generateContent" in tr.urls[0]


def test_google_system_instruction_and_roles():
    tr = FakeTransport([GoogleScript.text("hi")])
    from coach.coach.llm import AssistantTurn, UserTurn

    GoogleScript.build(tr).complete(
        system="SYS",
        turns=[UserTurn("q"), AssistantTurn([{"text": "a"}])],
        tools=[],
    )
    body = tr.requests[0]
    assert body["systemInstruction"]["parts"][0]["text"] == "SYS"
    assert [c["role"] for c in body["contents"]] == ["user", "model"]  # not "assistant"


def test_google_tool_schema_is_openapi_subset():
    """Types upper-cased; unsupported keywords (minimum) dropped, else 400."""
    tr = FakeTransport([GoogleScript.text("hi")])
    from coach.coach.tools import tool_specs

    GoogleScript.build(tr).complete(system="s", turns=[], tools=tool_specs())
    decls = tr.requests[0]["tools"][0]["functionDeclarations"]
    window_tool = next(d for d in decls if d["name"] == "get_weight_trend")
    params = window_tool["parameters"]
    assert params["type"] == "OBJECT"
    assert params["properties"]["end"]["type"] == "STRING"
    assert params["properties"]["window"]["type"] == "INTEGER"
    assert "minimum" not in params["properties"]["window"]  # stripped


def test_google_usage_splits_cached_from_input():
    tr = FakeTransport(
        [
            GoogleScript._resp(
                "STOP",
                [{"text": "hi"}],
                usage={
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 20,
                    "cachedContentTokenCount": 40,
                },
            )
        ]
    )
    resp = GoogleScript.build(tr).complete(system="s", turns=[], tools=[])
    assert resp.usage.input_tokens == 60  # prompt total minus cached
    assert resp.usage.cached_input_tokens == 40
    assert resp.usage.output_tokens == 20


def test_google_blocked_prompt_without_candidates_is_refusal():
    tr = FakeTransport([(200, {"promptFeedback": {"blockReason": "SAFETY"}})])
    resp = GoogleScript.build(tr).complete(system="s", turns=[], tools=[])
    assert resp.stop_reason == "refusal"


# ---- agent loop: identical behavior on every provider ----------------------


@pytest.mark.parametrize("script", SCRIPTS, ids=SCRIPT_IDS)
def test_ask_text_only(seeded_conn, script):
    tr = FakeTransport([script.text("You're doing fine.")])
    res = ask(seeded_conn, script.build(tr), "how am I doing?")
    assert res.text == "You're doing fine."
    assert res.rounds == 1 and not res.tool_calls


@pytest.mark.parametrize("script", SCRIPTS, ids=SCRIPT_IDS)
def test_ask_tool_round_executes_and_feeds_back(seeded_conn, script):
    tr = FakeTransport(
        [script.tool("get_daily_status", {"date": WEIGH_DAY}), script.text("Weight logged.")]
    )
    res = ask(seeded_conn, script.build(tr), "status?")
    assert res.text == "Weight logged."
    assert [c.name for c in res.tool_calls] == ["get_daily_status"]
    assert res.tool_calls[0].ok

    # the second request carried REAL tool output back to the model
    sent = json.dumps(tr.requests[1])
    assert "healthkit" in sent  # the weigh-in provenance made the round trip


@pytest.mark.parametrize("script", SCRIPTS, ids=SCRIPT_IDS)
def test_ask_unknown_tool_is_error_not_crash(seeded_conn, script):
    tr = FakeTransport([script.tool("get_everything", {}), script.text("No such tool.")])
    res = ask(seeded_conn, script.build(tr), "?")
    assert not res.tool_calls[0].ok
    assert "tool error" in json.dumps(tr.requests[1])


@pytest.mark.parametrize("script", SCRIPTS, ids=SCRIPT_IDS)
def test_ask_round_bound_stops(seeded_conn, script):
    looping = script.tool("get_daily_status", {"date": WEIGH_DAY})
    tr = FakeTransport([looping] * 3)
    res = ask(seeded_conn, script.build(tr), "?", max_rounds=3)
    assert res.stopped_early and res.rounds == 3


@pytest.mark.parametrize("script", SCRIPTS, ids=SCRIPT_IDS)
def test_ask_refusal_surfaces(seeded_conn, script):
    tr = FakeTransport([script.refusal()])
    res = ask(seeded_conn, script.build(tr), "?")
    assert "declined" in res.text


@pytest.mark.parametrize("script", SCRIPTS, ids=SCRIPT_IDS)
def test_ask_max_tokens_notes_truncation(seeded_conn, script):
    tr = FakeTransport([script.truncated("partial")])
    res = ask(seeded_conn, script.build(tr), "?")
    assert "truncated" in res.text


@pytest.mark.parametrize("script", SCRIPTS, ids=SCRIPT_IDS)
def test_usage_accumulates_across_rounds(seeded_conn, script):
    tr = FakeTransport(
        [script.tool("get_daily_status", {"date": WEIGH_DAY}), script.text("done")]
    )
    res = ask(seeded_conn, script.build(tr), "?")
    assert res.usage.input_tokens == 20 and res.usage.output_tokens == 10


def test_anthropic_tool_results_share_one_user_message(seeded_conn):
    """Splitting parallel tool results across messages trains the model badly."""
    tr = FakeTransport(
        [
            (
                200,
                {
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "a",
                            "name": "get_daily_status",
                            "input": {"date": WEIGH_DAY},
                        },
                        {
                            "type": "tool_use",
                            "id": "b",
                            "name": "get_weight_trend",
                            "input": {"end": WEIGH_DAY},
                        },
                    ],
                    "usage": {},
                },
            ),
            AnthropicScript.text("done"),
        ]
    )
    ask(seeded_conn, AnthropicScript.build(tr), "?")
    last = tr.requests[1]["messages"][-1]
    assert last["role"] == "user"
    assert len(last["content"]) == 2  # both results, one message


def test_google_tool_results_match_by_name(seeded_conn):
    tr = FakeTransport(
        [GoogleScript.tool("get_daily_status", {"date": WEIGH_DAY}), GoogleScript.text("done")]
    )
    ask(seeded_conn, GoogleScript.build(tr), "?")
    last = tr.requests[1]["contents"][-1]
    fr = last["parts"][0]["functionResponse"]
    assert fr["name"] == "get_daily_status"  # Gemini matches by name, not id
    assert "result" in fr["response"]


# ---- grounding helpers + offline harness -----------------------------------


def test_fabricated_numbers_ignores_dates_and_years():
    assert fabricated_numbers("On 2026-05-01 I have no data for you.", []) == []
    assert fabricated_numbers("Back in 2024 you weighed 83 kg.", []) == ["83"]


@pytest.mark.parametrize("script", SCRIPTS, ids=SCRIPT_IDS)
def test_run_grounding_offline_faithful_passes(script):
    tr = FakeTransport([script.text("I don't have that logged for this day.")] * 3)
    results = run_live_grounding(script.build(tr))
    assert all(r["passed"] for r in results)


@pytest.mark.parametrize("script", SCRIPTS, ids=SCRIPT_IDS)
def test_run_grounding_offline_fabrication_fails(script):
    tr = FakeTransport([script.text("Your recovery was 62.")] * 3)
    results = run_live_grounding(script.build(tr))
    assert not any(r["passed"] for r in results)


# ---- CLI surface -----------------------------------------------------------


def test_parser_accepts_ask_and_eval():
    from coach.cli.main import build_parser

    p = build_parser()
    a = p.parse_args(["ask", "how am I doing?", "--show-tools"])
    assert a.question and a.show_tools
    assert p.parse_args(["eval", "grounding"]).func
