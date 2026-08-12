from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import config
from api.api_client import APIClient
from api.providers.codex_app_server import CodexAppServerAdapter
from api.providers.codex_session import codex_child_environment, copy_chatgpt_auth
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
    Timeouts,
)
from api.routing import Connectivity
from api.streaming import CancellationController


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
        self.account_checks = 0
        self.closed = False

    def account_kind(self) -> str | None:
        self.account_checks += 1
        return self.kind

    def start_turn(self, **kwargs: Any) -> FakeTurn:
        self.started = kwargs
        return self.turn

    def close(self) -> None:
        self.closed = True


class BlockingAuthRuntime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__("chatgpt", [])
        self.auth_entered = threading.Event()
        self.auth_release = threading.Event()

    def account_kind(self) -> str | None:
        self.account_checks += 1
        self.auth_entered.set()
        self.auth_release.wait(timeout=2)
        return self.kind


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
    assert codex_child_environment() == {
        "OPENAI_API_KEY": "",
        "CODEX_API_KEY": "",
    }


def test_remote_context_disabled_emits_an_explicit_startup_warning(caplog) -> None:
    caplog.set_level("WARNING")

    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=FakeRuntime("chatgpt", []),
        allow_remote_context=False,
    )

    assert "event=remote_context_disabled" in caplog.text
    assert "fresh_thread" in caplog.text
    provider.close()


def test_isolated_codex_home_copies_auth_but_not_user_configuration(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text('{"auth_mode":"chatgpt"}', encoding="utf-8")
    (source / "config.toml").write_text("[mcp_servers.unsafe]", encoding="utf-8")
    isolated = tmp_path / "isolated"

    copy_chatgpt_auth(source, isolated)

    assert (isolated / "auth.json").read_text(encoding="utf-8") == ('{"auth_mode":"chatgpt"}')
    assert not (isolated / "config.toml").exists()
    child_env = codex_child_environment(isolated)
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


def test_prepare_is_idempotent_and_never_starts_an_inference_turn() -> None:
    runtime = FakeRuntime(
        "chatgpt",
        [
            notification("item/agentMessage/delta", {"delta": "Ciao!"}),
            notification("turn/completed", {"turn": {"status": "completed"}}),
        ],
    )
    provider = CodexAppServerAdapter("openai-codex", runtime=runtime)

    provider.prepare()
    provider.prepare()

    assert runtime.account_checks == 1
    assert runtime.started is None

    list(provider.stream(request()))
    assert runtime.account_checks == 1
    assert runtime.started is not None


def test_prepare_rejects_non_chatgpt_auth_before_transmission() -> None:
    runtime = FakeRuntime("apiKey", [])
    provider = CodexAppServerAdapter("openai-codex", runtime=runtime)

    with pytest.raises(ProviderError) as captured:
        provider.prepare()

    assert captured.value.category is ErrorCategory.AUTHENTICATION
    assert captured.value.transmitted is False
    assert runtime.started is None


def test_timeout_while_prepare_holds_auth_lock_never_starts_orphan_turn() -> None:
    runtime = BlockingAuthRuntime()
    provider = CodexAppServerAdapter("openai-codex", runtime=runtime)
    prepare_errors: list[BaseException] = []

    def prepare() -> None:
        try:
            provider.prepare()
        except BaseException as error:
            prepare_errors.append(error)

    prepare_thread = threading.Thread(target=prepare)
    prepare_thread.start()
    assert runtime.auth_entered.wait(timeout=1)

    with pytest.raises(ProviderError) as captured:
        list(
            provider.stream(
                request(
                    timeouts=Timeouts(
                        connect_seconds=0.05,
                        first_token_seconds=0.05,
                        read_seconds=1,
                        total_seconds=1,
                    )
                )
            )
        )

    assert captured.value.category is ErrorCategory.CONNECT_TIMEOUT
    runtime.auth_release.set()
    prepare_thread.join(timeout=1)
    deadline = time.monotonic() + 1
    while runtime.account_checks < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    assert runtime.started is None
    assert prepare_errors


def test_hidden_reasoning_does_not_reset_visible_first_token_timeout() -> None:
    def reasoning_only() -> Any:
        for _ in range(10):
            time.sleep(0.02)
            yield notification("item/reasoning/textDelta", {"delta": "hidden"})
        yield notification("turn/completed", {"turn": {"status": "completed"}})

    runtime = FakeRuntime("chatgpt", reasoning_only())
    provider = CodexAppServerAdapter("openai-codex", runtime=runtime)

    with pytest.raises(ProviderError) as captured:
        list(
            provider.stream(
                request(
                    timeouts=Timeouts(
                        connect_seconds=0.5,
                        first_token_seconds=0.05,
                        read_seconds=0.5,
                        total_seconds=1,
                    )
                )
            )
        )

    assert captured.value.category is ErrorCategory.FIRST_TOKEN_TIMEOUT
    assert runtime.turn.interrupted


def test_cancellation_waits_for_codex_worker_to_finish_unwinding() -> None:
    class BlockingTurn:
        id = "blocking-turn"
        thread_id = "blocking-thread"

        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()
            self.exited = threading.Event()
            self.interrupted = False

        def stream(self):
            self.entered.set()
            try:
                assert self.release.wait(timeout=2)
            finally:
                self.exited.set()
            if False:
                yield None

        def interrupt(self) -> None:
            self.interrupted = True
            self.release.set()

    class BlockingRuntime:
        def __init__(self) -> None:
            self.turn = BlockingTurn()

        def account_kind(self) -> str:
            return "chatgpt"

        def start_turn(self, **_kwargs: Any) -> BlockingTurn:
            return self.turn

        def close(self) -> None:
            self.turn.release.set()

    runtime = BlockingRuntime()
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        interrupt_ack_timeout_seconds=0.5,
    )
    cancellation = CancellationController()
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            list(provider.stream(request(), cancellation=cancellation))
        except BaseException as error:
            errors.append(error)

    consumer = threading.Thread(target=consume)
    consumer.start()
    assert runtime.turn.entered.wait(timeout=1)

    cancellation.cancel()
    consumer.join(timeout=1)

    assert not consumer.is_alive()
    assert runtime.turn.interrupted
    assert runtime.turn.exited.is_set()
    assert len(errors) == 1
    assert isinstance(errors[0], ProviderError)
    assert errors[0].category is ErrorCategory.CANCELLED


