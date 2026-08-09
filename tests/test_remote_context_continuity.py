from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from api.providers.codex_app_server import (
    CodexAppServerAdapter,
    _OfficialCodexRuntime,
)
from api.providers.contracts import (
    ChatMessage,
    ChatRequest,
    ContentOrigin,
    ErrorCategory,
    PrivacyLevel,
    ProviderError,
    Role,
    TextDelta,
)


def notification(method: str, payload: Any) -> dict[str, Any]:
    return {"method": method, "payload": payload}


def completed_events(text: str) -> list[dict[str, Any]]:
    return [
        notification("item/agentMessage/delta", {"delta": text}),
        notification("turn/completed", {"turn": {"status": "completed"}}),
    ]


@dataclass
class MutableClock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


@dataclass
class FakeTurn:
    thread_id: str
    id: str
    events: list[Any]
    interrupted: bool = False

    def stream(self) -> list[Any]:
        return self.events

    def interrupt(self) -> None:
        self.interrupted = True


class FakeRuntime:
    def __init__(self, events: list[list[Any] | None] | None = None) -> None:
        self.events = list(events or [])
        self.calls: list[dict[str, Any]] = []
        self.account_checks = 0
        self.closed = False
        self._thread_number = 0
        self._turn_number = 0

    def account_kind(self) -> str:
        self.account_checks += 1
        return "chatgpt"

    def start_turn(self, **kwargs: Any) -> FakeTurn:
        self._turn_number += 1
        resume_requested = "thread_id" in kwargs
        thread_id = kwargs.get("thread_id")
        if not resume_requested:
            self._thread_number += 1
            thread_id = f"thread-{self._thread_number}"
        assert isinstance(thread_id, str)
        call = dict(kwargs)
        call["operation"] = "resume" if resume_requested else "start"
        call["effective_thread_id"] = thread_id
        self.calls.append(call)
        scripted = self.events.pop(0) if self.events else None
        turn_events = (
            scripted if scripted is not None else completed_events(f"answer-{self._turn_number}")
        )
        return FakeTurn(
            thread_id=thread_id,
            id=f"turn-{self._turn_number}",
            events=turn_events,
        )

    def close(self) -> None:
        self.closed = True


def request(message: str = "Ciao", *, model: str = "gpt-5.6-luna") -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=(
            ChatMessage(
                Role.USER,
                message,
                origin=ContentOrigin.RAW_TRANSCRIPT,
            ),
        ),
        mode="talk",
        language="it",
        privacy=PrivacyLevel.REMOTE_ALLOWED,
        remote_authorized=True,
    )


def response_text(provider: CodexAppServerAdapter, value: ChatRequest) -> str:
    return "".join(event.text for event in provider.stream(value) if isinstance(event, TextDelta))


def test_step_zero_runtime_and_connection_are_reused_while_threads_stay_fresh() -> None:
    runtime = FakeRuntime()
    factory_calls = 0

    def factory() -> FakeRuntime:
        nonlocal factory_calls
        factory_calls += 1
        return runtime

    provider = CodexAppServerAdapter("openai-codex", runtime_factory=factory)

    assert response_text(provider, request("Uno")) == "answer-1"
    assert response_text(provider, request("Due")) == "answer-2"

    assert factory_calls == 1
    assert runtime.account_checks == 1
    assert [call["operation"] for call in runtime.calls] == ["start", "start"]
    assert all("thread_id" not in call for call in runtime.calls)
    assert [call["effective_thread_id"] for call in runtime.calls] == [
        "thread-1",
        "thread-2",
    ]

    provider.close()
    assert runtime.closed


def test_opt_in_context_resumes_the_same_ephemeral_thread() -> None:
    runtime = FakeRuntime()
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        allow_remote_context=True,
    )

    assert response_text(provider, request("Uno")) == "answer-1"
    assert response_text(provider, request("Due")) == "answer-2"

    assert [call["operation"] for call in runtime.calls] == ["start", "resume"]
    assert runtime.calls[1]["thread_id"] == "thread-1"
    assert "[user]\nUno" in runtime.calls[0]["prompt"]
    assert "[user]\nDue" in runtime.calls[1]["prompt"]


def test_context_resets_at_the_idle_timeout_boundary() -> None:
    clock = MutableClock()
    runtime = FakeRuntime()
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        clock=clock,
        allow_remote_context=True,
        context_idle_timeout_seconds=10,
    )

    response_text(provider, request("Uno"))
    clock.value = 9
    response_text(provider, request("Due"))
    clock.value = 19
    response_text(provider, request("Tre"))

    assert [call["operation"] for call in runtime.calls] == ["start", "resume", "start"]
    assert [call["effective_thread_id"] for call in runtime.calls] == [
        "thread-1",
        "thread-1",
        "thread-2",
    ]


def test_context_resets_before_the_turn_after_the_configured_cap() -> None:
    runtime = FakeRuntime()
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        allow_remote_context=True,
        context_max_turns=2,
    )

    for message in ("Uno", "Due", "Tre", "Quattro"):
        response_text(provider, request(message))

    assert [call["operation"] for call in runtime.calls] == [
        "start",
        "resume",
        "start",
        "resume",
    ]
    assert [call["effective_thread_id"] for call in runtime.calls] == [
        "thread-1",
        "thread-1",
        "thread-2",
        "thread-2",
    ]


