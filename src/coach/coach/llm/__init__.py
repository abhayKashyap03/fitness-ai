"""LLM providers for the coach layer — one module per vendor.

Mirrors ``coach.adapters``: the coach speaks the canonical shape in
:mod:`coach.coach.llm.base`; each provider translates its vendor wire format at
the edge (§2.5). Adding a provider is a new module plus one line in
:data:`PROVIDERS` — never a change to the agent loop.

Stdlib only, no vendor SDKs (§6.4).
"""

from __future__ import annotations

from collections.abc import Callable

from .anthropic import AnthropicProvider
from .base import (
    ApiError,
    AssistantTurn,
    LLMProvider,
    LLMResponse,
    ToolResult,
    ToolResultTurn,
    ToolSpec,
    ToolUse,
    Transport,
    Turn,
    Usage,
    UserTurn,
)
from .google import GoogleProvider
from .grok import GrokProvider

# name -> constructor. Typed as returning the protocol so a new provider only
# has to satisfy LLMProvider, not inherit any particular base.
PROVIDERS: dict[str, Callable[..., LLMProvider]] = {
    "google": GoogleProvider,
    "anthropic": AnthropicProvider,
    "grok": GrokProvider,
}

__all__ = [
    "PROVIDERS",
    "AnthropicProvider",
    "ApiError",
    "AssistantTurn",
    "GoogleProvider",
    "GrokProvider",
    "LLMProvider",
    "LLMResponse",
    "ToolResult",
    "ToolResultTurn",
    "ToolSpec",
    "ToolUse",
    "Transport",
    "Turn",
    "Usage",
    "UserTurn",
    "build_provider",
]


def build_provider(
    provider: str, api_key: str, *, model: str = "", **kw
) -> LLMProvider:
    """Construct a provider by name. Raises on an unknown provider."""
    cls = PROVIDERS.get(provider)
    if cls is None:
        known = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"unknown LLM provider {provider!r} (known: {known})")
    return cls(api_key, model=model, **kw)
