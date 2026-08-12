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


def contextual_request(
    turn: int,
    messages: tuple[ChatMessage, ...],
    *,
    session_id: str = "logical-session",
) -> ChatRequest:
    return ChatRequest(
        model="gpt-5.6-luna",
        messages=messages,
        mode="talk",
        language="en",
        privacy=PrivacyLevel.REMOTE_ALLOWED,
        remote_authorized=True,
        conversation_id=session_id,
        conversation_turn=turn,
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


def test_context_state_registry_is_lru_bounded_across_logical_sessions() -> None:
    runtime = FakeRuntime()
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        allow_remote_context=True,
        context_state_limit=2,
    )

    for session_id in ("session-a", "session-b", "session-c"):
        response_text(
            provider,
            contextual_request(
                1,
                (
                    ChatMessage(
                        Role.USER,
                        f"question for {session_id}",
                        origin=ContentOrigin.RAW_TRANSCRIPT,
                    ),
                ),
                session_id=session_id,
            ),
        )

    assert tuple(provider._context_states) == ("session-b", "session-c")


def test_runtime_retirement_invalidates_other_sessions_before_recovery() -> None:
    first = FakeRuntime()
    second = FakeRuntime()
    runtimes = iter([first, second])
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime_factory=lambda: next(runtimes),
        allow_remote_context=True,
    )
    user_a = ChatMessage(Role.USER, "A one", origin=ContentOrigin.RAW_TRANSCRIPT)
    user_b = ChatMessage(Role.USER, "B one", origin=ContentOrigin.RAW_TRANSCRIPT)

    response_text(provider, contextual_request(1, (user_a,), session_id="session-a"))
    response_text(provider, contextual_request(1, (user_b,), session_id="session-b"))
    provider._retire_runtime(first)

    response_text(
        provider,
        contextual_request(
            2,
            (
                ChatMessage(
                    Role.USER,
                    "B one",
                    origin=ContentOrigin.CONVERSATION_HISTORY,
                    source_origins=frozenset({ContentOrigin.RAW_TRANSCRIPT}),
                ),
                ChatMessage(
                    Role.ASSISTANT,
                    "answer-2",
                    origin=ContentOrigin.CONVERSATION_HISTORY,
                ),
                ChatMessage(Role.USER, "B two", origin=ContentOrigin.RAW_TRANSCRIPT),
            ),
            session_id="session-b",
        ),
    )

    assert second.calls[0]["operation"] == "start"
    assert "B one" in second.calls[0]["prompt"]
    assert "B two" in second.calls[0]["prompt"]


def test_ambiguous_start_turn_failure_invalidates_resumed_checkpoint() -> None:
    class AmbiguousRuntime(FakeRuntime):
        def start_turn(self, **kwargs: Any) -> FakeTurn:
            if len(self.calls) == 1:
                thread_id = str(kwargs["thread_id"])
                self.calls.append(
                    {
                        **kwargs,
                        "operation": "resume",
                        "effective_thread_id": thread_id,
                    }
                )
                raise OSError("connection lost after remote append")
            return super().start_turn(**kwargs)

    runtime = AmbiguousRuntime()
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        allow_remote_context=True,
    )
    first_user = ChatMessage(Role.USER, "first", origin=ContentOrigin.RAW_TRANSCRIPT)
    response_text(provider, contextual_request(1, (first_user,)))

    with pytest.raises(ProviderError):
        response_text(
            provider,
            contextual_request(
                2,
                (
                    ChatMessage(
                        Role.USER,
                        "first",
                        origin=ContentOrigin.CONVERSATION_HISTORY,
                    ),
                    ChatMessage(Role.USER, "second", origin=ContentOrigin.RAW_TRANSCRIPT),
                ),
            ),
        )

    state = provider._context_states["logical-session"]
    assert state.thread_id is None
    assert state.invalid_reason == "worker_error"

    response_text(
        provider,
        contextual_request(
            3,
            (
                ChatMessage(
                    Role.USER,
                    "first",
                    origin=ContentOrigin.CONVERSATION_HISTORY,
                ),
                ChatMessage(
                    Role.USER,
                    "second",
                    origin=ContentOrigin.CONVERSATION_HISTORY,
                ),
                ChatMessage(Role.USER, "third", origin=ContentOrigin.RAW_TRANSCRIPT),
            ),
        ),
    )
    assert [call["operation"] for call in runtime.calls] == ["start", "resume", "start"]


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


def test_healthy_resume_sends_only_the_unsynced_current_turn() -> None:
    runtime = FakeRuntime()
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        allow_remote_context=True,
    )
    first_user = ChatMessage(Role.USER, "Name three planets", ContentOrigin.RAW_TRANSCRIPT)
    first_answer = ChatMessage(
        Role.ASSISTANT,
        "Mercury, Venus, Earth",
        ContentOrigin.CONVERSATION_HISTORY,
    )
    historical_user = ChatMessage(
        Role.USER,
        first_user.content,
        ContentOrigin.CONVERSATION_HISTORY,
    )
    second_user = ChatMessage(Role.USER, "Only the second", ContentOrigin.RAW_TRANSCRIPT)

    response_text(provider, contextual_request(1, (first_user,)))
    response_text(
        provider,
        contextual_request(2, (historical_user, first_answer, second_user)),
    )

    assert [call["operation"] for call in runtime.calls] == ["start", "resume"]
    assert "Name three planets" in runtime.calls[0]["prompt"]
    assert "Only the second" in runtime.calls[1]["prompt"]
    assert "Name three planets" not in runtime.calls[1]["prompt"]
    assert "Mercury, Venus, Earth" not in runtime.calls[1]["prompt"]


