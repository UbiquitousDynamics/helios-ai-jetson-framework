from __future__ import annotations

from types import SimpleNamespace
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from api.api_client import APIClient, APIClientError
from api.metrics import SafeMetricsRecorder
from api.transcripts import TranscriptPromoter
from api.conversation_control import ConversationFloorState
from api.realtime_conversation import RealtimeConversationController, ResponseEvent
from recognizer.turn_endpoint_detector import TurnEndpointConfig
from audio.speech_pipeline import SpeechPipelineShutdownTimeout


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


def test_pipeline_shutdown_timeout_closes_provider_but_does_not_wait_on_owned_tts():
    closed = []

    class TTS(FakeTTS):
        def close(self):
            pytest.fail("a stuck native stage still owns the TTS locks")

    class Pipeline:
        def cancel(self):
            pass

        def close(self):
            raise SpeechPipelineShutdownTimeout("synthetic stuck native stage")

    client = APIClient(client=FakeClient(), tts=TTS(), retry_wait=0)
    client._owns_tts = True
    client._speech_pipeline = Pipeline()
    client._ollama.close = lambda: closed.append("provider")
    with pytest.raises(SpeechPipelineShutdownTimeout):
        client.close()
    assert closed == ["provider"]


def test_failure_during_eof_audio_drain_is_not_replayed_and_next_turn_recovers():
    class TTS:
        calls = 0

        def __init__(self):
            self.played = []

        def synthesize_fragment(self, text):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("synthetic synthesis failure")
            return text

        def play_fragment(self, fragment):
            self.played.append(fragment)

    tts = TTS()
    fake = FakeClient([chunk("Answer.", done=True)])
    client = APIClient(client=fake, tts=tts, retry_wait=0)
    try:
        with pytest.raises(RuntimeError):
            client.talk("first synthetic request")
        assert len(fake.calls) == 1
        assert client.talk("second synthetic request") == "Answer."
        assert len(fake.calls) == 2
        assert tts.played == ["Answer."]
    finally:
        client.close()


def test_correlated_generation_eof_does_not_release_floor_until_audio_finishes():
    playing = threading.Event()
    release = threading.Event()

    class StagedTTS:
        def synthesize_fragment(self, text):
            return text

        def play_fragment(self, fragment):
            playing.set()
            assert release.wait(timeout=3)
            return None

    control = RealtimeConversationController(endpointing=TurnEndpointConfig(), activity_energy=0.08)
    token = control.begin_response()
    events = []

    def observe(event):
        events.append(event)
        control.response_event(token, event)

    client = APIClient(
        client=FakeClient([chunk("Answer.", done=True)]), tts=StagedTTS(), retry_wait=0
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.talk, "synthetic question", on_lifecycle=observe)
            try:
                assert playing.wait(timeout=2)
                snapshot = control.snapshot()
                assert snapshot.generation_complete
                assert snapshot.floor.state is ConversationFloorState.ASSISTANT_SPEAKING
                assert not future.done()
            finally:
                release.set()
            assert future.result(timeout=2) == "Answer."
        assert events.count(ResponseEvent.GENERATION_COMPLETED) == 1
        assert events.count(ResponseEvent.SYNTHESIS_STARTED) == 1
        assert events.count(ResponseEvent.PLAYBACK_COMPLETED) == 1
        control.finish_response(token)
        assert control.snapshot().floor.state is ConversationFloorState.ARMED
    finally:
        release.set()
        client.close()


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


def test_promoted_utterance_reaches_provider_and_canonical_history() -> None:
    tts = FakeTTS()
    fake = FakeClient([chunk("Answer.", done=True)])
    client = APIClient(client=fake, tts=tts, retry_wait=0)
    final = TranscriptPromoter().observe("synthetic final request", is_final=True)
    try:
        assert client.talk(final) == "Answer."
        assert fake.calls[0]["messages"][-1]["content"] == "synthetic final request"
        next_turn = client.conversation.begin_turn("next request")
        history = client.conversation.history_before(next_turn)
        assert [message.content for message in history] == ["synthetic final request", "Answer."]
        client.conversation.fail_turn(next_turn, interrupted=True)
    finally:
        client.close()


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
