from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

import config
from api.api_client import APIClient, APIClientError
from api.catalog import ModelCatalog
from api.metrics import SafeMetricsRecorder
from api.providers.contracts import (
    ChatProvider,
    ChatRequest,
    Completed,
    CompletionMetadata,
    ContentOrigin,
    ErrorCategory,
    FinishReason,
    ProviderCapabilities,
    ProviderError,
    ProviderIdentity,
    Role,
    TextDelta,
)
from api.routing import Connectivity
from api.streaming import CancellationController


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


class FakeRemoteProvider:
    def __init__(self, streams: list[object]) -> None:
        self.streams = iter(streams)
        self.calls: list[ChatRequest] = []
        self.prepare_calls = 0
        self.closed = False

    @property
    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            "remote",
            "https://provider.invalid/v1",
            remote=True,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def stream(
        self,
        request: ChatRequest,
        *,
        cancellation: object | None = None,
    ) -> Iterable[object]:
        assert cancellation is not None
        self.calls.append(request)
        stream = next(self.streams)
        if isinstance(stream, Exception):
            raise stream
        return stream  # type: ignore[return-value]

    def warm_up(self, model: str) -> None:
        raise AssertionError(f"remote warm-up is forbidden: {model}")

    def prepare(self) -> None:
        self.prepare_calls += 1

    def close(self) -> None:
        self.closed = True


class FakeNetworkMonitor:
    def __init__(self, state: Connectivity) -> None:
        self.state = state
        self.start_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def connectivity(self) -> Connectivity:
        return self.state

    def snapshot(self) -> object:
        return {"connectivity": self.state.value}


def completed(provider: str, model: str) -> Completed:
    return Completed(
        CompletionMetadata(
            provider=provider,
            requested_model=model,
            finish_reason=FinishReason.STOP,
        )
    )


def hybrid_settings(
    tmp_path: Path,
    *,
    allow_transcripts: bool = True,
    allow_context: bool = True,
    budget_enabled: bool = False,
) -> config.LLMSettings:
    return config.LLMSettings(
        routing_file=tmp_path / "routing.toml",
        routing_policy="remote_first",
        remote_enabled=True,
        privacy=config.LLMPrivacySettings(
            default="remote_allowed",
            allow_remote_transcripts=allow_transcripts,
            allow_remote_context=allow_context,
        ),
        budget=config.LLMBudgetSettings(enabled=budget_enabled),
        talk=config.LLMModeSettings(
            candidates=("remote-talk", "local-talk"),
            max_output_tokens=64,
        ),
        providers=(
            config.LLMProviderSettings(
                name="remote",
                adapter="openai_chat_sse",
                endpoint="https://provider.invalid/v1",
                locality="remote",
                api_key_env="REMOTE_API_KEY",
            ),
        ),
        targets=(
            config.LLMTargetSettings(
                name="remote-talk",
                provider="remote",
                model="remote-model",
                max_output_words=50,
            ),
            config.LLMTargetSettings(
                name="local-talk",
                provider="ollama",
                model="local-model",
                max_output_words=20,
            ),
        ),
    )


def make_client(
    tmp_path: Path,
    remote: ChatProvider,
    *,
    allow_transcripts: bool = True,
    budget_enabled: bool = False,
    metrics: SafeMetricsRecorder | None = None,
) -> tuple[APIClient, FakeOllamaClient, FakeTTS]:
    local = FakeOllamaClient()
    tts = FakeTTS()
    client = APIClient(
        client=local,
        tts=tts,
        llm_settings=hybrid_settings(
            tmp_path,
            allow_transcripts=allow_transcripts,
            budget_enabled=budget_enabled,
        ),
        language="en",
        providers={"remote": remote},
        connectivity=Connectivity.ONLINE,
        retry_wait=0,
        metrics=metrics,
    )
    return client, local, tts


