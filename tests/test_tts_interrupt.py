from __future__ import annotations

import threading
import wave

import pytest

from audio.tts import PiperTTS, SoundDeviceBackend, TTSError


FRAME_COUNT = 16_000


class LongVoice:
    def synthesize(self, _text: str, wav_file: wave.Wave_write) -> None:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x01\x00" * FRAME_COUNT)


class ControllableBackend:
    def __init__(self) -> None:
        self.first_chunk_written = threading.Event()
        self.calls = 0
        self.frames_written: list[int] = []

    def play(self, *_args: object) -> None:
        raise AssertionError("interrupt-aware playback should be used")

    def play_interruptibly(
        self,
        frames: bytes,
        _sample_rate: int,
        channels: int,
        sample_width: int,
        interrupt_event: threading.Event,
    ) -> int:
        self.calls += 1
        total_frames = len(frames) // (channels * sample_width)
        if self.calls == 1:
            frames_written = total_frames // 4
            self.first_chunk_written.set()
            interrupt_event.wait(timeout=1)
            if not interrupt_event.is_set():
                frames_written = total_frames
        else:
            frames_written = total_frames
        self.frames_written.append(frames_written)
        return frames_written


class RecordingBlockingVoice:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def synthesize(self, _text: str, wav_file: wave.Wave_write) -> None:
        self.started.set()
        assert self.release.wait(timeout=1)
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x01\x00" * FRAME_COUNT)


class CancellingPreloadVoice:
    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_event = stop_event
        self.synthesized: list[str] = []

    def synthesize(self, text: str, wav_file: wave.Wave_write) -> None:
        self.synthesized.append(text)
        self.stop_event.set()
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x01\x00" * FRAME_COUNT)


class NonInterruptibleRecordingBackend:
    def __init__(self) -> None:
        self.play_calls = 0
        self.close_calls = 0

    def play(self, *_args: object) -> None:
        self.play_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class InterruptingRawOutputStream:
    def __init__(self, interrupt_event: threading.Event) -> None:
        self.interrupt_event = interrupt_event
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self.writes: list[bytes] = []

    def start(self) -> None:
        self.started += 1

    def write(self, frames: bytes) -> bool:
        self.writes.append(frames)
        if len(self.writes) == 1:
            self.interrupt_event.set()
        return False

    def stop(self) -> None:
        self.stopped += 1

    def close(self) -> None:
        self.closed += 1


class FakeSoundDevice:
    def __init__(self, interrupt_event: threading.Event) -> None:
        self.stream = InterruptingRawOutputStream(interrupt_event)

    def RawOutputStream(self, **_kwargs: object) -> InterruptingRawOutputStream:
        return self.stream


class PausingRawOutputStream:
    def __init__(self, pause_event: threading.Event) -> None:
        self.pause_event = pause_event
        self.first_chunk_written = threading.Event()
        self.paused = threading.Event()
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self.writes: list[bytes] = []

    def start(self) -> None:
        self.started += 1

    def write(self, frames: bytes) -> bool:
        self.writes.append(frames)
        if len(self.writes) == 1:
            self.pause_event.set()
            self.first_chunk_written.set()
        return False

    def stop(self) -> None:
        self.stopped += 1
        if self.pause_event.is_set():
            self.paused.set()

    def close(self) -> None:
        self.closed += 1


class PausingSoundDevice:
    def __init__(self, pause_event: threading.Event) -> None:
        self.stream = PausingRawOutputStream(pause_event)

    def RawOutputStream(self, **_kwargs: object) -> PausingRawOutputStream:
        return self.stream


class PauseAwareRecordingBackend:
    def __init__(self) -> None:
        self.playback_entered = threading.Event()
        self.frames_written: list[int] = []
        self.close_calls = 0

    def play(self, *_args: object) -> None:
        raise AssertionError("pause-aware playback should be used")

    def play_interruptibly(
        self,
        frames: bytes,
        _sample_rate: int,
        channels: int,
        sample_width: int,
        interrupt_event: threading.Event,
        *,
        pause_event: threading.Event | None = None,
    ) -> int:
        assert pause_event is not None
        self.playback_entered.set()
        while pause_event.is_set():
            if interrupt_event.wait(0.02):
                self.frames_written.append(0)
                return 0
        frame_count = 0 if interrupt_event.is_set() else len(frames) // (channels * sample_width)
        self.frames_written.append(frame_count)
        return frame_count

    def close(self) -> None:
        self.close_calls += 1


