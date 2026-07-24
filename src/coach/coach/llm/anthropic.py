"""Anthropic Messages API provider — stdlib only, no SDK (§6.4).

Translates the canonical conversation to Anthropic's wire format and back.
Vendor field names (``content``, ``tool_use``, ``cache_control``, …) stop at
this module's boundary, exactly like a data adapter (§2.5).
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

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-8"

# Anthropic-specific transient statuses (529 = overloaded).
_RETRYABLE = frozenset({429, 500, 529})

_STOP_MAP = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "pause_turn": "pause",
}


class AnthropicProvider(HttpProviderBase):
    name = "anthropic"
    retryable = _RETRYABLE

    def __init__(self, api_key: str, *, model: str = "", **kw):
        super().__init__(**kw)
        self._api_key = api_key
        self.model = model or DEFAULT_MODEL

    # -- canonical -> wire ---------------------------------------------------

    @staticmethod
    def _messages(turns: list[Turn]) -> list[dict]:
        out: list[dict] = []
        for t in turns:
            if isinstance(t, UserTurn):
                out.append({"role": "user", "content": t.text})
            elif isinstance(t, AssistantTurn):
                out.append({"role": "assistant", "content": t.native})
            elif isinstance(t, ToolResultTurn):
                # all results for one assistant turn go in ONE user message
                blocks = []
                for r in t.results:
                    content = r.payload if isinstance(r.payload, str) else json.dumps(r.payload)
                    block: dict = {
                        "type": "tool_result",
                        "tool_use_id": r.tool_use_id,
                        "content": content,
                    }
                    if r.is_error:
                        block["is_error"] = True
                    blocks.append(block)
                out.append({"role": "user", "content": blocks})
        return out

    @staticmethod
    def _tools(tools: list[ToolSpec]) -> list[dict]:
        return [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]

    # -- call ----------------------------------------------------------------

    def complete(
        self, *, system: str, turns: list[Turn], tools: list[ToolSpec], max_tokens: int = 8192
    ) -> LLMResponse:
        body: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            # cache the stable system prefix (§8.7)
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": self._messages(turns),
        }
        if tools:
            body["tools"] = self._tools(tools)

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        data = self._post(API_URL, headers, body)

        content = data.get("content") or []
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        tool_uses = [
            ToolUse(b["id"], b.get("name", ""), b.get("input") or {})
            for b in content
            if b.get("type") == "tool_use"
        ]
        u = data.get("usage") or {}
        return LLMResponse(
            stop_reason=_STOP_MAP.get(data.get("stop_reason") or "", "end_turn"),
            text=text,
            tool_uses=tool_uses,
            usage=Usage(
                input_tokens=u.get("input_tokens") or 0,
                output_tokens=u.get("output_tokens") or 0,
                cached_input_tokens=u.get("cache_read_input_tokens") or 0,
                cache_write_tokens=u.get("cache_creation_input_tokens") or 0,
            ),
            native=content,
        )
