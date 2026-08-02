"""Lazy construction of concrete providers from validated configuration."""

from __future__ import annotations

from collections.abc import Callable

import config
from api.providers.contracts import ChatProvider

ProviderFactory = Callable[[], ChatProvider]


def configured_provider_factory(
    settings: config.LLMProviderSettings,
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
            )

        return codex_app_server_factory

    return None


__all__ = ["ProviderFactory", "configured_provider_factory"]
