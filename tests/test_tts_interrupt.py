from __future__ import annotations

import threading
import wave

from audio.tts import PiperTTS, SoundDeviceBackend


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
