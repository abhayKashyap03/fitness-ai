"""The coach agent loop (Phase 4): question -> tool calls -> grounded answer.

Wires the deterministic tool contract (:mod:`coach.coach.tools`) to whichever
LLM provider is configured (:mod:`coach.coach.llm`) under the faithfulness
SYSTEM_PROMPT (:mod:`coach.coach.grounding`). The loop speaks only the
canonical shape — it never sees a vendor field name (§2.5), so switching
providers changes a config value, not this file.

The model decides which tools to call; every number comes from the tools; the
loop is **bounded** (§8.7: never loop model calls without a bound).

Failure handling:
  * unknown/failed tool -> error result fed back (the model can recover)
  * ``refusal`` stop -> explicit marker, never a fabricated answer
  * ``pause`` -> re-send, counted against the same round bound
  * round bound hit -> explicit note, never silent truncation
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .grounding import SYSTEM_PROMPT
from .llm import (
    AssistantTurn,
    LLMProvider,
    ToolResult,
    ToolResultTurn,
    ToolUse,
    Turn,
    Usage,
    UserTurn,
)
from .tools import dispatch, tool_specs

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
    conn: sqlite3.Connection,
    uses: list[ToolUse],
    *,
    user_id: int,
    calls: list[ToolCall],
) -> list[ToolResult]:
    """Execute every requested tool; return canonical results.

    A failing tool becomes an error result instead of crashing the loop — the
    model sees what went wrong and can recover or say it lacks the data.
    """
    results: list[ToolResult] = []
    for use in uses:
        try:
            payload = dispatch(conn, use.name, use.args, user_id=user_id)
            results.append(ToolResult(use.id, use.name, payload))
            calls.append(ToolCall(use.name, use.args, True))
        except Exception as exc:
            results.append(ToolResult(use.id, use.name, f"tool error: {exc}", is_error=True))
            calls.append(ToolCall(use.name, use.args, False))
    return results


def ask(
    conn: sqlite3.Connection,
    provider: LLMProvider,
    question: str,
    *,
    user_id: int = 1,
    max_rounds: int = MAX_ROUNDS,
) -> AgentResult:
    """Answer one coaching question, grounded in tool results."""
    turns: list[Turn] = [UserTurn(question)]
    tools = tool_specs()
    calls: list[ToolCall] = []
    usage = Usage()

    for round_n in range(1, max_rounds + 1):
        resp = provider.complete(system=SYSTEM_PROMPT, turns=turns, tools=tools)
        usage = usage + resp.usage

        if resp.stop_reason == "refusal":
            return AgentResult(
                "The model declined to answer this request.", calls, round_n, usage
            )

        if resp.stop_reason == "pause":
            turns.append(AssistantTurn(resp.native))
            continue  # provider resumes; still counted against the bound

        if resp.stop_reason == "tool_use":
            turns.append(AssistantTurn(resp.native))
            turns.append(
                ToolResultTurn(
                    _run_tools(conn, resp.tool_uses, user_id=user_id, calls=calls)
                )
            )
            continue

        text = resp.text
        if resp.stop_reason == "max_tokens":
            text += "\n[response truncated at the model's output limit]"
        return AgentResult(text, calls, round_n, usage)

    return AgentResult(
        "[stopped: reached the tool-call round limit without a final answer]",
        calls,
        max_rounds,
        usage,
        stopped_early=True,
    )
