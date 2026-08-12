from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest

from api.api_client import APIClient, APIClientError
from api.conversation import ConversationSession, ConversationTurnStatus


class SilentTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)


class SequencedOllamaClient:
    def __init__(self, responses: Iterable[Iterable[object]]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> Iterable[object]:
        self.calls.append(kwargs)
        return iter(next(self._responses))


def completed(text: str) -> list[object]:
    return [{"message": {"content": text}, "done": True}]


def test_local_model_receives_canonical_history_on_the_next_turn() -> None:
    ollama = SequencedOllamaClient(
        [
            completed("Venus is the second planet."),
            completed("Its year lasts about 225 Earth days."),
        ]
    )
    client = APIClient(client=ollama, tts=SilentTTS(), retry_wait=0)

    assert client.talk("Name the first three planets") == "Venus is the second planet."
    assert client.talk("How long is the second one's year?") == (
        "Its year lasts about 225 Earth days."
    )

    assert ollama.calls[1]["messages"] == [
        {"role": "user", "content": "Name the first three planets"},
        {"role": "assistant", "content": "Venus is the second planet."},
        {"role": "user", "content": "How long is the second one's year?"},
    ]


def test_interrupted_user_turn_remains_in_history_without_partial_assistant_text() -> None:
    def interrupted() -> Iterable[object]:
        yield {"message": {"content": "Mercury, Venus, and Earth."}, "done": False}
        raise OSError("stream interrupted")

    ollama = SequencedOllamaClient(
        [
            interrupted(),
            completed("Venus is the second planet."),
        ]
    )
    client = APIClient(
        client=ollama,
        tts=SilentTTS(),
        retry_attempts=1,
        retry_wait=0,
    )

    with pytest.raises(APIClientError):
        client.talk("Name the first three planets")
    assert client.talk("Only discuss the second one") == "Venus is the second planet."

    assert ollama.calls[1]["messages"] == [
        {"role": "user", "content": "Name the first three planets"},
        {"role": "user", "content": "Only discuss the second one"},
    ]


def test_session_idle_expiry_creates_a_new_logical_conversation() -> None:
    now = [0.0]
    identifiers = iter(["session-one", "session-two"])
    session = ConversationSession(
        idle_timeout_seconds=10,
        clock=lambda: now[0],
        id_factory=lambda: next(identifiers),
    )
    first = session.begin_turn("first")
    session.complete_turn(first, "answer")

    now[0] = 10.0
    second = session.begin_turn("new conversation")

    assert session.session_id == "session-two"
    assert second.number == 1
    assert session.history_before(second) == ()
    assert session.snapshot().last_reset_reason == "idle_timeout"
    session.fail_turn(second, interrupted=False)


def test_session_history_is_bounded_without_resetting_logical_turn_count() -> None:
    session = ConversationSession(
        max_history_turns=2,
        id_factory=lambda: "stable-session",
    )
    for position in range(1, 5):
        turn = session.begin_turn(f"question {position}")
        session.complete_turn(turn, f"answer {position}")

    fifth = session.begin_turn("question 5")
    session.mark_streaming(fifth)
    assert session.snapshot().active_turn_status is ConversationTurnStatus.STREAMING
    history = session.history_before(fifth)

    assert fifth.number == 5
    assert [(message.role.value, message.content) for message in history] == [
        ("user", "question 3"),
        ("assistant", "answer 3"),
        ("user", "question 4"),
        ("assistant", "answer 4"),
    ]
    session.fail_turn(fifth, interrupted=True)
    assert fifth.status is ConversationTurnStatus.INTERRUPTED
    snapshot = session.snapshot()
    assert snapshot.turn_count == 5
    assert snapshot.retained_turn_count == 2
    assert snapshot.history_message_count == 3


def test_explicit_session_reset_does_not_reuse_prior_history() -> None:
    identifiers = iter(["before-reset", "after-reset"])
    session = ConversationSession(id_factory=lambda: next(identifiers))
    first = session.begin_turn("private first turn")
    session.complete_turn(first, "private answer")

    assert session.reset(reason="privacy_boundary") == "after-reset"
    next_turn = session.begin_turn("fresh turn")

    assert next_turn.number == 1
    assert session.history_before(next_turn) == ()
    assert session.snapshot().last_reset_reason == "privacy_boundary"
    session.fail_turn(next_turn, interrupted=False)
