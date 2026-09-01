from __future__ import annotations

import threading
from array import array

import pytest

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

    assert result is not None
    assert result.text == "ciao equipaggio"
    assert result.is_final is True
    assert result.segment_id == 1
    assert result.segment_started_at is not None
    assert result.energy_reemit is False
    assert audio.stream.started
    assert audio.stream.stopped
    assert audio.stream.closed

    recognizer.close()
    recognizer.close()
    assert audio.terminated


def test_configured_microphone_name_is_resolved_without_default_fallback() -> None:
    class DeviceAudio(FakeAudio):
        def get_device_count(self) -> int:
            return 2

        def get_device_info_by_index(self, index: int) -> dict[str, object]:
            return (
                {"name": "Tegra capture", "maxInputChannels": 1}
                if index == 0
                else {"name": "USB PnP Audio Device", "maxInputChannels": 2}
            )

    audio = DeviceAudio()
    recognizer = SpeechRecognizer(
        model=object(),
        audio_interface=audio,
        recognizer_factory=FinalRecognizer,
        input_device="usb pnp",
    )

    result = recognizer.listen_once(timeout=1)

    assert result is not None
    assert audio.open_kwargs["input_device_index"] == 1


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

    assert [(result.text, result.is_final) for result in results] == [
        ("nuova domanda", False),
        ("nuova domanda completa", True),
    ]
    assert [result.segment_id for result in results] == [1, 1]
    assert results[0].segment_started_at == results[1].segment_started_at
    assert [result.energy_reemit for result in results] == [False, False]
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

    results = list(recognizer.listen_events(stop_event=stop_event))

    assert [(result.text, result.is_final) for result in results] == [("nuova domanda", False)]
    assert results[0].segment_id == 1
    assert results[0].segment_started_at is not None


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

    results = list(recognizer.listen_events(timeout=0.5))

    assert [(result.text, result.is_final) for result in results] == [("nuova domanda", False)]
    assert results[0].segment_id == 1
    assert results[0].segment_started_at == 0.0


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
    assert [result.segment_id for result in results] == [1, 1]
    assert results[0].segment_started_at == results[1].segment_started_at
    assert [result.energy_reemit for result in results] == [False, True]


def test_segment_provenance_resets_after_vosk_final_boundaries() -> None:
    stop_event = threading.Event()

    class ThreeFrameStream(FakeStream):
        def __init__(self) -> None:
            super().__init__()
            self.read_calls = 0

        def read(self, chunk: int, *, exception_on_overflow: bool) -> bytes:
            self.read_calls += 1
            data = super().read(chunk, exception_on_overflow=exception_on_overflow)
            if self.read_calls == 3:
                stop_event.set()
            return data

    class SegmentedRecognizer:
        def __init__(self, _model: object, _rate: int) -> None:
            self.accept_calls = 0

        def AcceptWaveform(self, _data: bytes) -> bool:
            self.accept_calls += 1
            return self.accept_calls == 2

        def PartialResult(self) -> str:
            if self.accept_calls == 1:
                return '{"partial": "prima"}'
            return '{"partial": "seconda"}'

        def Result(self) -> str:
            return '{"text": "prima completa"}'

        def FinalResult(self) -> str:
            return '{"text": "seconda completa"}'

    audio = FakeAudio()
    audio.stream = ThreeFrameStream()
    observed = iter([0.0, 1.0, 2.0, 3.0])
    recognizer = SpeechRecognizer(
        model=object(),
        audio_interface=audio,
        recognizer_factory=SegmentedRecognizer,
        clock=lambda: next(observed),
    )

    results = list(recognizer.listen_events(stop_event=stop_event))

    assert [(result.text, result.is_final) for result in results] == [
        ("prima", False),
        ("prima completa", True),
        ("seconda", False),
        ("seconda completa", True),
    ]
    assert [result.segment_id for result in results] == [1, 1, 2, 2]
    assert [result.segment_started_at for result in results] == [1.0, 1.0, 3.0, 3.0]
    assert all(result.energy_reemit is False for result in results)