def test_remote_route_receives_only_canonical_authorized_messages(
    tmp_path: Path,
) -> None:
    remote = FakeRemoteProvider([[TextDelta("Remote."), completed("remote", "remote-model")]])
    client, local, tts = make_client(tmp_path, remote)

    assert client.talk("Emilia, answer") == "Remote."

    assert local.calls == []
    assert tts.spoken == ["Remote."]
    assert len(remote.calls) == 1
    request = remote.calls[0]
    assert request.remote_authorized
    assert request.model == "remote-model"
    assert request.messages[0].role is Role.SYSTEM
    assert request.messages[0].origin is ContentOrigin.STATIC_INSTRUCTION
    assert "You are Emilia" in request.messages[0].content
    assert "at most 50 words" in request.messages[0].content
    assert "at most 20 words" not in request.messages[0].content
    assert request.messages[-1].role is Role.USER
    assert request.messages[-1].content == "Emilia, answer"


def test_runtime_health_snapshot_reports_configured_local_and_remote_circuits(
    tmp_path: Path,
) -> None:
    client, _local, _tts = make_client(tmp_path, FakeRemoteProvider([]))

    healthy = client._runtime_health_snapshot()
    providers = {(item["provider"], item["model"]): item for item in healthy["providers"]}
    assert healthy["status"] == "healthy"
    assert healthy["local_available"] is True
    assert healthy["remote_available"] is True
    assert providers[("ollama", "local-model")]["circuit_state"] == "available"
    assert providers[("remote", "remote-model")]["circuit_state"] == "available"

    for _ in range(client.llm_settings.health.failures_to_open):
        client.health.record_failure("remote/remote-model", ErrorCategory.CONNECTIVITY)

    degraded_remote = client._runtime_health_snapshot()
    assert degraded_remote["status"] == "healthy"
    assert degraded_remote["local_available"] is True
    assert degraded_remote["remote_available"] is False
    remote = next(item for item in degraded_remote["providers"] if item["provider"] == "remote")
    assert remote["circuit_state"] == "cooldown"
    assert remote["available"] is False


def test_static_hybrid_instruction_is_reused_between_requests(tmp_path: Path) -> None:
    remote = FakeRemoteProvider(
        [
            [TextDelta("First."), completed("remote", "remote-model")],
            [TextDelta("Second."), completed("remote", "remote-model")],
        ]
    )
    llm = hybrid_settings(tmp_path)
    llm = replace(
        llm,
        targets=(replace(llm.targets[0], max_output_words=None), llm.targets[1]),
    )
    client = APIClient(
        client=FakeOllamaClient(),
        tts=FakeTTS(),
        llm_settings=llm,
        language="en",
        providers={"remote": remote},
        connectivity=Connectivity.ONLINE,
        retry_wait=0,
    )

    assert client.talk("first") == "First."
    assert client.talk("second") == "Second."

    assert remote.calls[0].messages[0] is remote.calls[1].messages[0]


def test_codex_to_local_route_change_preserves_canonical_history(tmp_path: Path) -> None:
    remote = FakeRemoteProvider(
        [[TextDelta("Mercury, Venus, Earth."), completed("remote", "remote-model")]]
    )
    client, local, _tts = make_client(tmp_path, remote)

    assert client.talk("Name three planets") == "Mercury, Venus, Earth."
    client.connectivity = Connectivity.OFFLINE
    assert client.talk("Only discuss the second one") == "Local."

    assert local.calls[0]["messages"][-3:] == [
        {"role": "user", "content": "Name three planets"},
        {"role": "assistant", "content": "Mercury, Venus, Earth."},
        {"role": "user", "content": "Only discuss the second one"},
    ]


def test_local_to_codex_route_change_preserves_canonical_history(tmp_path: Path) -> None:
    remote = FakeRemoteProvider([[TextDelta("Venus."), completed("remote", "remote-model")]])
    client, _local, _tts = make_client(tmp_path, remote)
    client.connectivity = Connectivity.OFFLINE

    assert client.talk("Name three planets") == "Local."
    client.connectivity = Connectivity.ONLINE
    assert client.talk("Only discuss the second one") == "Venus."

    messages = remote.calls[0].messages[-3:]
    assert [(message.role, message.content) for message in messages] == [
        (Role.USER, "Name three planets"),
        (Role.ASSISTANT, "Local."),
        (Role.USER, "Only discuss the second one"),
    ]