def test_interrupted_turn_is_never_reused_or_counted() -> None:
    runtime = FakeRuntime(
        [
            completed_events("first"),
            [
                notification("item/agentMessage/delta", {"delta": "partial"}),
                notification("turn/completed", {"turn": {"status": "interrupted"}}),
            ],
            completed_events("fresh"),
        ]
    )
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        allow_remote_context=True,
        context_max_turns=2,
    )

    assert response_text(provider, request("Uno")) == "first"
    with pytest.raises(ProviderError) as captured:
        list(provider.stream(request("Due")))
    assert captured.value.category is ErrorCategory.CANCELLED
    assert response_text(provider, request("Tre")) == "fresh"

    assert [call["operation"] for call in runtime.calls] == ["start", "resume", "start"]
    assert runtime.calls[2]["effective_thread_id"] == "thread-2"


def test_model_switch_on_resumed_thread_is_forwarded_without_warning_output() -> None:
    warning = (
        "This session was recorded with another model; a model-switch instruction was applied."
    )
    runtime = FakeRuntime(
        [
            completed_events("Luna answer."),
            [
                notification("warning", {"message": warning}),
                notification(
                    "item/started",
                    {"item": {"type": "developerMessage", "text": warning}},
                ),
                *completed_events("Sol answer."),
            ],
        ]
    )
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        allow_remote_context=True,
    )

    first = response_text(provider, request("Facile", model="gpt-5.6-luna"))
    second = response_text(provider, request("Difficile", model="gpt-5.6-sol"))

    assert first == "Luna answer."
    assert second == "Sol answer."
    assert warning not in second
    assert runtime.calls[1]["operation"] == "resume"
    assert runtime.calls[1]["model"] == "gpt-5.6-sol"


@pytest.mark.parametrize(
    ("overrides", "error", "message"),
    [
        ({"allow_remote_context": "true"}, TypeError, "allow_remote_context"),
        ({"context_idle_timeout_seconds": 0}, ValueError, "idle_timeout"),
        ({"context_idle_timeout_seconds": float("inf")}, ValueError, "idle_timeout"),
        ({"context_max_turns": 0}, ValueError, "max_turns"),
        ({"context_max_turns": True}, ValueError, "max_turns"),
    ],
)
def test_context_configuration_is_strictly_validated(
    overrides: dict[str, Any],
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        CodexAppServerAdapter("openai-codex", runtime=FakeRuntime(), **overrides)


def test_official_runtime_uses_start_then_resume_with_per_turn_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Any]] = []

    class FakeApprovalMode:
        deny_all = object()

    class FakeSandbox:
        read_only = object()

    class FakeCodexConfig:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(("config", kwargs))

    class FakeSDKThread:
        def __init__(self, thread_id: str) -> None:
            self.id = thread_id

        def turn(self, prompt: str, **kwargs: Any) -> FakeTurn:
            calls.append(("turn", {"thread_id": self.id, "prompt": prompt, **kwargs}))
            return FakeTurn(self.id, f"turn-{len(calls)}", [])

    class FakeCodex:
        def __init__(self, _config: FakeCodexConfig) -> None:
            calls.append(("client", None))

        def thread_start(self, **kwargs: Any) -> FakeSDKThread:
            calls.append(("thread_start", kwargs))
            return FakeSDKThread("thread-new")

        def thread_resume(self, thread_id: str, **kwargs: Any) -> FakeSDKThread:
            calls.append(("thread_resume", {"thread_id": thread_id, **kwargs}))
            return FakeSDKThread(thread_id)

        def close(self) -> None:
            calls.append(("close", None))

    module = types.ModuleType("openai_codex")
    module.ApprovalMode = FakeApprovalMode
    module.Codex = FakeCodex
    module.CodexConfig = FakeCodexConfig
    module.Sandbox = FakeSandbox
    monkeypatch.setitem(sys.modules, "openai_codex", module)
    codex_home = tmp_path / "source-codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    runtime = _OfficialCodexRuntime()
    runtime.start_turn(
        model="gpt-5.6-luna",
        prompt="first",
        developer_instructions="instructions",
        effort="none",
        service_tier=None,
    )
    runtime.start_turn(
        model="gpt-5.6-sol",
        prompt="second",
        developer_instructions="instructions",
        effort="low",
        service_tier="priority",
        thread_id="thread-new",
    )
    runtime.close()

    start = next(value for operation, value in calls if operation == "thread_start")
    resume = next(value for operation, value in calls if operation == "thread_resume")
    turns = [value for operation, value in calls if operation == "turn"]
    assert start["ephemeral"] is True
    assert start["model"] == "gpt-5.6-luna"
    assert "model" not in turns[0]
    assert resume["thread_id"] == "thread-new"
    assert resume["model"] == "gpt-5.6-sol"
    assert turns[1]["model"] == "gpt-5.6-sol"
    assert turns[1]["service_tier"] == "priority"
