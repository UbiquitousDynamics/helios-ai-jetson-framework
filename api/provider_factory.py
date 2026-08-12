"""Lazy construction of concrete providers from validated configuration."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import config
from api.providers.contracts import ChatProvider

if TYPE_CHECKING:
    from api.conversation import ConversationSession

ProviderFactory = Callable[[], ChatProvider]


def configured_provider_factory(
    settings: config.LLMProviderSettings,
    *,
    allow_remote_context: bool = False,
    context_idle_timeout_seconds: float = 900.0,
    context_max_turns: int = 20,
    conversation_session: ConversationSession | None = None,
) -> ProviderFactory | None:
    """Return a lazy adapter factory, or ``None`` for externally handled types."""

    if settings.adapter == "openai_chat_sse" and settings.api_key_env is not None:

        def openai_chat_sse_factory() -> ChatProvider:
            from api.providers.openai_chat_sse import OpenAIChatSSEAdapter

            return OpenAIChatSSEAdapter(
                provider=settings.name,
                endpoint=settings.endpoint,
                api_key_env=settings.api_key_env,
            )

        return openai_chat_sse_factory

    if settings.adapter == "codex_app_server":

        def codex_app_server_factory() -> ChatProvider:
            from api.providers.codex_app_server import CodexAppServerAdapter

            return CodexAppServerAdapter(
                provider=settings.name,
                endpoint=settings.endpoint,
                allow_remote_context=allow_remote_context,
                context_idle_timeout_seconds=context_idle_timeout_seconds,
                context_max_turns=context_max_turns,
                conversation_session=conversation_session,
            )

        return codex_app_server_factory

    return None


__all__ = ["ProviderFactory", "configured_provider_factory"]
