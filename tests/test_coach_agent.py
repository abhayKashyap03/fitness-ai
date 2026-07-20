"""Coach agent loop + stdlib LLM client — fake transport, zero network (§6.2)."""

from __future__ import annotations

import json

import pytest

from coach.coach.agent import ask
from coach.coach.grounding import fabricated_numbers, run_live_grounding
from coach.coach.llm import AnthropicClient, ApiError

WEIGH_DAY = "2026-01-02"


# ---- fake transport --------------------------------------------------------


class FakeTransport:
    """Scripted (status, body) responses; records every request body."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, url, headers, body):
        self.requests.append(json.loads(body))
        status, payload = self.responses.pop(0)
        return status, payload, {}


def _msg(stop_reason, content, usage=None):
    return {
        "stop_reason": stop_reason,
        "content": content,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
    }


def _text(t):
    return {"type": "text", "text": t}


def _tool_use(tid, name, args):
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


def _client(transport, **kw):
    return AnthropicClient("test-key", transport=transport, sleep=lambda _s: None, **kw)


# ---- llm client ------------------------------------------------------------


def test_client_sends_cache_control_and_headers():
    tr = FakeTransport([(200, _msg("end_turn", [_text("hi")]))])
    client = _client(tr)
    client.create_message(model="m", system="SYS", messages=[{"role": "user", "content": "q"}])
    body = tr.requests[0]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["system"][0]["text"] == "SYS"


def test_client_retries_429_then_succeeds():
    tr = FakeTransport(
        [
            (429, {"error": {"type": "rate_limit_error", "message": "slow down"}}),
            (200, _msg("end_turn", [_text("ok")])),
        ]
    )
    resp = _client(tr).create_message(model="m", system="s", messages=[])
    assert resp.stop_reason == "end_turn"
    assert len(tr.requests) == 2


def test_client_400_raises_immediately():
    tr = FakeTransport([(400, {"error": {"type": "invalid_request_error", "message": "bad"}})])
    with pytest.raises(ApiError) as exc:
        _client(tr).create_message(model="m", system="s", messages=[])
    assert exc.value.status == 400
    assert len(tr.requests) == 1  # no retry on client errors


def test_client_retries_exhausted_raises():
    tr = FakeTransport([(529, {"error": {"type": "overloaded_error", "message": "x"}})] * 4)
    with pytest.raises(ApiError) as exc:
        _client(tr).create_message(model="m", system="s", messages=[])
    assert exc.value.status == 529


# ---- agent loop ------------------------------------------------------------


@pytest.fixture
def seeded_conn(migrated_conn):
    from pathlib import Path

    from coach.adapters.healthkit.ingest import ingest_healthkit
    from coach.normalize.runner import normalize_all

    fix = Path(__file__).parent / "fixtures" / "healthkit" / "export_sample.xml"
    ingest_healthkit(migrated_conn, fix)
    normalize_all(migrated_conn)
    return migrated_conn


def test_ask_text_only(seeded_conn):
    tr = FakeTransport([(200, _msg("end_turn", [_text("You're doing fine.")]))])
    res = ask(seeded_conn, _client(tr), "how am I doing?", model="m")
    assert res.text == "You're doing fine."
    assert res.rounds == 1 and not res.tool_calls


def test_ask_tool_round_executes_and_feeds_back(seeded_conn):
    tr = FakeTransport(
        [
            (200, _msg("tool_use", [_tool_use("t1", "get_daily_status", {"date": WEIGH_DAY})])),
            (200, _msg("end_turn", [_text("Weight logged.")])),
        ]
    )
    res = ask(seeded_conn, _client(tr), "status?", model="m")
    assert res.text == "Weight logged."
    assert [c.name for c in res.tool_calls] == ["get_daily_status"]
    assert res.tool_calls[0].ok
    # second request carried the tool_result with real data, in ONE user message
    followup = tr.requests[1]["messages"]
    tool_result_msg = followup[-1]
    assert tool_result_msg["role"] == "user"
    payload = json.loads(tool_result_msg["content"][0]["content"])
    assert payload["weight"]["source"] == "healthkit"


def test_ask_unknown_tool_is_error_not_crash(seeded_conn):
    tr = FakeTransport(
        [
            (200, _msg("tool_use", [_tool_use("t1", "get_everything", {})])),
            (200, _msg("end_turn", [_text("I don't have that tool.")])),
        ]
    )
    res = ask(seeded_conn, _client(tr), "?", model="m")
    assert not res.tool_calls[0].ok
    err_block = tr.requests[1]["messages"][-1]["content"][0]
    assert err_block["is_error"] is True


def test_ask_round_bound_stops(seeded_conn):
    looping = (200, _msg("tool_use", [_tool_use("t", "get_daily_status", {"date": WEIGH_DAY})]))
    tr = FakeTransport([looping] * 3)
    res = ask(seeded_conn, _client(tr), "?", model="m", max_rounds=3)
    assert res.stopped_early
    assert res.rounds == 3


def test_ask_refusal_surfaces(seeded_conn):
    tr = FakeTransport([(200, _msg("refusal", []))])
    res = ask(seeded_conn, _client(tr), "?", model="m")
    assert "declined" in res.text


def test_ask_max_tokens_notes_truncation(seeded_conn):
    tr = FakeTransport([(200, _msg("max_tokens", [_text("partial")]))])
    res = ask(seeded_conn, _client(tr), "?", model="m")
    assert "truncated" in res.text


def test_usage_accumulates_across_rounds(seeded_conn):
    tr = FakeTransport(
        [
            (200, _msg("tool_use", [_tool_use("t1", "get_daily_status", {"date": WEIGH_DAY})])),
            (200, _msg("end_turn", [_text("done")])),
        ]
    )
    res = ask(seeded_conn, _client(tr), "?", model="m")
    assert res.usage.input_tokens == 20 and res.usage.output_tokens == 10


# ---- grounding helpers + offline harness -----------------------------------


def test_fabricated_numbers_ignores_dates_and_years():
    assert fabricated_numbers("On 2026-05-01 I have no data for you.", []) == []
    assert fabricated_numbers("Back in 2024 you weighed 83 kg.", []) == ["83"]


def test_run_live_grounding_offline_faithful_passes():
    # every scenario answered honestly, no tool round -> all pass
    tr = FakeTransport(
        [(200, _msg("end_turn", [_text("I don't have that logged for this day.")]))] * 3
    )
    results = run_live_grounding("k", model="m", transport=tr)
    assert all(r["passed"] for r in results)


def test_run_live_grounding_offline_fabrication_fails():
    tr = FakeTransport([(200, _msg("end_turn", [_text("Your recovery was 62.")]))] * 3)
    results = run_live_grounding("k", model="m", transport=tr)
    assert not any(r["passed"] for r in results)


# ---- CLI surface -----------------------------------------------------------


def test_parser_accepts_ask_and_eval():
    from coach.cli.main import build_parser

    p = build_parser()
    a = p.parse_args(["ask", "how am I doing?", "--show-tools"])
    assert a.question and a.show_tools
    assert p.parse_args(["eval", "grounding"]).func
