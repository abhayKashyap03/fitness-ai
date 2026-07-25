"""Provider-agnostic LLM contract — the canonical shape the coach speaks.

Same principle as the data adapters (§2.5): the agent loop knows ONE shape;
each provider translates its vendor wire format at the edge. Adding a provider
is a new module here, not a change to the coach.

Canonical conversation:
  * :class:`UserTurn`        — the person's message
  * :class:`AssistantTurn`   — the model's reply, carrying the provider's OWN
    content blob verbatim so the round-trip back to that provider is lossless
  * :class:`ToolResultTurn`  — our deterministic tool output going back in

Every provider returns a :class:`LLMResponse` with a canonical ``stop_reason``:
``end_turn`` | ``tool_use`` | ``max_tokens`` | ``refusal`` | ``pause``.

Shared HTTP plumbing lives here too: an injectable ``transport`` (tests never
touch the network, §6.2) and bounded retries (§8.7 — never loop unbounded).
API keys go in headers only, never in a URL or an error message (§8.4).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

# (status_code, parsed_json_body, response_headers) — injectable for tests (§6.2)
Transport = Callable[[str, dict[str, str], bytes], tuple[int, dict, dict[str, str]]]


class ApiError(RuntimeError):
    """Non-retryable (or retries-exhausted) provider failure. Carries no secrets."""

    def __init__(self, provider: str, status: int, error_type: str, message: str):
        super().__init__(f"{provider} API error {status} ({error_type}): {message}")
        self.provider = provider
        self.status = status
        self.error_type = error_type


# ---- canonical conversation ------------------------------------------------


@dataclass(frozen=True)
class UserTurn:
    text: str


@dataclass(frozen=True)
class AssistantTurn:
    """The model's reply. ``native`` is that provider's own content, echoed back
    verbatim on the next call — never reinterpreted by the coach."""

    native: object


@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str  # Anthropic matches results by id...
    name: str  # ...Google matches them by function name; carry both.
    payload: object  # JSON-serializable: dict on success, str on error
    is_error: bool = False


@dataclass(frozen=True)
class ToolResultTurn:
    results: list[ToolResult]


Turn = UserTurn | AssistantTurn | ToolResultTurn


@dataclass(frozen=True)
class ToolSpec:
    """One callable tool, provider-neutral. Providers map this to their schema."""

    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class Usage:
    """Token accounting. ``cached_input_tokens`` / ``cache_write_tokens`` are
    provider-optional and stay 0 where the provider doesn't report them (§2.7:
    absence is absence, not a fabricated number)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True)
class LLMResponse:
    """One model reply, normalized."""

    stop_reason: str  # end_turn | tool_use | max_tokens | refusal | pause
    text: str
    tool_uses: list[ToolUse] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    native: object = None  # provider content, for the AssistantTurn round-trip


class LLMProvider(Protocol):
    """What the coach needs from any model provider."""

    name: str
    model: str

    def complete(
        self, *, system: str, turns: list[Turn], tools: list[ToolSpec], max_tokens: int = ...
    ) -> LLMResponse: ...


# ---- shared HTTP plumbing --------------------------------------------------


def urllib_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, dict, dict]:
    """Default transport: one POST, JSON in / JSON out. No third-party deps."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return resp.status, json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode())
        except Exception:
            payload = {"error": {"message": "unparseable error body"}}
        return exc.code, payload, dict(exc.headers or {})


class HttpProviderBase:
    """Retry/backoff shared by the concrete providers.

    Retries only the statuses a provider declares transient; client errors
    (400/401/403/404) raise immediately — retrying them is just spam.
    """

    name: str = "llm"
    retryable: frozenset[int] = frozenset({429, 500, 503, 529})

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._transport = transport or urllib_transport
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._sleep = sleep

    def _post(self, url: str, headers: dict[str, str], body: dict) -> dict:
        payload = json.dumps(body).encode()
        attempt = 0
        while True:
            status, data, resp_headers = self._transport(url, headers, payload)
            if status == 200:
                return data
            if status in self.retryable and attempt < self._max_retries:
                attempt += 1
                self._sleep(self._retry_delay(attempt, resp_headers, data))
                continue
            etype, message = self._error_fields(data)
            raise ApiError(self.name, status, etype, message)

    def _retry_delay(self, attempt: int, headers: dict[str, str], data: dict) -> float:
        """Server-suggested wait when available, else exponential backoff.

        ``data`` (the error body) is passed so providers that put the hint in
        JSON instead of a Retry-After header (Gemini's RetryInfo) can override.
        """
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return self._backoff_s * (2 ** (attempt - 1))

    @staticmethod
    def _error_fields(data: dict) -> tuple[str, str]:
        err = (data or {}).get("error") or {}
        etype = err.get("type") or err.get("status") or "unknown"
        return str(etype), str(err.get("message", "no detail"))