def test_blocking_interrupt_rpc_cannot_defeat_cancellation_timeout() -> None:
    class HungInterruptTurn:
        id = "hung-interrupt-turn"
        thread_id = "hung-interrupt-thread"

        def __init__(self) -> None:
            self.stream_entered = threading.Event()
            self.stream_release = threading.Event()
            self.interrupt_entered = threading.Event()
            self.interrupt_release = threading.Event()

        def stream(self):
            self.stream_entered.set()
            self.stream_release.wait(timeout=2)
            if False:
                yield None

        def interrupt(self) -> None:
            self.interrupt_entered.set()
            self.interrupt_release.wait(timeout=2)

    class HungInterruptRuntime:
        def __init__(self) -> None:
            self.turn = HungInterruptTurn()

        def account_kind(self) -> str:
            return "chatgpt"

        def start_turn(self, **_kwargs: Any) -> HungInterruptTurn:
            return self.turn

        def close(self) -> None:
            self.turn.stream_release.set()

    runtime = HungInterruptRuntime()
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        interrupt_ack_timeout_seconds=0.05,
    )
    cancellation = CancellationController()
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            list(provider.stream(request(), cancellation=cancellation))
        except BaseException as error:
            errors.append(error)

    consumer = threading.Thread(target=consume)
    consumer.start()
    assert runtime.turn.stream_entered.wait(timeout=1)

    started = time.monotonic()
    cancellation.cancel()
    consumer.join(timeout=0.5)
    elapsed = time.monotonic() - started

    assert not consumer.is_alive()
    assert elapsed < 0.4
    assert runtime.turn.interrupt_entered.wait(timeout=0.2)
    assert len(errors) == 1
    assert isinstance(errors[0], ProviderError)
    assert errors[0].category is ErrorCategory.CANCELLED
    runtime.turn.interrupt_release.set()