def test_codex_local_codex_sequence_keeps_one_logical_history(tmp_path: Path) -> None:
    remote = FakeRemoteProvider(
        [
            [TextDelta("Mercury, Venus, Earth."), completed("remote", "remote-model")],
            [TextDelta("About 225 days."), completed("remote", "remote-model")],
        ]
    )
    client, _local, _tts = make_client(tmp_path, remote)

    assert client.talk("Name three planets") == "Mercury, Venus, Earth."
    client.connectivity = Connectivity.OFFLINE
    assert client.talk("Only discuss the second one") == "Local."
    client.connectivity = Connectivity.ONLINE
    assert client.talk("How long is its year?") == "About 225 days."

    messages = remote.calls[1].messages[-5:]
    assert [(message.role, message.content) for message in messages] == [
        (Role.USER, "Name three planets"),
        (Role.ASSISTANT, "Mercury, Venus, Earth."),
        (Role.USER, "Only discuss the second one"),
        (Role.ASSISTANT, "Local."),
        (Role.USER, "How long is its year?"),
    ]


def test_transcript_privacy_denial_falls_back_to_local(tmp_path: Path) -> None:
    remote = FakeRemoteProvider([[TextDelta("Remote."), completed("remote", "remote-model")]])
    client, local, tts = make_client(
        tmp_path,
        remote,
        allow_transcripts=False,
    )

    assert client.talk("Emilia, stay private") == "Local."

    assert remote.calls == []
    assert len(local.calls) == 1
    assert tts.spoken == ["Local."]


def test_local_only_turn_cannot_egress_as_later_remote_history(tmp_path: Path) -> None:
    remote = FakeRemoteProvider([[TextDelta("Remote."), completed("remote", "remote-model")]])
    client, local, _tts = make_client(tmp_path, remote)

    assert client.talk("private fact", privacy="local_only") == "Local."
    assert client.talk("refer to that fact", privacy="remote_allowed") == "Local."

    assert remote.calls == []
    assert len(local.calls) == 2
    assert local.calls[1]["messages"][-3:] == [
        {"role": "user", "content": "private fact"},
        {"role": "assistant", "content": "Local."},
        {"role": "user", "content": "refer to that fact"},
    ]


def test_local_document_taint_survives_into_later_assistant_history(
    tmp_path: Path,
) -> None:
    remote = FakeRemoteProvider(
        [[TextDelta("Remote."), completed("remote", "remote-model")]]
    )
    client, local, _tts = make_client(tmp_path, remote)

    assert (
        client.talk(
            "summarize it",
            context="local document contents",
            context_origin=ContentOrigin.LOCAL_DOCUMENT,
            privacy="remote_allowed",
        )
        == "Local."
    )
    assert client.talk("what was the conclusion?", privacy="remote_allowed") == "Local."

    assert remote.calls == []
    assert len(local.calls) == 2


def test_unredacted_remote_redacted_turn_stays_ineligible_for_later_egress(
    tmp_path: Path,
) -> None:
    remote = FakeRemoteProvider(
        [[TextDelta("Remote."), completed("remote", "remote-model")]]
    )
    client, local, _tts = make_client(tmp_path, remote)

    assert client.talk("unredacted secret", privacy="remote_redacted") == "Local."
    assert client.talk("repeat it", privacy="remote_allowed") == "Local."

    assert remote.calls == []
    assert len(local.calls) == 2


def test_network_monitor_blocks_remote_before_provider_execution(
    tmp_path: Path,
) -> None:
    remote = FakeRemoteProvider([[TextDelta("Remote."), completed("remote", "remote-model")]])
    local = FakeOllamaClient()
    monitor = FakeNetworkMonitor(Connectivity.OFFLINE)
    client = APIClient(
        client=local,
        tts=FakeTTS(),
        llm_settings=hybrid_settings(tmp_path),
        providers={"remote": remote},
        network_monitor=monitor,
        retry_wait=0,
    )

    assert client.talk("hello") == "Local."
    assert monitor.start_calls == 1
    assert remote.calls == []
    assert len(local.calls) == 1
    assert client.network_snapshot == {"connectivity": "offline"}