def test_interrupt_cuts_playback_short_and_next_speak_recovers() -> None:
    backend = ControllableBackend()
    tts = PiperTTS("unused.onnx", voice=LongVoice(), audio_backend=backend)

    playback = threading.Thread(target=tts.speak, args=("hello",))
    assert tts.is_speaking is False
    playback.start()
    assert backend.first_chunk_written.wait(timeout=1)
    assert tts.is_speaking is True

    assert tts.interrupt() is True
    playback.join(timeout=1)

    assert not playback.is_alive()
    assert tts.is_speaking is False
    assert backend.frames_written == [FRAME_COUNT // 4]
    assert tts.last_playback_frame_count == FRAME_COUNT // 4
    assert tts.last_playback_total_frames == FRAME_COUNT
    assert tts.last_playback_was_interrupted is True

    tts.speak("hello")

    assert tts.is_speaking is False
    assert backend.frames_written == [FRAME_COUNT // 4, FRAME_COUNT]
    assert tts.last_playback_frame_count == FRAME_COUNT
    assert tts.last_playback_total_frames == FRAME_COUNT
    assert tts.last_playback_was_interrupted is False


def test_interrupt_while_idle_does_not_cancel_future_playback() -> None:
    backend = ControllableBackend()
    backend.calls = 1
    tts = PiperTTS("unused.onnx", voice=LongVoice(), audio_backend=backend)

    assert tts.interrupt() is False
    tts.speak("hello")

    assert backend.frames_written == [FRAME_COUNT]
    assert tts.last_playback_was_interrupted is False


def test_interrupt_during_synthesis_prevents_the_buffer_from_playing() -> None:
    voice = RecordingBlockingVoice()
    backend = NonInterruptibleRecordingBackend()
    tts = PiperTTS("unused.onnx", voice=voice, audio_backend=backend)
    errors: list[BaseException] = []

    def speak() -> None:
        try:
            tts.speak("hello")
        except BaseException as error:  # pragma: no cover - diagnostic guard
            errors.append(error)

    speech = threading.Thread(target=speak)
    speech.start()
    assert voice.started.wait(timeout=1)

    assert tts.interrupt() is True
    voice.release.set()
    speech.join(timeout=1)

    assert not speech.is_alive()
    assert errors == []
    assert backend.play_calls == 0
    assert tts.last_playback_frame_count == 0
    assert tts.last_playback_total_frames == FRAME_COUNT


def test_preload_does_not_start_when_cancellation_was_already_requested() -> None:
    stop_event = threading.Event()
    stop_event.set()
    voice = CancellingPreloadVoice(stop_event)
    tts = PiperTTS("unused.onnx", voice=voice)

    assert tts.preload_phrases(("first", "second"), stop_event=stop_event) == ()
    assert voice.synthesized == []


def test_preload_stops_after_in_flight_synthesis_without_caching_its_result() -> None:
    stop_event = threading.Event()
    voice = CancellingPreloadVoice(stop_event)
    tts = PiperTTS("unused.onnx", voice=voice)

    assert tts.preload_phrases(("first", "second"), stop_event=stop_event) == ()
    assert voice.synthesized == ["first"]
    assert tts.has_preloaded_phrase("first") is False
    assert tts.has_preloaded_phrase("second") is False


def test_close_is_idempotent_and_rejects_future_operations() -> None:
    backend = NonInterruptibleRecordingBackend()
    tts = PiperTTS("unused.onnx", voice=LongVoice(), audio_backend=backend)

    tts.close()
    tts.close()

    assert backend.close_calls == 1
    with pytest.raises(TTSError, match="closed"):
        tts.speak("hello")
    with pytest.raises(TTSError, match="closed"):
        tts.preload_phrases(("Sure.",))


def test_sounddevice_backend_stops_between_chunks_and_recovers() -> None:
    interrupt_event = threading.Event()
    module = FakeSoundDevice(interrupt_event)
    backend = SoundDeviceBackend(sounddevice_module=module)
    frames = b"\x01\x00" * 4_000

    interrupted_frame_count = backend.play_interruptibly(
        frames,
        16_000,
        1,
        2,
        interrupt_event,
    )

    assert interrupted_frame_count == 1_600
    assert [len(chunk) for chunk in module.stream.writes] == [3_200]
    assert module.stream.stopped == 1

    complete_frame_count = backend.play_interruptibly(
        frames,
        16_000,
        1,
        2,
        threading.Event(),
    )

    assert complete_frame_count == 4_000
    assert [len(chunk) for chunk in module.stream.writes] == [
        3_200,
        3_200,
        3_200,
        1_600,
    ]
    assert module.stream.started == 2
    assert module.stream.stopped == 2


def test_sounddevice_backend_duck_resumes_without_replaying_pcm() -> None:
    interrupt_event = threading.Event()
    pause_event = threading.Event()
    module = PausingSoundDevice(pause_event)
    backend = SoundDeviceBackend(sounddevice_module=module)
    frames = b"\x01\x00" * 4_000
    result: list[int] = []

    playback = threading.Thread(
        target=lambda: result.append(
            backend.play_interruptibly(
                frames,
                16_000,
                1,
                2,
                interrupt_event,
                pause_event=pause_event,
            )
        )
    )
    playback.start()

    assert module.stream.first_chunk_written.wait(timeout=1)
    assert module.stream.paused.wait(timeout=1)
    assert playback.is_alive()
    assert [len(chunk) for chunk in module.stream.writes] == [3_200]

    pause_event.clear()
    playback.join(timeout=1)

    assert not playback.is_alive()
    assert result == [4_000]
    assert [len(chunk) for chunk in module.stream.writes] == [3_200, 3_200, 1_600]
    assert module.stream.started == 2
    assert module.stream.stopped == 2


def test_sounddevice_backend_interrupt_while_ducked_exits_promptly() -> None:
    interrupt_event = threading.Event()
    pause_event = threading.Event()
    module = PausingSoundDevice(pause_event)
    backend = SoundDeviceBackend(sounddevice_module=module)
    frames = b"\x01\x00" * 4_000
    result: list[int] = []

    playback = threading.Thread(
        target=lambda: result.append(
            backend.play_interruptibly(
                frames,
                16_000,
                1,
                2,
                interrupt_event,
                pause_event=pause_event,
            )
        )
    )
    playback.start()

    assert module.stream.first_chunk_written.wait(timeout=1)
    assert module.stream.paused.wait(timeout=1)
    interrupt_event.set()
    playback.join(timeout=1)

    assert not playback.is_alive()
    assert result == [1_600]
    assert [len(chunk) for chunk in module.stream.writes] == [3_200]


def test_piper_duck_during_synthesis_blocks_playback_until_resume() -> None:
    voice = RecordingBlockingVoice()
    backend = PauseAwareRecordingBackend()
    tts = PiperTTS("unused.onnx", voice=voice, audio_backend=backend)
    errors: list[BaseException] = []

    def speak() -> None:
        try:
            tts.speak("hello")
        except BaseException as error:  # pragma: no cover - diagnostic guard
            errors.append(error)

    speech = threading.Thread(target=speak)
    speech.start()
    assert voice.started.wait(timeout=1)

    assert tts.duck() is True
    voice.release.set()
    assert backend.playback_entered.wait(timeout=1)
    assert backend.frames_written == []
    assert speech.is_alive()

    assert tts.resume() is True
    speech.join(timeout=1)

    assert not speech.is_alive()
    assert errors == []
    assert backend.frames_written == [FRAME_COUNT]
    assert tts.last_playback_was_interrupted is False


def test_piper_close_wakes_playback_waiting_in_duck() -> None:
    backend = PauseAwareRecordingBackend()
    tts = PiperTTS("unused.onnx", voice=LongVoice(), audio_backend=backend)
    speech_errors: list[BaseException] = []
    close_errors: list[BaseException] = []

    def speak() -> None:
        try:
            tts.speak("hello")
        except BaseException as error:  # pragma: no cover - diagnostic guard
            speech_errors.append(error)

    def close() -> None:
        try:
            tts.close()
        except BaseException as error:  # pragma: no cover - diagnostic guard
            close_errors.append(error)

    assert tts.duck() is True
    speech = threading.Thread(target=speak)
    speech.start()
    assert backend.playback_entered.wait(timeout=1)

    closing = threading.Thread(target=close)
    closing.start()
    closing.join(timeout=1)
    speech.join(timeout=1)

    assert not closing.is_alive()
    assert not speech.is_alive()
    assert close_errors == []
    assert speech_errors == []
    assert backend.frames_written == [0]
    assert backend.close_calls == 1