def test_interrupted_thread_recovery_rehydrates_canonical_history() -> None:
    runtime = FakeRuntime(
        [
            completed_events("Mercury, Venus, Earth"),
            [
                notification("item/agentMessage/delta", {"delta": "Venus is"}),
                notification("turn/completed", {"turn": {"status": "interrupted"}}),
            ],
            completed_events("About 225 Earth days"),
        ]
    )
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        allow_remote_context=True,
    )
    first_user = ChatMessage(Role.USER, "Name three planets", ContentOrigin.RAW_TRANSCRIPT)
    previous_user = ChatMessage(
        Role.USER,
        "Name three planets",
        ContentOrigin.CONVERSATION_HISTORY,
    )
    first_answer = ChatMessage(
        Role.ASSISTANT,
        "Mercury, Venus, Earth",
        ContentOrigin.CONVERSATION_HISTORY,
    )
    second_user = ChatMessage(Role.USER, "Only the second", ContentOrigin.RAW_TRANSCRIPT)

    response_text(provider, contextual_request(1, (first_user,)))
    with pytest.raises(ProviderError) as interrupted:
        response_text(
            provider,
            contextual_request(2, (previous_user, first_answer, second_user)),
        )
    assert interrupted.value.category is ErrorCategory.CANCELLED

    recovered = response_text(
        provider,
        contextual_request(
            3,
            (
                previous_user,
                first_answer,
                ChatMessage(
                    Role.USER,
                    "Only the second",
                    ContentOrigin.CONVERSATION_HISTORY,
                ),
                ChatMessage(Role.USER, "How long is its year?", ContentOrigin.RAW_TRANSCRIPT),
            ),
        ),
    )

    assert recovered == "About 225 Earth days"
    assert [call["operation"] for call in runtime.calls] == ["start", "resume", "start"]
    recovery_prompt = runtime.calls[2]["prompt"]
    assert "Name three planets" in recovery_prompt
    assert "Mercury, Venus, Earth" in recovery_prompt
    assert "Only the second" in recovery_prompt
    assert "How long is its year?" in recovery_prompt


def test_turn_cap_rotates_physical_thread_and_rehydrates_logical_history() -> None:
    runtime = FakeRuntime()
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        allow_remote_context=True,
        context_max_turns=1,
    )
    first_user = ChatMessage(Role.USER, "first question", ContentOrigin.RAW_TRANSCRIPT)
    response_text(provider, contextual_request(1, (first_user,)))
    response_text(
        provider,
        contextual_request(
            2,
            (
                ChatMessage(
                    Role.USER,
                    "first question",
                    ContentOrigin.CONVERSATION_HISTORY,
                ),
                ChatMessage(
                    Role.ASSISTANT,
                    "answer-1",
                    ContentOrigin.CONVERSATION_HISTORY,
                ),
                ChatMessage(Role.USER, "second question", ContentOrigin.RAW_TRANSCRIPT),
            ),
        ),
    )

    assert [call["operation"] for call in runtime.calls] == ["start", "start"]
    assert "first question" in runtime.calls[1]["prompt"]
    assert "answer-1" in runtime.calls[1]["prompt"]
    assert "second question" in runtime.calls[1]["prompt"]


def test_provider_history_gap_recovers_instead_of_resuming_stale_thread() -> None:
    runtime = FakeRuntime()
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        allow_remote_context=True,
    )
    response_text(
        provider,
        contextual_request(
            1,
            (ChatMessage(Role.USER, "remote turn", ContentOrigin.RAW_TRANSCRIPT),),
        ),
    )
    response_text(
        provider,
        contextual_request(
            3,
            (
                ChatMessage(Role.USER, "remote turn", ContentOrigin.CONVERSATION_HISTORY),
                ChatMessage(Role.ASSISTANT, "answer-1", ContentOrigin.CONVERSATION_HISTORY),
                ChatMessage(Role.USER, "local turn", ContentOrigin.CONVERSATION_HISTORY),
                ChatMessage(Role.ASSISTANT, "local answer", ContentOrigin.CONVERSATION_HISTORY),
                ChatMessage(Role.USER, "back to Codex", ContentOrigin.RAW_TRANSCRIPT),
            ),
        ),
    )

    assert [call["operation"] for call in runtime.calls] == ["start", "start"]
    assert "local turn" in runtime.calls[1]["prompt"]
    assert "local answer" in runtime.calls[1]["prompt"]


def test_logical_sessions_never_share_codex_threads() -> None:
    runtime = FakeRuntime()
    provider = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        allow_remote_context=True,
    )
    response_text(
        provider,
        contextual_request(
            1,
            (ChatMessage(Role.USER, "session A one", ContentOrigin.RAW_TRANSCRIPT),),
            session_id="session-a",
        ),
    )
    response_text(
        provider,
        contextual_request(
            1,
            (ChatMessage(Role.USER, "session B one", ContentOrigin.RAW_TRANSCRIPT),),
            session_id="session-b",
        ),
    )
    response_text(
        provider,
        contextual_request(
            2,
            (ChatMessage(Role.USER, "session A two", ContentOrigin.RAW_TRANSCRIPT),),
            session_id="session-a",
        ),
    )

    assert [call["operation"] for call in runtime.calls] == ["start", "start", "resume"]
    assert runtime.calls[0]["effective_thread_id"] == "thread-1"
    assert runtime.calls[1]["effective_thread_id"] == "thread-2"
    assert runtime.calls[2]["effective_thread_id"] == "thread-1"


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