def test_precompiled_planner_still_reads_provider_health_live(tmp_path: Path) -> None:
    remote = FakeRemoteProvider([[TextDelta("Remote."), completed("remote", "remote-model")]])
    client, local, tts = make_client(tmp_path, remote)
    planner = client._route_planners["talk"]

    for _ in range(client.llm_settings.health.failures_to_open):
        client.health.record_failure(
            "remote/remote-model",
            ErrorCategory.CONNECTIVITY,
        )

    assert client.talk("hello") == "Local."
    assert client._route_planners["talk"] is planner
    assert remote.calls == []
    assert len(local.calls) == 1
    assert tts.spoken == ["Local."]


def test_runtime_health_snapshot_reads_configured_circuits_without_provider_io(
    tmp_path: Path,
) -> None:
    remote = FakeRemoteProvider([[TextDelta("unused"), completed("remote", "remote-model")]])
    client, local, _tts = make_client(tmp_path, remote)

    initial = client._runtime_health_snapshot()
    for _ in range(client.llm_settings.health.failures_to_open):
        client.health.record_failure("remote/remote-model", ErrorCategory.CONNECTIVITY)
    blocked = client._runtime_health_snapshot()

    assert initial["status"] == "healthy"
    assert initial["local_available"] is True
    assert initial["remote_available"] is True
    assert blocked["status"] == "healthy"
    assert blocked["local_available"] is True
    assert blocked["remote_available"] is False
    providers = blocked["providers"]
    assert isinstance(providers, list)
    provider_states = {(item["provider"], item["model"]): item for item in providers}
    assert provider_states[("remote", "remote-model")]["circuit_state"] == "cooldown"
    assert provider_states[("remote", "remote-model")]["available"] is False
    assert remote.calls == []
    assert local.calls == []


def test_unknown_context_provenance_never_leaves_the_device(tmp_path: Path) -> None:
    remote = FakeRemoteProvider([[TextDelta("Remote."), completed("remote", "remote-model")]])
    client, local, _tts = make_client(tmp_path, remote)

    assert client.talk("Emilia, use this", context="retrieved local passage") == "Local."

    assert remote.calls == []
    assert len(local.calls) == 1


def test_remote_redacted_requires_an_explicit_redaction_attestation(
    tmp_path: Path,
) -> None:
    remote = FakeRemoteProvider(
        [[TextDelta("Redacted remote."), completed("remote", "remote-model")]]
    )
    llm = replace(
        hybrid_settings(tmp_path),
        privacy=config.LLMPrivacySettings(
            default="remote_redacted",
            allow_remote_transcripts=True,
            allow_remote_context=True,
        ),
    )
    local = FakeOllamaClient()
    client = APIClient(
        client=local,
        tts=FakeTTS(),
        llm_settings=llm,
        language="en",
        providers={"remote": remote},
        connectivity=Connectivity.ONLINE,
        retry_wait=0,
    )

    assert client.talk("Emilia, private value") == "Local."
    assert remote.calls == []
    client.reset_conversation(reason="privacy_boundary")

    assert (
        client.talk(
            "Emilia, [redacted]",
            message_redacted=True,
        )
        == "Redacted remote."
    )
    assert remote.calls[0].messages[-1].redacted is True


def test_remote_failure_before_speech_falls_back_to_ollama(tmp_path: Path) -> None:
    error = ProviderError(
        ErrorCategory.CONNECT_TIMEOUT,
        "remote timeout",
        provider="remote",
        model="remote-model",
        transmitted=False,
    )
    remote = FakeRemoteProvider([error])
    metrics = SafeMetricsRecorder()
    client, local, tts = make_client(tmp_path, remote, metrics=metrics)

    assert client.talk("Emilia, answer") == "Local."

    assert len(remote.calls) == 1
    assert len(local.calls) == 1
    assert tts.spoken == ["Local."]
    local_messages = local.calls[0]["messages"]
    assert isinstance(local_messages, list)
    assert local_messages[0]["role"] == "system"
    assert "You are Emilia" in local_messages[0]["content"]
    assert "at most 20 words" in local_messages[0]["content"]
    assert "at most 50 words" not in local_messages[0]["content"]
    assert local_messages[-1] == {"role": "user", "content": "Emilia, answer"}
    terminal = [event for event in metrics.snapshot() if event.event == "llm_request_succeeded"]
    assert len(terminal) == 1
    assert terminal[0].route == "local-talk"
    assert terminal[0].fallback_count == 1
    assert terminal[0].fallback_from == "remote-talk"
    assert terminal[0].fallback_to == "local-talk"
    assert terminal[0].fallback_cause == ErrorCategory.CONNECT_TIMEOUT.value


