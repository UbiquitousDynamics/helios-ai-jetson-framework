from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import config
from api.api_client import APIClient
from api.providers.codex_app_server import (
    CodexAppServerAdapter,
    _codex_child_env,
    _copy_chatgpt_auth,
)
from api.providers.contracts import (
    ChatMessage,
    ChatRequest,
    Completed,
    ContentOrigin,
    ErrorCategory,
    PrivacyLevel,
    ProviderError,
    ReasoningDelta,
    Role,
    TextDelta,
)


@dataclass
class FakeTurn:
    events: list[Any]
    id: str = "turn-safe-id"
    interrupted: bool = False

    def stream(self) -> list[Any]:
        return self.events

    def interrupt(self) -> None:
        self.interrupted = True


class FakeRuntime:
    def __init__(self, kind: str | None, events: list[Any]) -> None:
        self.kind = kind
        self.turn = FakeTurn(events)
        self.started: dict[str, Any] | None = None
        self.closed = False

    def account_kind(self) -> str | None:
        return self.kind

    def start_turn(self, **kwargs: Any) -> FakeTurn:
        self.started = kwargs
        return self.turn

    def close(self) -> None:
        self.closed = True


def notification(method: str, payload: Any) -> dict[str, Any]:
    return {"method": method, "payload": payload}


def request(**overrides: Any) -> ChatRequest:
    values: dict[str, Any] = {
        "model": "gpt-example",
        "messages": (
            ChatMessage(
                Role.SYSTEM,
                "Be concise.",
                origin=ContentOrigin.STATIC_INSTRUCTION,
            ),
            ChatMessage(
                Role.USER,
                "Ciao",
                origin=ContentOrigin.RAW_TRANSCRIPT,
            ),
        ),
        "mode": "talk",
        "language": "it",
        "privacy": PrivacyLevel.REMOTE_ALLOWED,
        "remote_authorized": True,
        "max_output_tokens": 80,
        "options": {"reasoning_effort": "low"},
    }
    values.update(overrides)
    return ChatRequest(**values)


def test_child_environment_prevents_api_key_auth() -> None:
    assert _codex_child_env() == {
        "OPENAI_API_KEY": "",
        "CODEX_API_KEY": "",
    }


def test_isolated_codex_home_copies_auth_but_not_user_configuration(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text('{"auth_mode":"chatgpt"}', encoding="utf-8")
    (source / "config.toml").write_text("[mcp_servers.unsafe]", encoding="utf-8")
    isolated = tmp_path / "isolated"

    _copy_chatgpt_auth(source, isolated)

    assert (isolated / "auth.json").read_text(encoding="utf-8") == (
        '{"auth_mode":"chatgpt"}'
    )
    assert not (isolated / "config.toml").exists()
    child_env = _codex_child_env(isolated)
    assert child_env["CODEX_HOME"] == str(isolated)


@pytest.mark.parametrize("account_kind", [None, "apiKey"])
def test_only_chatgpt_accounts_are_accepted_before_transmission(
    account_kind: str | None,
) -> None:
    runtime = FakeRuntime(account_kind, [])
    provider = CodexAppServerAdapter("openai-codex", runtime=runtime)

    with pytest.raises(ProviderError) as captured:
        list(provider.stream(request()))

    assert captured.value.category is ErrorCategory.AUTHENTICATION
    assert captured.value.transmitted is False
    assert runtime.started is None


def test_streams_text_reasoning_usage_and_completion() -> None:
    runtime = FakeRuntime(
        "chatgpt",
        [
            notification("item/reasoning/textDelta", {"delta": "Penso. "}),
            notification("item/agentMessage/delta", {"delta": "Ciao!"}),
            notification(
                "thread/tokenUsage/updated",
                {
                    "token_usage": {
                        "last": {
                            "input_tokens": 11,
                            "cached_input_tokens": 3,
                            "output_tokens": 5,
                            "reasoning_output_tokens": 2,
                            "total_tokens": 16,
                        }
                    }
                },
            ),
            notification("model/rerouted", {"to_model": "gpt-resolved"}),
            notification("turn/completed", {"turn": {"status": "completed"}}),
        ],
    )
    provider = CodexAppServerAdapter("openai-codex", runtime=runtime)

    events = list(provider.stream(request()))

    assert events[0] == ReasoningDelta("Penso. ")
    assert events[1] == TextDelta("Ciao!")
    assert isinstance(events[-1], Completed)
    assert events[-1].metadata.resolved_model == "gpt-resolved"
    assert events[-1].metadata.usage.input_tokens == 11
    assert events[-1].metadata.usage.reasoning_tokens == 2
    assert events[-1].metadata.request_id == "turn-safe-id"
    assert runtime.started is not None
    assert runtime.started["model"] == "gpt-example"
    assert runtime.started["effort"] == "low"
    assert "Be concise." in runtime.started["developer_instructions"]
    assert "[user]\nCiao" in runtime.started["prompt"]


def test_privacy_and_unknown_options_fail_before_runtime_creation() -> None:
    calls = 0

    def factory() -> FakeRuntime:
        nonlocal calls
        calls += 1
        return FakeRuntime("chatgpt", [])

    provider = CodexAppServerAdapter("openai-codex", runtime_factory=factory)

    with pytest.raises(ProviderError) as privacy:
        list(
            provider.stream(
                request(
                    privacy=PrivacyLevel.LOCAL_ONLY,
                    remote_authorized=False,
                )
            )
        )
    with pytest.raises(ProviderError) as unsupported:
        list(provider.stream(request(options={"temperature": 0.2})))

    assert privacy.value.category is ErrorCategory.PRIVACY_BLOCKED
    assert unsupported.value.category is ErrorCategory.UNSUPPORTED_FEATURE
    assert calls == 0


def test_close_releases_owned_runtime() -> None:
    runtime = FakeRuntime("chatgpt", [])
    provider = CodexAppServerAdapter("openai-codex", runtime=runtime)

    provider.close()
    provider.close()

    assert runtime.closed


def test_api_client_registers_configured_codex_adapter_lazily() -> None:
    routing = Path(__file__).resolve().parents[1] / "examples" / (
        "llm-routing.codex-subscription.toml"
    )
    client = APIClient(llm_settings=config.load_llm_settings(routing))
    try:
        provider = client._registry.get("openai-codex")
        assert isinstance(provider, CodexAppServerAdapter)
        assert provider._runtime is None
    finally:
        client.close()
