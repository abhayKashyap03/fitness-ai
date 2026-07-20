"""Minimal Anthropic Messages API client — **standard library only**.

Why not the official SDK: CLAUDE.md §6.4 forbids cloud SDKs without sign-off,
and the Messages API is a single JSON POST — ~40 lines of urllib keeps the
dependency budget at zero while §8.7's cost controls (prompt caching, bounded
retries) stay fully in our hands.

Design:
  * The HTTP layer is an injectable ``transport`` callable so tests script
    responses without any network (§6.2). The default transport is urllib.
  * Retries: 429/500/529 with exponential backoff, honoring ``retry-after``.
    Client errors (400/401/403/404) raise immediately — retrying them is spam.
  * Secrets: the API key lives only in the request header; it is never logged,
    stored, or included in raised errors (§8.4).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# (status_code, parsed_response_json, headers) — injectable for tests (§6.2)
Transport = Callable[[str, dict[str, str], bytes], tuple[int, dict, dict[str, str]]]

_RETRYABLE = {429, 500, 529}


class ApiError(RuntimeError):
    """Non-retryable (or retries-exhausted) API failure. Carries no secrets."""

    def __init__(self, status: int, error_type: str, message: str):
        super().__init__(f"Anthropic API error {status} ({error_type}): {message}")
        self.status = status
        self.error_type = error_type


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
        )


@dataclass(frozen=True)
class ApiResponse:
    """One Messages API response, reduced to what the agent loop needs."""

    stop_reason: str | None
    content: list[dict]  # verbatim content blocks (text / tool_use / thinking)
    usage: Usage
    raw: dict = field(repr=False, default_factory=dict)


def _urllib_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, dict, dict]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode())
        except Exception:
            payload = {"error": {"type": "unknown", "message": "unparseable error body"}}
        return exc.code, payload, dict(exc.headers or {})


class AnthropicClient:
    """Thin Messages API caller with bounded retries and cache-aware system prompt."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: Transport | None = None,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._api_key = api_key
        self._transport = transport or _urllib_transport
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._sleep = sleep

    def create_message(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 8192,
    ) -> ApiResponse:
        """One ``POST /v1/messages``. Retries 429/500/529; raises ApiError otherwise.

        The system prompt is sent as a block list with a ``cache_control``
        breakpoint (§8.7: cache the stable prefix). Order tools -> system ->
        messages is the API's render order; volatile content stays last.
        """
        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": messages,
        }
        if tools:
            body["tools"] = tools

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        payload = json.dumps(body).encode()

        attempt = 0
        while True:
            status, data, resp_headers = self._transport(API_URL, headers, payload)
            if status == 200:
                u = data.get("usage") or {}
                return ApiResponse(
                    stop_reason=data.get("stop_reason"),
                    content=data.get("content") or [],
                    usage=Usage(
                        input_tokens=u.get("input_tokens") or 0,
                        output_tokens=u.get("output_tokens") or 0,
                        cache_read_input_tokens=u.get("cache_read_input_tokens") or 0,
                        cache_creation_input_tokens=u.get("cache_creation_input_tokens") or 0,
                    ),
                    raw=data,
                )

            err = (data or {}).get("error") or {}
            if status in _RETRYABLE and attempt < self._max_retries:
                attempt += 1
                retry_after = resp_headers.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after else self._backoff_s * (2 ** (attempt - 1))
                except ValueError:
                    delay = self._backoff_s * (2 ** (attempt - 1))
                self._sleep(delay)
                continue
            raise ApiError(status, err.get("type", "unknown"), err.get("message", "no detail"))
