from __future__ import annotations

import threading
from array import array

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


class PendingRecognizer:
    instances: list[PendingRecognizer] = []

    def __init__(self, _model: object, _rate: int) -> None:
        self.final_result_calls = 0
        self.instances.append(self)

    def AcceptWaveform(self, _data: bytes) -> bool:
        return False

    def Result(self) -> str:
        return '{"text": ""}'

    def PartialResult(self) -> str:
        return '{"partial": "nuova nuova domanda"}'

    def FinalResult(self) -> str:
        self.final_result_calls += 1
        return '{"text": "nuova nuova domanda completa"}'


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


def test_stop_event_ends_one_session_and_flushes_pending_text() -> None:
    stop_event = threading.Event()

    class StoppingStream(FakeStream):
        def __init__(self) -> None:
            super().__init__()
            self.read_calls = 0

        def read(self, chunk: int, *, exception_on_overflow: bool) -> bytes:
            self.read_calls += 1
            data = super().read(chunk, exception_on_overflow=exception_on_overflow)
            stop_event.set()
            return data

    audio = FakeAudio()
    audio.stream = StoppingStream()
    PendingRecognizer.instances.clear()
    recognizer = SpeechRecognizer(
        model=object(),
        audio_interface=audio,
        recognizer_factory=PendingRecognizer,
    )

    results = list(recognizer.listen_events(stop_event=stop_event))

    assert results == [
        RecognitionResult("nuova domanda", is_final=False),
        RecognitionResult("nuova domanda completa", is_final=True),
    ]
    assert audio.stream.read_calls == 1
    assert len(PendingRecognizer.instances) == 1
    assert PendingRecognizer.instances[0].final_result_calls == 1
    assert audio.stream.started
    assert audio.stream.stopped
    assert audio.stream.closed


def test_stop_event_never_promotes_last_partial_when_vosk_flush_is_empty() -> None:
    stop_event = threading.Event()

    class EmptyFlushRecognizer(PendingRecognizer):
        def FinalResult(self) -> str:
            self.final_result_calls += 1
            return '{"text": ""}'

    class StoppingStream(FakeStream):
        def read(self, chunk: int, *, exception_on_overflow: bool) -> bytes:
            data = super().read(chunk, exception_on_overflow=exception_on_overflow)
            stop_event.set()
            return data

    audio = FakeAudio()
    audio.stream = StoppingStream()
    recognizer = SpeechRecognizer(
        model=object(),
        audio_interface=audio,
        recognizer_factory=EmptyFlushRecognizer,
    )

    assert list(recognizer.listen_events(stop_event=stop_event)) == [
        RecognitionResult("nuova domanda", is_final=False),
    ]


def test_normal_timeout_keeps_an_empty_flush_as_partial() -> None:
    class EmptyFlushRecognizer(PendingRecognizer):
        def FinalResult(self) -> str:
            return '{"text": ""}'

    observed = iter([0.0, 0.0, 1.0])
    recognizer = SpeechRecognizer(
        model=object(),
        audio_interface=FakeAudio(),
        recognizer_factory=EmptyFlushRecognizer,
        clock=lambda: next(observed),
    )

    assert list(recognizer.listen_events(timeout=0.5)) == [
        RecognitionResult("nuova domanda", is_final=False)
    ]


def test_identical_partial_is_reemitted_when_pcm_energy_rises_materially() -> None:
    stop_event = threading.Event()

    class EnergyStream(FakeStream):
        def __init__(self) -> None:
            super().__init__()
            self.frames = [
                array("h", [655] * 400).tobytes(),
                array("h", [3277] * 400).tobytes(),
            ]
            self.position = 0

        def read(self, _chunk: int, *, exception_on_overflow: bool) -> bytes:
            assert exception_on_overflow is False
            frame = self.frames[self.position]
            self.position += 1
            if self.position == len(self.frames):
                stop_event.set()
            return frame

    class RepeatedPartialRecognizer(PendingRecognizer):
        def FinalResult(self) -> str:
            return '{"text": ""}'

    audio = FakeAudio()
    audio.stream = EnergyStream()
    recognizer = SpeechRecognizer(
        model=object(),
        audio_interface=audio,
        recognizer_factory=RepeatedPartialRecognizer,
    )

    results = list(recognizer.listen_events(stop_event=stop_event))

    assert [result.text for result in results] == [
        "nuova domanda",
        "nuova domanda",
    ]
    assert results[0].is_final is False
    assert results[1].is_final is False
    assert results[1].frame_energy is not None
    assert results[0].frame_energy is not None
    assert results[1].frame_energy > results[0].frame_energy


def test_pre_set_stop_event_does_not_open_microphone_stream() -> None:
    stop_event = threading.Event()
    stop_event.set()
    audio = FakeAudio()
    recognizer = SpeechRecognizer(
        model=object(),
        audio_interface=audio,
        recognizer_factory=FinalRecognizer,
    )

    assert list(recognizer.listen_events(stop_event=stop_event)) == []
    assert audio.open_kwargs == {}


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
