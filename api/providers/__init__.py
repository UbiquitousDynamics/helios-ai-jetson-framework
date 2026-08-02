"""Provider adapters and normalized language-model contracts."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from api.providers.contracts import (
    CancellationToken,
    ChatMessage,
    ChatProvider,
    ChatRequest,
    Completed,
    CompletionMetadata,
    ContentOrigin,
    ErrorCategory,
    FinishReason,
    PrivacyLevel,
    ProviderCapabilities,
    ProviderError,
    ProviderIdentity,
    RateLimitSnapshot,
    ReasoningDelta,
    Refused,
    Role,
    StreamEvent,
    TextDelta,
    Timeouts,
    Usage,
)

if TYPE_CHECKING:
    from api.providers.codex_app_server import CodexAppServerAdapter
    from api.providers.ollama import OllamaAdapter
    from api.providers.openai_chat_sse import OpenAIChatSSEAdapter

_ADAPTER_MODULES = {
    "CodexAppServerAdapter": "api.providers.codex_app_server",
    "OllamaAdapter": "api.providers.ollama",
    "OpenAIChatSSEAdapter": "api.providers.openai_chat_sse",
}


def __getattr__(name: str) -> Any:
    """Load concrete adapters only when a compatibility re-export is used."""

    module_name = _ADAPTER_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "CancellationToken",
    "CodexAppServerAdapter",
    "ChatMessage",
    "ChatProvider",
    "ChatRequest",
    "Completed",
    "CompletionMetadata",
    "ContentOrigin",
    "ErrorCategory",
    "FinishReason",
    "OllamaAdapter",
    "PrivacyLevel",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderIdentity",
    "RateLimitSnapshot",
    "ReasoningDelta",
    "Refused",
    "Role",
    "StreamEvent",
    "TextDelta",
    "Timeouts",
    "Usage",
    "OpenAIChatSSEAdapter",
]
