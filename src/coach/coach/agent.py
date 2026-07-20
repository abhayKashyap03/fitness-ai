"""The coach agent loop (Phase 4): question -> tool calls -> grounded answer.

Wires the deterministic tool contract (:mod:`coach.coach.tools`) to the model
under the faithfulness SYSTEM_PROMPT (:mod:`coach.coach.grounding`). The model
decides which tools to call; every number comes from the tools; the loop is
**bounded** (§8.7: never loop model calls without a bound).

Failure handling:
  * unknown/failed tool -> ``tool_result`` with ``is_error`` (model recovers)
  * ``refusal`` stop -> explicit marker, no fabricated answer
  * ``pause_turn`` -> re-send, counted against the same round bound
  * round bound hit -> partial text + explicit note, never silent truncation
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .grounding import SYSTEM_PROMPT
from .llm import AnthropicClient, Usage
from .tools import anthropic_tool_defs, dispatch

MAX_ROUNDS = 8  # hard bound on model calls per question (§8.7)


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict
    ok: bool


@dataclass(frozen=True)
class AgentResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    rounds: int = 0
    usage: Usage = field(default_factory=Usage)
    stopped_early: bool = False  # round bound hit before a natural end


def _run_tools(
    conn: sqlite3.Connection, blocks: list[dict], *, user_id: int, calls: list[ToolCall]
) -> list[dict]:
    """Execute every tool_use block; return tool_result blocks (single message).

    A failing tool becomes ``is_error`` instead of crashing the loop — the
    model sees the error text and can recover or say it lacks the data.
    """
    import json

    results: list[dict] = []
    for b in blocks:
        name, args = b.get("name", ""), b.get("input") or {}
        try:
            out = dispatch(conn, name, args, user_id=user_id)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b["id"],
                    "content": json.dumps(out),
                }
            )
            calls.append(ToolCall(name, args, True))
        except Exception as exc:
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b["id"],
                    "content": f"tool error: {exc}",
                    "is_error": True,
                }
            )
            calls.append(ToolCall(name, args, False))
    return results


def ask(
    conn: sqlite3.Connection,
    client: AnthropicClient,
    question: str,
    *,
    model: str,
    user_id: int = 1,
    max_rounds: int = MAX_ROUNDS,
) -> AgentResult:
    """Answer one coaching question, grounded in tool results."""
    messages: list[dict] = [{"role": "user", "content": question}]
    tools = anthropic_tool_defs()
    calls: list[ToolCall] = []
    usage = Usage()

    for round_n in range(1, max_rounds + 1):
        resp = client.create_message(
            model=model, system=SYSTEM_PROMPT, messages=messages, tools=tools
        )
        usage = usage + resp.usage

        if resp.stop_reason == "refusal":
            return AgentResult(
                "The model declined to answer this request.", calls, round_n, usage
            )

        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue  # server resumes; still counted against the bound

        if resp.stop_reason == "tool_use":
            tool_blocks = [b for b in resp.content if b.get("type") == "tool_use"]
            messages.append({"role": "assistant", "content": resp.content})
            messages.append(
                {
                    "role": "user",
                    "content": _run_tools(conn, tool_blocks, user_id=user_id, calls=calls),
                }
            )
            continue

        # end_turn / max_tokens / stop_sequence: extract final text
        text = "".join(b.get("text", "") for b in resp.content if b.get("type") == "text")
        if resp.stop_reason == "max_tokens":
            text += "\n[response truncated at max_tokens]"
        return AgentResult(text, calls, round_n, usage)

    return AgentResult(
        "[stopped: reached the tool-call round limit without a final answer]",
        calls,
        max_rounds,
        usage,
        stopped_early=True,
    )