def test_vosk_word_metadata_is_enabled_and_exposed_content_free() -> None:
    stop_event = threading.Event()

    class MetadataStream(FakeStream):
        def __init__(self) -> None:
            super().__init__()
            self.frames = [
                array("h", [655] * 400).tobytes(),
                array("h", [3_277] * 400).tobytes(),
            ]
            self.position = 0

        def read(self, _chunk: int, *, exception_on_overflow: bool) -> bytes:
            assert exception_on_overflow is False
            frame = self.frames[self.position]
            self.position += 1
            if self.position == len(self.frames):
                stop_event.set()
            return frame

    class MetadataRecognizer:
        instances: list[MetadataRecognizer] = []

        def __init__(self, _model: object, _rate: int) -> None:
            self.accept_calls = 0
            self.words_enabled = False
            self.partial_words_enabled = False
            self.instances.append(self)

        def SetWords(self, enabled: bool) -> None:
            self.words_enabled = enabled

        def SetPartialWords(self, enabled: bool) -> None:
            self.partial_words_enabled = enabled

        def AcceptWaveform(self, _data: bytes) -> bool:
            self.accept_calls += 1
            return self.accept_calls == 2

        def PartialResult(self) -> str:
            return (
                '{"partial":"nuova domanda",'
                '"partial_result":['
                '{"word":"nuova","conf":0.8,"start":0.1,"end":0.3},'
                '{"word":"domanda","conf":0.6,"start":0.3,"end":0.55}]}'
            )

        def Result(self) -> str:
            return (
                '{"text":"nuova domanda completa",'
                '"result":['
                '{"word":"nuova","conf":0.9,"start":0.1,"end":0.3},'
                '{"word":"domanda","conf":0.7,"start":0.3,"end":0.55},'
                '{"word":"completa","conf":0.8,"start":0.55,"end":0.9}]}'
            )

    audio = FakeAudio()
    audio.stream = MetadataStream()
    MetadataRecognizer.instances.clear()
    recognizer = SpeechRecognizer(
        model=object(),
        audio_interface=audio,
        recognizer_factory=MetadataRecognizer,
    )

    results = list(recognizer.listen_events(stop_event=stop_event))

    assert [(result.text, result.is_final) for result in results] == [
        ("nuova domanda", False),
        ("nuova domanda completa", True),
    ]
    assert results[0].confidence == pytest.approx(0.7)
    assert results[0].speech_duration_seconds == pytest.approx(0.45)
    assert results[0].word_confidences == pytest.approx((0.8, 0.6))
    assert results[0].word_timings == ((0.1, 0.3), (0.3, 0.55))
    assert results[1].confidence == pytest.approx(0.8)
    assert results[1].speech_duration_seconds == pytest.approx(0.8)
    assert results[1].segment_peak_energy == pytest.approx(0.1, abs=0.001)
    assert results[1].word_confidences == pytest.approx((0.9, 0.7, 0.8))
    assert len(MetadataRecognizer.instances) == 1
    assert MetadataRecognizer.instances[0].words_enabled
    assert MetadataRecognizer.instances[0].partial_words_enabled


def test_optional_vosk_metadata_configuration_failure_is_non_fatal() -> None:
    class LegacyRecognizer:
        def SetWords(self, _enabled: bool) -> None:
            raise RuntimeError("unsupported")

        def SetPartialWords(self, _enabled: bool) -> None:
            raise RuntimeError("unsupported")

    SpeechRecognizer._enable_word_metadata(LegacyRecognizer())


def test_duplicate_removal_keeps_word_metadata_aligned() -> None:
    parsed = SpeechRecognizer._parse_recognition(
        '{"text":"echo echo user",'
        '"result":['
        '{"word":"echo","conf":0.95,"start":0.0,"end":0.2},'
        '{"word":"echo","conf":0.95,"start":0.2,"end":0.4},'
        '{"word":"user","conf":0.1,"start":0.4,"end":0.6}]}',
        "text",
    )

    deduplicated = SpeechRecognizer._deduplicate_parsed(parsed)

    assert deduplicated.text == "echo user"
    assert deduplicated.word_confidences == pytest.approx((0.95, 0.1))
    assert deduplicated.confidence == pytest.approx(0.525)


def test_legacy_text_only_result_parser_remains_compatible() -> None:
    assert SpeechRecognizer._parse_result('{"text":"legacy text"}', "text") == "legacy text"


def test_recognition_result_provenance_defaults_preserve_legacy_construction() -> None:
    result = RecognitionResult("legacy", is_final=False, frame_energy=0.2)

    assert result.segment_id is None
    assert result.segment_started_at is None
    assert result.energy_reemit is False
    assert result.confidence is None
    assert result.speech_duration_seconds is None
    assert result.segment_peak_energy is None


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


class _DeadlineStream(FakeStream):
    """Stream whose reads consume simulated time proportional to frame count."""

    def __init__(self, clock: _SimulatedClock, rate: int) -> None:
        super().__init__()
        self._clock = clock
        self._rate = rate
        self.requested_frames: list[int] = []

    def read(self, chunk: int, *, exception_on_overflow: bool) -> bytes:
        assert exception_on_overflow is False
        self.requested_frames.append(chunk)
        self._clock.advance(chunk / self._rate)
        return b"\x00\x00" * chunk


class _SimulatedClock:
    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def test_capture_does_not_overshoot_timeout_by_a_whole_chunk() -> None:
    """A blocking read must not extend the nominal listening deadline."""

    clock = _SimulatedClock()
    audio = FakeAudio()
    rate = 16_000
    chunk = 1_600
    audio.stream = _DeadlineStream(clock, rate)
    recognizer = SpeechRecognizer(
        model=object(),
        audio_interface=audio,
        recognizer_factory=PendingRecognizer,
        rate=rate,
        chunk=chunk,
        clock=clock,
    )

    timeout = 0.25
    list(recognizer.listen_events(timeout=timeout))

    # Every read is clamped to the time actually remaining, so total simulated
    # capture time stays within the deadline plus the 10 ms read floor.
    assert clock.now <= timeout + (160 / rate) + 1e-9
    assert max(audio.stream.requested_frames) <= chunk
