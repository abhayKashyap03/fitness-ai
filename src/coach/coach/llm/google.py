"""Google Gemini (Generative Language API) provider — stdlib only, no SDK (§6.4).

Translates the canonical conversation to Gemini's wire format and back. Chosen
as the default provider because it has a genuine free tier — the coach should
not require paid API credits to run (§8.7).

Vendor shapes that stop at this boundary (§2.5):
  * ``contents[] / parts[]`` instead of messages/blocks; role ``model``, not
    ``assistant``.
  * ``functionCall`` / ``functionResponse`` instead of tool_use/tool_result,
    matched by **name** rather than id — the canonical ``ToolResult`` carries
    both, so neither provider is starved.
  * ``functionDeclarations`` take an OpenAPI-subset schema: uppercase type
    names, and unsupported keywords must be stripped or the request 400s.
  * Caching is implicit on 2.5-series models; ``cachedContentTokenCount`` is
    reported when it happens (no explicit cache_control to set).
"""

from __future__ import annotations

import re

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

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-2.5-flash"  # free tier; override with COACH_MODEL

_RETRYABLE = frozenset({429, 500, 503})

# Gemini's Schema is an OpenAPI subset — anything else is rejected.
_SCHEMA_KEYS = frozenset(
    {"type", "format", "description", "nullable", "enum", "items", "properties", "required"}
)
# finishReason values that mean "the model declined", not "the model finished".
_REFUSAL_REASONS = frozenset(
    {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY"}
)


def _to_gemini_schema(schema: dict) -> dict:
    """Convert a JSON Schema fragment to Gemini's Schema shape.

    Type names are upper-cased (the proto enum form) and unsupported keywords
    (``minimum``, ``additionalProperties``, …) are dropped rather than passed
    through — Gemini rejects the whole request on an unknown field.
    """
    out: dict = {}
    for key, value in schema.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "type" and isinstance(value, str):
            out["type"] = value.upper()
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {k: _to_gemini_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            out["items"] = _to_gemini_schema(value)
        else:
            out[key] = value
    return out


class GoogleProvider(HttpProviderBase):
    name = "google"
    retryable = _RETRYABLE

    def __init__(self, api_key: str, *, model: str = "", **kw):
        super().__init__(**kw)
        self._api_key = api_key
        self.model = model or DEFAULT_MODEL

    def _retry_delay(self, attempt: int, headers: dict[str, str], data: dict) -> float:
        """Honor Gemini's JSON retry hint (free-tier 429s carry it in the BODY).

        Gemini sends no Retry-After header; the wait lives in
        ``error.details[].retryDelay`` ("54s") and/or the message text
        ("Please retry in 54.1s"). Without this, the base exponential backoff
        (capped well under a minute-window quota reset) exhausts its retries
        too early and a free-tier burst kills the whole eval run.
        """
        err = (data or {}).get("error") or {}
        for d in err.get("details") or []:
            delay = d.get("retryDelay")
            if isinstance(delay, str):
                m = re.fullmatch(r"([0-9.]+)s", delay.strip())
                if m:
                    return min(float(m.group(1)) + 1.0, 90.0)
        m = re.search(r"retry in ([0-9.]+)s", str(err.get("message", "")))
        if m:
            return min(float(m.group(1)) + 1.0, 90.0)
        return super()._retry_delay(attempt, headers, data)

    # -- canonical -> wire ---------------------------------------------------

    @staticmethod
    def _contents(turns: list[Turn]) -> list[dict]:
        out: list[dict] = []
        for t in turns:
            if isinstance(t, UserTurn):
                out.append({"role": "user", "parts": [{"text": t.text}]})
            elif isinstance(t, AssistantTurn):
                out.append({"role": "model", "parts": t.native})
            elif isinstance(t, ToolResultTurn):
                # Gemini matches a response to its call by NAME, and all
                # responses for one model turn go in a single user content.
                parts = []
                for r in t.results:
                    payload = {"error": r.payload} if r.is_error else {"result": r.payload}
                    parts.append(
                        {"functionResponse": {"name": r.name, "response": payload}}
                    )
                out.append({"role": "user", "parts": parts})
        return out

    @staticmethod
    def _tools(tools: list[ToolSpec]) -> list[dict]:
        return [
            {
                "functionDeclarations": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": _to_gemini_schema(t.input_schema),
                    }
                    for t in tools
                ]
            }
        ]

    # -- call ----------------------------------------------------------------

    def complete(
        self, *, system: str, turns: list[Turn], tools: list[ToolSpec], max_tokens: int = 8192
    ) -> LLMResponse:
        body: dict = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": self._contents(turns),
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if tools:
            body["tools"] = self._tools(tools)

        # key travels in a header, never a URL query parameter
        headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
        url = f"{API_BASE}/{self.model}:generateContent"
        data = self._post(url, headers, body)

        candidates = data.get("candidates") or []
        if not candidates:
            # a prompt blocked before generation has no candidate at all
            blocked = ((data.get("promptFeedback") or {}).get("blockReason")) or ""
            return LLMResponse(
                stop_reason="refusal" if blocked else "end_turn",
                text="",
                usage=self._usage(data),
                native=[],
            )

        cand = candidates[0]
        parts = ((cand.get("content") or {}).get("parts")) or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p.get("text"), str))

        tool_uses: list[ToolUse] = []
        for i, p in enumerate(parts):
            call = p.get("functionCall")
            if call:
                name = call.get("name", "")
                # Gemini has no call id; synthesize a stable one for our records
                tool_uses.append(ToolUse(f"{name}-{i}", name, call.get("args") or {}))

        finish = cand.get("finishReason") or "STOP"
        if finish in _REFUSAL_REASONS:
            stop_reason = "refusal"
        elif finish == "MAX_TOKENS":
            stop_reason = "max_tokens"
        elif tool_uses:
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"

        return LLMResponse(
            stop_reason=stop_reason,
            text=text,
            tool_uses=tool_uses,
            usage=self._usage(data),
            native=parts,
        )

    @staticmethod
    def _usage(data: dict) -> Usage:
        m = data.get("usageMetadata") or {}
        prompt = m.get("promptTokenCount") or 0
        cached = m.get("cachedContentTokenCount") or 0
        return Usage(
            # promptTokenCount includes cached tokens; report them separately
            input_tokens=max(prompt - cached, 0),
            output_tokens=m.get("candidatesTokenCount") or 0,
            cached_input_tokens=cached,
        )

    @staticmethod
    def _error_fields(data: dict) -> tuple[str, str]:
        err = (data or {}).get("error") or {}
        return str(err.get("status") or "unknown"), str(err.get("message", "no detail"))