def test_cancelled_blocking_runtime_factory_does_not_poison_follow_up() -> None:
    factory_entered = threading.Event()
    factory_release = threading.Event()
    first = FakeRuntime("chatgpt", [])
    healthy = FakeRuntime(
        "chatgpt",
        [
            notification("item/agentMessage/delta", {"delta": "Recovered"}),
            notification("turn/completed", {"turn": {"status": "completed"}}),
        ],
    )
    factory_calls = 0

    def factory() -> FakeRuntime:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            factory_entered.set()
            factory_release.wait(timeout=2)
            return first
        return healthy

    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime_factory=factory,
        interrupt_ack_timeout_seconds=0.05,
    )
    cancellation = CancellationController()
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            list(provider.stream(request(), cancellation=cancellation))
        except BaseException as error:
            errors.append(error)

    consumer = threading.Thread(target=consume)
    consumer.start()
    assert factory_entered.wait(timeout=1)
    cancellation.cancel()
    consumer.join(timeout=0.5)

    assert not consumer.is_alive()
    assert isinstance(errors[0], ProviderError)
    assert errors[0].category is ErrorCategory.CANCELLED
    assert [event.text for event in provider.stream(request()) if isinstance(event, TextDelta)] == [
        "Recovered"
    ]
    factory_release.set()


def test_cancelled_blocking_account_check_does_not_poison_follow_up() -> None:
    auth_entered = threading.Event()
    auth_release = threading.Event()

    class FirstRuntime(FakeRuntime):
        def account_kind(self) -> str | None:
            self.account_checks += 1
            auth_entered.set()
            auth_release.wait(timeout=2)
            return "chatgpt"

    first = FirstRuntime("chatgpt", [])
    healthy = FakeRuntime(
        "chatgpt",
        [
            notification("item/agentMessage/delta", {"delta": "Recovered"}),
            notification("turn/completed", {"turn": {"status": "completed"}}),
        ],
    )
    runtimes = iter([first, healthy])
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime_factory=lambda: next(runtimes),
        interrupt_ack_timeout_seconds=0.05,
    )
    cancellation = CancellationController()
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            list(provider.stream(request(), cancellation=cancellation))
        except BaseException as error:
            errors.append(error)

    consumer = threading.Thread(target=consume)
    consumer.start()
    assert auth_entered.wait(timeout=1)
    cancellation.cancel()
    consumer.join(timeout=0.5)

    assert not consumer.is_alive()
    assert isinstance(errors[0], ProviderError)
    assert errors[0].category is ErrorCategory.CANCELLED
    assert [event.text for event in provider.stream(request()) if isinstance(event, TextDelta)] == [
        "Recovered"
    ]
    auth_release.set()


def test_close_does_not_wait_for_blocked_account_validation() -> None:
    runtime = BlockingAuthRuntime()
    provider = CodexAppServerAdapter("openai-codex", runtime=runtime)
    errors: list[BaseException] = []

    def prepare() -> None:
        try:
            provider.prepare()
        except BaseException as error:
            errors.append(error)

    prepare_thread = threading.Thread(target=prepare)
    prepare_thread.start()
    assert runtime.auth_entered.wait(timeout=1)

    started = time.monotonic()
    provider.close()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert runtime.closed
    runtime.auth_release.set()
    prepare_thread.join(timeout=1)
    assert errors


def test_api_client_registers_configured_codex_adapter_lazily() -> None:
    routing = (
        Path(__file__).resolve().parents[1] / "examples" / ("llm-routing.codex-subscription.toml")
    )
    client = APIClient(
        llm_settings=config.load_llm_settings(routing),
        connectivity=Connectivity.UNKNOWN,
    )
    try:
        provider = client._registry.get("openai-codex")
        assert isinstance(provider, CodexAppServerAdapter)
        assert provider._runtime is None
    finally:
        client.close()
