from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.api_client import APIClient, APIClientError
from api.metrics import SafeMetricsRecorder


class FakeTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)


class FakeClient:
    def __init__(self, responses: list[object] | None = None) -> None:
        self.responses = responses or []
        self.calls: list[dict[str, object]] = []
        self.failures_remaining = 0

    def chat(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise OSError("Ollama is starting")
        return iter(self.responses)


def chunk(text: str, *, done: bool = False, done_reason: str | None = None) -> object:
    return SimpleNamespace(
        message=SimpleNamespace(content=text),
        done=done,
        done_reason=done_reason,
    )


def test_constructor_is_lazy_and_normalizes_legacy_endpoint() -> None:
    created_hosts: list[str] = []
    fake = FakeClient()

    client = APIClient(
        api_url="http://example.test:11434/api/generate",
        client_factory=lambda host: created_hosts.append(host) or fake,
        tts=FakeTTS(),
    )

    assert client.host == "http://example.test:11434"
    assert created_hosts == []
    assert fake.calls == []


def test_local_prepare_is_background_and_idempotent() -> None:
    fake = FakeClient()
    client = APIClient(client=fake, tts=FakeTTS(), retry_wait=0)

    first = client.prepare_local_async()
    second = client.prepare_local_async()

    assert first is not None
    assert second is first
    first.join(timeout=1)
    assert not first.is_alive()
    assert fake.calls == [
        {
            "model": client.models["talk"],
            "messages": [{"role": "user", "content": ""}],
            "stream": False,
        }
    ]


def test_talk_uses_one_stream_parser_and_flushes_done_reason() -> None:
    tts = FakeTTS()
    fake = FakeClient(
        [
            {"message": {"content": "Hello, "}, "done": False},
            chunk("crew", done_reason="stop"),
        ]
    )
    client = APIClient(client=fake, tts=tts, retry_wait=0)

    response = client.talk("Emilia, say hello", context="Be concise")

    assert response == "Hello, crew"
    assert tts.spoken == ["Hello, crew"]
    assert fake.calls == [
        {
            "model": client.models["talk"],
            "messages": [
                {"role": "system", "content": "Be concise"},
                {"role": "user", "content": "Emilia, say hello"},
            ],
            "stream": True,
        }
    ]


def test_think_does_not_speak_unless_requested() -> None:
    tts = FakeTTS()
    fake = FakeClient([chunk("Answer.", done=True)])
    client = APIClient(client=fake, tts=tts, retry_wait=0)

    assert client.think("question") == "Answer."
    assert tts.spoken == []


def test_stream_failures_are_retried_and_then_succeed() -> None:
    fake = FakeClient([chunk("Ready.", done=True)])
    fake.failures_remaining = 1
    client = APIClient(
        client=fake,
        tts=FakeTTS(),
        retry_attempts=2,
        retry_wait=0,
    )

    assert client.talk("hello") == "Ready."
    assert len(fake.calls) == 2


def test_configured_retry_wait_above_five_seconds_does_not_disable_retry() -> None:
    fake = FakeClient([chunk("Ready.", done=True)])
    fake.failures_remaining = 1
    sleeps: list[float] = []
    client = APIClient(
        client=fake,
        tts=FakeTTS(),
        retry_attempts=2,
        retry_wait=6,
        sleep=sleeps.append,
    )

    assert client.talk("hello") == "Ready."
    assert sleeps == [6]


def test_exhausted_retries_raise_a_typed_error() -> None:
    fake = FakeClient()
    fake.failures_remaining = 3
    client = APIClient(
        client=fake,
        tts=FakeTTS(),
        retry_attempts=2,
        retry_wait=0,
    )

    with pytest.raises(APIClientError, match="after 2 attempt"):
        client.talk("hello")


def test_tts_failure_is_not_retried_or_relabeled() -> None:
    class FailingTTS:
        def speak(self, _text: str) -> None:
            raise RuntimeError("speaker failed")

    fake = FakeClient([chunk("Ready.", done=True)])
    metrics = SafeMetricsRecorder()
    client = APIClient(
        client=fake,
        tts=FailingTTS(),
        retry_attempts=3,
        retry_wait=0,
        metrics=metrics,
    )

    with pytest.raises(RuntimeError, match="speaker failed"):
        client.talk("hello")
    assert len(fake.calls) == 1
    events = metrics.snapshot()
    terminal = [event for event in events if event.event == "llm_request_failed"]
    assert len(terminal) == 1
    assert terminal[0].provider == "ollama"
    assert terminal[0].speech_committed is True
    assert terminal[0].retry_count == 0
    assert [event.event for event in events].count("tts_failed") == 1


def test_stream_is_not_retried_after_speech_has_started() -> None:
    class InterruptedClient(FakeClient):
        def chat(self, **kwargs: object) -> object:
            self.calls.append(kwargs)

            def responses() -> object:
                yield chunk("First sentence.", done=False)
                raise OSError("connection dropped")

            return responses()

    tts = FakeTTS()
    fake = InterruptedClient()
    client = APIClient(
        client=fake,
        tts=tts,
        retry_attempts=3,
        retry_wait=0,
    )

    with pytest.raises(APIClientError, match="after speech output began"):
        client.talk("hello")
    assert tts.spoken == ["First sentence."]
    assert len(fake.calls) == 1


def test_failed_active_initialization_closes_owned_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackingMetrics:
        def __init__(self) -> None:
            self.closed = 0

        def record(self, event: object) -> object:
            return event

        def close(self) -> bool:
            self.closed += 1
            return True

    tracking = TrackingMetrics()
    monkeypatch.setattr(APIClient, "_build_metrics", lambda _self: tracking)

    def fail_warm_up(_self: APIClient, _mode: str = "talk") -> None:
        raise RuntimeError("warm-up failed")

    monkeypatch.setattr(APIClient, "warm_up", fail_warm_up)

    with pytest.raises(RuntimeError, match="warm-up failed"):
        APIClient(client=FakeClient(), tts=FakeTTS(), warm_up=True)

    assert tracking.closed == 1
