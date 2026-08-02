from __future__ import annotations

import threading

from recognizer.speech_recognizer import RecognitionResult, SpeechRecognizer


class FakeStream:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.closed = False

    def start_stream(self) -> None:
        self.started = True

    def read(self, _chunk: int, *, exception_on_overflow: bool) -> bytes:
        assert exception_on_overflow is False
        return b"audio"

    def stop_stream(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class FakeAudio:
    def __init__(self) -> None:
        self.stream = FakeStream()
        self.open_kwargs: dict[str, object] = {}
        self.terminated = False

    def open(self, **kwargs: object) -> FakeStream:
        self.open_kwargs = kwargs
        return self.stream

    def terminate(self) -> None:
        self.terminated = True


class FinalRecognizer:
    def __init__(self, _model: object, _rate: int) -> None:
        pass

    def AcceptWaveform(self, _data: bytes) -> bool:
        return True

    def Result(self) -> str:
        return '{"text": "ciao ciao equipaggio"}'

    def PartialResult(self) -> str:
        return '{"partial": ""}'


def test_listen_once_returns_first_final_and_always_closes_stream() -> None:
    audio = FakeAudio()
    recognizer = SpeechRecognizer(
        model=object(),
        audio_interface=audio,
        recognizer_factory=FinalRecognizer,
        owns_audio=True,
    )

    result = recognizer.listen_once(timeout=1)

    assert result == RecognitionResult("ciao equipaggio", is_final=True)
    assert audio.stream.started
    assert audio.stream.stopped
    assert audio.stream.closed

    recognizer.close()
    recognizer.close()
    assert audio.terminated


def test_legacy_listen_generator_yields_text() -> None:
    audio = FakeAudio()
    recognizer = SpeechRecognizer(
        model=object(),
        audio_interface=audio,
        recognizer_factory=FinalRecognizer,
    )

    events = recognizer.listen(timeout=1)
    assert next(events) == "ciao equipaggio"
    events.close()
    assert audio.stream.closed


def test_background_prepare_is_idempotent() -> None:
    class BlockingRecognizer(SpeechRecognizer):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def _ensure_runtime(self) -> None:
            self.entered.set()
            self.release.wait(timeout=1)

    recognizer = BlockingRecognizer()

    first = recognizer.prepare_async()
    assert first is not None
    assert recognizer.entered.wait(timeout=1)
    second = recognizer.prepare_async()

    assert second is first
    recognizer.release.set()
    first.join(timeout=1)
    assert not first.is_alive()


def test_prepare_does_not_open_microphone_stream() -> None:
    audio = FakeAudio()
    recognizer = SpeechRecognizer(
        model=object(),
        audio_interface=audio,
        recognizer_factory=FinalRecognizer,
        audio_format=8,
    )

    assert recognizer.prepare_async() is None
    assert audio.open_kwargs == {}
