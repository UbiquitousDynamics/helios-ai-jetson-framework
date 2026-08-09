from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import config
from api.api_client import APIClient
from api.provider_factory import configured_provider_factory
from api.providers.codex_app_server import CodexAppServerAdapter
from api.routing import Connectivity


class FakeTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)


class FakeOllamaClient:
    def __init__(self, text: str = "Local.") -> None:
        self.text = text
        self.calls: list[dict[str, object]] = []

    def chat(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return iter([{"message": {"content": self.text}, "done": True}])


class NoCallCodexRuntime:
    def __init__(self) -> None:
        self.account_calls = 0
        self.turn_calls = 0
        self.inject_calls = 0

    def account_kind(self) -> str:
        self.account_calls += 1
        raise AssertionError("a local Ollama route must not authenticate Codex")

    def start_turn(self, **_kwargs: Any) -> object:
        self.turn_calls += 1
        raise AssertionError("a local Ollama route must not start or resume a Codex turn")

    def inject_items(self, *_args: Any, **_kwargs: Any) -> None:
        self.inject_calls += 1
        raise AssertionError("an Ollama answer must not be backfilled into Codex")

    def close(self) -> None:
        return None


def codex_provider_settings() -> config.LLMProviderSettings:
    return config.LLMProviderSettings(
        name="openai-codex",
        adapter="codex_app_server",
        endpoint="stdio://codex",
        locality="remote",
    )


def codex_routing_settings(
    tmp_path: Path,
    *,
    allow_remote_context: bool = True,
    idle_timeout: float = 37.5,
    max_turns: int = 9,
) -> config.LLMSettings:
    return config.LLMSettings(
        routing_file=tmp_path / "routing.toml",
        routing_policy="remote_first",
        remote_enabled=True,
        privacy=config.LLMPrivacySettings(
            default="remote_allowed",
            allow_remote_transcripts=True,
            allow_remote_context=allow_remote_context,
        ),
        context_idle_timeout_seconds=idle_timeout,
        context_max_turns=max_turns,
        budget=config.LLMBudgetSettings(enabled=False),
        observability=config.LLMObservabilitySettings(metrics_enabled=False),
        talk=config.LLMModeSettings(candidates=("codex-talk", "local-talk")),
        providers=(codex_provider_settings(),),
        targets=(
            config.LLMTargetSettings(
                name="codex-talk",
                provider="openai-codex",
                model="gpt-5.6-luna",
            ),
            config.LLMTargetSettings(
                name="local-talk",
                provider="ollama",
                model="local-model",
            ),
        ),
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"context_idle_timeout_seconds": 0}, "idle timeout"),
        ({"context_idle_timeout_seconds": float("inf")}, "idle timeout"),
        ({"context_idle_timeout_seconds": True}, "idle timeout"),
        ({"context_max_turns": 0}, "turn cap"),
        ({"context_max_turns": 1.5}, "turn cap"),
        ({"context_max_turns": True}, "turn cap"),
    ],
)
def test_llm_context_lifecycle_settings_are_strictly_validated(
    overrides: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(config.ConfigurationError, match=message):
        config.LLMSettings(**overrides)


def test_context_lifecycle_environment_overrides_are_parsed(tmp_path: Path) -> None:
    settings = config.Settings.from_env(
        tmp_path,
        environ={
            "HELIOS_LLM_CONTEXT_IDLE_TIMEOUT_SECONDS": "42.5",
            "HELIOS_LLM_CONTEXT_MAX_TURNS": "7",
        },
    )

    assert settings.llm.context_idle_timeout_seconds == 42.5
    assert settings.llm.context_max_turns == 7


@pytest.mark.parametrize(
    "environ",
    [
        {"HELIOS_LLM_CONTEXT_IDLE_TIMEOUT_SECONDS": "not-a-number"},
        {"HELIOS_LLM_CONTEXT_IDLE_TIMEOUT_SECONDS": "0"},
        {"HELIOS_LLM_CONTEXT_MAX_TURNS": "not-an-integer"},
        {"HELIOS_LLM_CONTEXT_MAX_TURNS": "0"},
    ],
)
def test_invalid_context_lifecycle_environment_fails_remote_routing_closed(
    tmp_path: Path,
    environ: dict[str, str],
) -> None:
    settings = config.Settings.from_env(tmp_path, environ=environ)

    assert settings.llm.emergency_local_only
    assert not settings.llm.remote_enabled


def test_codex_provider_factory_forwards_context_lifecycle_knobs() -> None:
    factory = configured_provider_factory(
        codex_provider_settings(),
        allow_remote_context=True,
        context_idle_timeout_seconds=81.25,
        context_max_turns=13,
    )

    assert factory is not None
    provider = factory()
    try:
        assert isinstance(provider, CodexAppServerAdapter)
        assert provider._allow_remote_context is True
        assert provider._context_idle_timeout_seconds == 81.25
        assert provider._context_max_turns == 13
        assert provider._runtime is None
    finally:
        provider.close()


def test_api_client_forwards_privacy_and_lifecycle_settings_to_lazy_codex_adapter(
    tmp_path: Path,
) -> None:
    client = APIClient(
        client=FakeOllamaClient(),
        tts=FakeTTS(),
        llm_settings=codex_routing_settings(
            tmp_path,
            allow_remote_context=True,
            idle_timeout=53.5,
            max_turns=11,
        ),
        connectivity=Connectivity.UNKNOWN,
    )
    try:
        provider = client._registry.get("openai-codex")

        assert isinstance(provider, CodexAppServerAdapter)
        assert provider._allow_remote_context is True
        assert provider._context_idle_timeout_seconds == 53.5
        assert provider._context_max_turns == 11
        assert provider._runtime is None
    finally:
        client.close()


def test_ollama_answer_makes_no_codex_runtime_or_backfill_calls(tmp_path: Path) -> None:
    runtime = NoCallCodexRuntime()
    codex = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        allow_remote_context=True,
    )
    ollama = FakeOllamaClient()
    tts = FakeTTS()
    client = APIClient(
        client=ollama,
        tts=tts,
        llm_settings=codex_routing_settings(tmp_path),
        providers={"openai-codex": codex},
        connectivity=Connectivity.OFFLINE,
        retry_wait=0,
        language="en",
    )
    try:
        assert client.talk("Answer locally") == "Local."
        assert len(ollama.calls) == 1
        assert tts.spoken == ["Local."]
        assert runtime.account_calls == 0
        assert runtime.turn_calls == 0
        assert runtime.inject_calls == 0
    finally:
        client.close()
        codex.close()
