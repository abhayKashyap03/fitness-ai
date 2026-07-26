"""xAI Grok provider — stdlib only, no SDK (§6.4).

Grok speaks the **OpenAI chat-completions** shape, which differs from both
existing providers in ways that all stop at this boundary (§2.5):

  * The system prompt is a MESSAGE (``role: "system"``), not a separate
    top-level field like Anthropic's ``system`` or Gemini's
    ``systemInstruction``.
  * Tool results are **one message each** (``role: "tool"`` +
    ``tool_call_id``), where Anthropic packs them into a single user message
    and Gemini into a single user content. So one canonical
    :class:`ToolResultTurn` fans out to N messages here.
  * ``tool_calls[].function.arguments`` is a **JSON string**, not an object —
    parsed on the way in, echoed back verbatim on the way out.
  * Tool parameters take plain JSON Schema, so ``input_schema`` passes through
    untouched (no Gemini-style key stripping / type upper-casing).
  * Prompt caching is automatic; ``usage.prompt_tokens_details.cached_tokens``
    reports it when it happens.
"""

from __future__ import annotations

import json

from .base import (
    AssistantTurn,
    HttpProviderBase,
    LLMResponse,
    ToolResultTurn,
    ToolSpec,
    ToolUse,
    Turn,
    Usage,
    UserTurn,
)

API_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_MODEL = "grok-4.20-0309-non-reasoning"  # override with COACH_MODEL; xAI model names churn

# xAI transient statuses (502/503 = gateway/capacity).
_RETRYABLE = frozenset({429, 500, 502, 503})

# finish_reason -> canonical stop_reason.
_STOP_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",  # legacy alias
    "content_filter": "refusal",
}


class GrokProvider(HttpProviderBase):
    name = "grok"
    retryable = _RETRYABLE

    def __init__(self, api_key: str, *, model: str = "", **kw):
        super().__init__(**kw)
        self._api_key = api_key
        self.model = model or DEFAULT_MODEL

    # -- canonical -> wire ---------------------------------------------------

    @staticmethod
    def _messages(system: str, turns: list[Turn]) -> list[dict]:
        """Flatten the canonical conversation into OpenAI-style messages."""
        out: list[dict] = []
        if system:
            out.append({"role": "system", "content": system})
        for t in turns:
            if isinstance(t, UserTurn):
                out.append({"role": "user", "content": t.text})
            elif isinstance(t, AssistantTurn):
                # native is Grok's own assistant message, echoed back verbatim so
                # its tool_call ids still match the tool messages that follow
                out.append(t.native)  # type: ignore[arg-type]
            elif isinstance(t, ToolResultTurn):
                # ONE message per result, matched by tool_call_id
                for r in t.results:
                    content = (
                        r.payload
                        if isinstance(r.payload, str)
                        else json.dumps(r.payload, default=str)
                    )
                    out.append(
                        {"role": "tool", "tool_call_id": r.tool_use_id, "content": content}
                    )
        return out

    @staticmethod
    def _tools(tools: list[ToolSpec]) -> list[dict]:
        """Plain JSON Schema passes through — no Gemini-style rewriting needed."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]

    # -- call ----------------------------------------------------------------

    def complete(
        self, *, system: str, turns: list[Turn], tools: list[ToolSpec], max_tokens: int = 8192
    ) -> LLMResponse:
        body: dict = {
            "model": self.model,
            "messages": self._messages(system, turns),
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = self._tools(tools)
            body["tool_choice"] = "auto"

        # key travels in a header, never a URL query parameter (§8.4)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        data = self._post(API_URL, headers, body)

        choices = data.get("choices") or []
        if not choices:
            return LLMResponse(
                stop_reason="end_turn", text="", usage=self._usage(data), native=None
            )

        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""

        tool_uses: list[ToolUse] = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    # a malformed arg blob must not crash the loop — the tool
                    # layer reports the failure and the model can recover
                    args = {}
            else:
                args = raw_args or {}
            tool_uses.append(ToolUse(str(call.get("id", "")), fn.get("name", ""), args))

        finish = choice.get("finish_reason") or "stop"
        stop_reason = _STOP_MAP.get(finish, "end_turn")
        # tool_calls present but finish_reason lagging: trust the payload
        if tool_uses and stop_reason == "end_turn":
            stop_reason = "tool_use"

        return LLMResponse(
            stop_reason=stop_reason,
            text=text,
            tool_uses=tool_uses,
            usage=self._usage(data),
            native=message,
        )

    @staticmethod
    def _usage(data: dict) -> Usage:
        u = data.get("usage") or {}
        prompt = u.get("prompt_tokens") or 0
        cached = ((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        return Usage(
            # prompt_tokens includes cached tokens; report them separately
            input_tokens=max(prompt - cached, 0),
            output_tokens=u.get("completion_tokens") or 0,
            cached_input_tokens=cached,
        )

    @staticmethod
    def _error_fields(data: dict) -> tuple[str, str]:
        err = (data or {}).get("error")
        if isinstance(err, str):  # xAI sometimes returns a bare string
            return "error", err
        err = err or {}
        etype = err.get("type") or err.get("code") or "unknown"
        return str(etype), str(err.get("message", "no detail"))