def test_terminal_failure_after_fallback_is_attributed_to_final_route(
    tmp_path: Path,
) -> None:
    class FailingLocal(FakeOllamaClient):
        def chat(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            raise OSError("local unavailable")

    remote_error = ProviderError(
        ErrorCategory.CONNECT_TIMEOUT,
        "remote timeout",
        provider="remote",
        model="remote-model",
        transmitted=False,
    )
    metrics = SafeMetricsRecorder()
    client = APIClient(
        client=FailingLocal(),
        tts=FakeTTS(),
        llm_settings=hybrid_settings(tmp_path),
        language="en",
        providers={"remote": FakeRemoteProvider([remote_error])},
        connectivity=Connectivity.ONLINE,
        retry_attempts=1,
        retry_wait=0,
        metrics=metrics,
    )

    with pytest.raises(APIClientError):
        client.talk("Emilia, answer")

    terminal = [event for event in metrics.snapshot() if event.event == "llm_request_failed"]
    assert len(terminal) == 1
    assert terminal[0].provider == "ollama"
    assert terminal[0].route == "local-talk"
    assert terminal[0].locality == "local"
    assert terminal[0].retry_count == 0
    assert terminal[0].fallback_count == 1
    assert terminal[0].fallback_from == "remote-talk"
    assert terminal[0].fallback_to == "local-talk"
    assert terminal[0].fallback_cause == ErrorCategory.CONNECT_TIMEOUT.value


def test_adaptive_remote_failure_falls_directly_to_capped_local_route(
    tmp_path: Path,
) -> None:
    error = ProviderError(
        ErrorCategory.CONNECT_TIMEOUT,
        "remote timeout",
        provider="remote",
        model="gpt-5.6-luna",
        transmitted=False,
    )
    remote = FakeRemoteProvider([error])
    base = hybrid_settings(tmp_path)
    llm = replace(
        base,
        talk=config.LLMModeSettings(
            candidates=("luna", "terra", "sol", "local-talk"),
            max_output_tokens=128,
        ),
        targets=(
            config.LLMTargetSettings(
                name="luna",
                provider="remote",
                model="gpt-5.6-luna",
                min_complexity_score=0,
            ),
            config.LLMTargetSettings(
                name="terra",
                provider="remote",
                model="gpt-5.6-terra",
                min_complexity_score=3,
            ),
            config.LLMTargetSettings(
                name="sol",
                provider="remote",
                model="gpt-5.6-sol",
                min_complexity_score=5,
            ),
            config.LLMTargetSettings(
                name="local-talk",
                provider="ollama",
                model="local-model",
                max_output_tokens=40,
            ),
        ),
    )
    local = FakeOllamaClient()
    client = APIClient(
        client=local,
        tts=FakeTTS(),
        llm_settings=llm,
        providers={"remote": remote},
        connectivity=Connectivity.ONLINE,
        retry_wait=0,
    )

    assert client.talk("hello") == "Local."

    assert [call.model for call in remote.calls] == ["gpt-5.6-luna"]
    assert len(local.calls) == 1
    assert local.calls[0]["options"]["num_predict"] == 40


def test_remote_prepare_is_background_idempotent_and_sends_no_prompt(
    tmp_path: Path,
) -> None:
    remote = FakeRemoteProvider([])
    llm = config.LLMSettings(
        routing_file=tmp_path / "routing.toml",
        routing_policy="remote_first",
        remote_enabled=True,
        privacy=config.LLMPrivacySettings(
            default="remote_allowed",
            allow_remote_transcripts=True,
        ),
        budget=config.LLMBudgetSettings(enabled=False),
        talk=config.LLMModeSettings(candidates=("codex-talk",)),
        providers=(
            config.LLMProviderSettings(
                name="openai-codex",
                adapter="codex_app_server",
                endpoint="stdio://codex",
                locality="remote",
            ),
        ),
        targets=(
            config.LLMTargetSettings(
                name="codex-talk",
                provider="openai-codex",
                model="gpt-5.6-luna",
            ),
        ),
    )
    client = APIClient(
        client=FakeOllamaClient(),
        tts=FakeTTS(),
        llm_settings=llm,
        providers={"openai-codex": remote},
        connectivity=Connectivity.ONLINE,
    )

    first = client.prepare_remote_async()
    assert first is not None
    first.join(timeout=1)
    second = client.prepare_remote_async()

    assert second is first
    assert remote.prepare_calls == 1
    assert remote.calls == []


def test_mode_visible_token_timeout_reaches_remote_request(tmp_path: Path) -> None:
    remote = FakeRemoteProvider([[TextDelta("Remote."), completed("remote", "remote-model")]])
    llm = hybrid_settings(tmp_path)
    llm = replace(
        llm,
        talk=replace(llm.talk, first_visible_token_seconds=7.5),
    )
    client = APIClient(
        client=FakeOllamaClient(),
        tts=FakeTTS(),
        llm_settings=llm,
        providers={"remote": remote},
        connectivity=Connectivity.ONLINE,
        retry_wait=0,
    )

    assert client.talk("hello") == "Remote."
    assert remote.calls[0].timeouts.first_token_seconds == 7.5


def test_committed_codex_profile_selects_luna_terra_and_sol() -> None:
    routing = (
        Path(__file__).resolve().parents[1] / "examples" / ("llm-routing.codex-subscription.toml")
    )
    llm = config.load_llm_settings(routing)
    llm = replace(
        llm,
        observability=config.LLMObservabilitySettings(metrics_enabled=False),
        privacy=replace(llm.privacy, allow_remote_context=True),
    )
    remote = FakeRemoteProvider(
        [
            [
                TextDelta("Luna."),
                completed("openai-codex", "gpt-5.6-luna"),
            ],
            [
                TextDelta("Terra."),
                completed("openai-codex", "gpt-5.6-terra"),
            ],
            [
                TextDelta("Sol."),
                completed("openai-codex", "gpt-5.6-sol"),
            ],
        ]
    )
    client = APIClient(
        client=FakeOllamaClient(),
        tts=FakeTTS(),
        llm_settings=llm,
        language="en",
        providers={"openai-codex": remote},
        connectivity=Connectivity.ONLINE,
        retry_wait=0,
    )
    try:
        assert client.talk("hello") == "Luna."
        assert client.talk("explain efficiency") == "Terra."
        assert (
            client.talk(
                "explain the complete strategy",
                request_options={"complex": True},
            )
            == "Sol."
        )
    finally:
        client.close()

    assert [call.model for call in remote.calls] == [
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    ]


def test_remote_failure_after_speech_never_calls_ollama(tmp_path: Path) -> None:
    def interrupted() -> Iterable[object]:
        yield TextDelta("Remote first.")
        raise ProviderError(
            ErrorCategory.READ_TIMEOUT,
            "stream interrupted",
            provider="remote",
            model="remote-model",
            retryable_same_provider=True,
            transmitted=True,
        )

    remote = FakeRemoteProvider([interrupted()])
    client, local, tts = make_client(tmp_path, remote)

    with pytest.raises(APIClientError, match="after speech output began"):
        client.talk("Emilia, answer")

    assert tts.spoken == ["Remote first."]
    assert local.calls == []


def test_missing_budget_catalog_blocks_remote_and_uses_local(tmp_path: Path) -> None:
    remote = FakeRemoteProvider([[TextDelta("Remote."), completed("remote", "remote-model")]])
    client, local, _tts = make_client(
        tmp_path,
        remote,
        budget_enabled=True,
    )

    assert client.talk("Emilia, answer") == "Local."

    assert remote.calls == []
    assert len(local.calls) == 1


def test_non_loopback_ollama_is_not_implicitly_treated_as_local() -> None:
    local = FakeOllamaClient()
    client = APIClient(
        api_url="http://ollama.lan:11434",
        client=local,
        tts=FakeTTS(),
        retry_wait=0,
    )

    with pytest.raises(APIClientError, match="privacy_blocked"):
        client.talk("Emilia, keep this private")

    assert local.calls == []


def test_non_loopback_ollama_cannot_bypass_policy_during_warm_up() -> None:
    local = FakeOllamaClient()
    client = APIClient(
        api_url="http://ollama.lan:11434",
        client=local,
        tts=FakeTTS(),
        retry_wait=0,
    )

    with pytest.raises(APIClientError, match="cannot be warmed up"):
        client.warm_up()

    assert local.calls == []


def test_pre_cancelled_request_never_reaches_provider() -> None:
    local = FakeOllamaClient()
    cancellation = CancellationController()
    cancellation.cancel()
    client = APIClient(
        client=local,
        tts=FakeTTS(),
        retry_wait=0,
    )

    with pytest.raises(APIClientError, match="cancelled"):
        client.talk("Emilia, stop", cancellation=cancellation)

    assert local.calls == []


def test_emergency_switch_restores_local_route_for_remote_only_mode(
    tmp_path: Path,
) -> None:
    base = hybrid_settings(tmp_path)
    llm = replace(
        base,
        emergency_local_only=True,
        allowlist=("remote",),
        talk=replace(base.talk, candidates=("remote-talk",)),
    )
    local = FakeOllamaClient()
    remote = FakeRemoteProvider([[TextDelta("Remote."), completed("remote", "remote-model")]])
    client = APIClient(
        client=local,
        tts=FakeTTS(),
        llm_settings=llm,
        providers={"remote": remote},
        connectivity=Connectivity.ONLINE,
        retry_wait=0,
    )

    assert client.talk("Emilia, rollback") == "Local."
    assert len(local.calls) == 1
    assert remote.calls == []


def test_matching_ollama_endpoint_can_be_explicitly_trusted(
    tmp_path: Path,
) -> None:
    llm = config.LLMSettings(
        routing_file=tmp_path / "routing.toml",
        routing_policy="local_only",
        providers=(
            config.LLMProviderSettings(
                name="ollama",
                adapter="ollama",
                endpoint="http://ollama.lan:11434",
                locality="trusted_lan",
            ),
        ),
        talk=config.LLMModeSettings(candidates=("local-talk",)),
        targets=(
            config.LLMTargetSettings(
                name="local-talk",
                provider="ollama",
                model="local-model",
            ),
        ),
    )
    local = FakeOllamaClient()
    client = APIClient(
        api_url="http://ollama.lan:11434",
        client=local,
        tts=FakeTTS(),
        llm_settings=llm,
        retry_wait=0,
    )

    assert client.talk("Emilia, answer") == "Local."
    assert len(local.calls) == 1

    client.warm_up()
    assert local.calls[-1] == {
        "model": "local-model",
        "messages": [{"role": "user", "content": ""}],
        "stream": False,
    }


def test_catalog_limits_cap_more_permissive_toml_limits(tmp_path: Path) -> None:
    llm = hybrid_settings(tmp_path)
    remote_target = replace(
        llm.targets[0],
        context_window=9999,
        max_output_tokens=999,
    )
    llm = replace(llm, targets=(remote_target, llm.targets[1]))
    catalog = ModelCatalog.from_mapping(
        {
            "schema_version": 1,
            "catalog_revision": "test",
            "verified_on": "2026-07-27",
            "expires_on": "2026-08-27",
            "models": [
                {
                    "id": "groq/example",
                    "provider": "remote",
                    "model": "remote-model",
                    "input_per_million_usd": "1",
                    "output_per_million_usd": "1",
                    "context_window": 100,
                    "max_output_tokens": 32,
                    "free_tier": False,
                }
            ],
        },
        today=date(2026, 7, 27),
    )
    llm = replace(
        llm,
        targets=(replace(remote_target, catalog_id="groq/example"), llm.targets[1]),
    )
    client = APIClient(
        client=FakeOllamaClient(),
        tts=FakeTTS(),
        llm_settings=llm,
        providers={"remote": FakeRemoteProvider([])},
        model_catalog=catalog,
        connectivity=Connectivity.ONLINE,
    )

    remote_execution = client._execution_targets["talk"][0]
    assert remote_execution.route.context_window == 100
    assert remote_execution.route.max_output_tokens == 32
    assert remote_execution.max_output_tokens == 32
