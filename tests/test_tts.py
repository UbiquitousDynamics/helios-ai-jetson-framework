from __future__ import annotations

import subprocess
import threading
import wave
from pathlib import Path

import pytest

from audio.sound_player import SoundPlaybackError, SoundPlayer
from audio.tts import (
    AudioSynthesisError,
    PiperTTS,
    Pyttsx3TTS,
    SoundDeviceBackend,
)


class FakeVoice:
    def synthesize(self, text: str, wav_file: wave.Wave_write) -> None:
        assert text == "hello"
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x01\x00\x02\x00")


class FakeModernVoice:
    def synthesize(self, *_args: object) -> None:
        raise AssertionError("Piper 1.3+ synthesize() must be consumed as an iterator")

    def synthesize_wav(self, text: str, wav_file: wave.Wave_write) -> None:
        assert text == "hello"
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22_050)
        wav_file.writeframes(b"\x03\x00\x04\x00")


class FailingVoice:
    def synthesize(self, _text: str, _wav_file: wave.Wave_write) -> None:
        raise RuntimeError("native synthesis failure")


class CapturingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, int, int, int]] = []

    def play(
        self,
        frames: bytes,
        sample_rate: int,
        channels: int,
        sample_width: int,
    ) -> None:
        self.calls.append((frames, sample_rate, channels, sample_width))


class BlockingBackend(CapturingBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def play(
        self,
        frames: bytes,
        sample_rate: int,
        channels: int,
        sample_width: int,
    ) -> None:
        super().play(frames, sample_rate, channels, sample_width)
        self.started.set()
        assert self.release.wait(timeout=1)


class FakeRawOutputStream:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self.writes: list[bytes] = []

    def start(self) -> None:
        self.started += 1

    def write(self, frames: bytes) -> bool:
        self.writes.append(frames)
        return False

    def stop(self) -> None:
        self.stopped += 1

    def close(self) -> None:
        self.closed += 1


class FakeSoundDevice:
    def __init__(self) -> None:
        self.streams: list[FakeRawOutputStream] = []

    def RawOutputStream(self, **kwargs: object) -> FakeRawOutputStream:
        stream = FakeRawOutputStream(**kwargs)
        self.streams.append(stream)
        return stream


def test_piper_plays_pcm_frames_not_the_wav_header() -> None:
    backend = CapturingBackend()
    tts = PiperTTS("unused.onnx", voice=FakeVoice(), audio_backend=backend)

    tts.speak("hello")

    assert backend.calls == [(b"\x01\x00\x02\x00", 16_000, 1, 2)]
    assert not backend.calls[0][0].startswith(b"RIFF")


def test_piper_exposes_only_active_and_most_recent_playback_text() -> None:
    backend = BlockingBackend()
    tts = PiperTTS("unused.onnx", voice=FakeVoice(), audio_backend=backend)
    speaker = threading.Thread(target=tts.speak, args=("hello",))

    speaker.start()
    assert backend.started.wait(timeout=1)
    assert tts.active_playback_text == "hello"
    assert tts.last_playback_text == "hello"

    backend.release.set()
    speaker.join(timeout=1)
    assert not speaker.is_alive()
    assert tts.active_playback_text is None
    assert tts.last_playback_text == "hello"


def test_piper_supports_modern_synthesize_wav_api() -> None:
    backend = CapturingBackend()
    tts = PiperTTS("unused.onnx", voice=FakeModernVoice(), audio_backend=backend)

    tts.speak("hello")

    assert backend.calls == [(b"\x03\x00\x04\x00", 22_050, 1, 2)]


def test_piper_skips_punctuation_only_fragment() -> None:
    backend = CapturingBackend()
    tts = PiperTTS("unused.onnx", voice=FakeVoice(), audio_backend=backend)

    tts.speak(":")

    assert backend.calls == []


def test_sounddevice_backend_reuses_stream_for_matching_pcm_format() -> None:
    module = FakeSoundDevice()
    backend = SoundDeviceBackend(sounddevice_module=module)

    backend.play(b"\x01\x00", 16_000, 1, 2)
    backend.play(b"\x02\x00", 16_000, 1, 2)

    assert len(module.streams) == 1
    stream = module.streams[0]
    assert stream.kwargs == {
        "samplerate": 16_000,
        "channels": 1,
        "dtype": "int16",
    }
    assert stream.writes == [b"\x01\x00", b"\x02\x00"]
    assert stream.started == 2
    assert stream.stopped == 2

    backend.close()
    assert stream.closed == 1


def test_sounddevice_backend_reopens_stream_when_pcm_format_changes() -> None:
    module = FakeSoundDevice()
    backend = SoundDeviceBackend(sounddevice_module=module)

    backend.play(b"\x01\x00", 16_000, 1, 2)
    backend.play(b"\x01\x00", 22_050, 1, 2)

    assert len(module.streams) == 2
    assert module.streams[0].closed == 1


def test_piper_preserves_failure_that_occurs_before_wav_header() -> None:
    tts = PiperTTS("unused.onnx", voice=FailingVoice())

    with pytest.raises(AudioSynthesisError) as captured:
        tts.synthesize_wave("hello")

    assert isinstance(captured.value.__cause__, RuntimeError)
    assert str(captured.value.__cause__) == "native synthesis failure"


def test_historical_class_name_is_an_alias() -> None:
    assert Pyttsx3TTS is PiperTTS


def test_sound_player_checks_capability_only_when_used(tmp_path: Path) -> None:
    checks: list[str] = []
    player = SoundPlayer(executable_resolver=lambda executable: checks.append(executable) or None)
    assert checks == []

    sound = tmp_path / "cue.wav"
    sound.write_bytes(b"not needed by the fake player")
    with pytest.raises(SoundPlaybackError, match="not available"):
        player.play_sound(sound)
    assert checks == ["aplay"]


def test_sound_player_bounds_subprocess_runtime(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def runner(_command: list[str], **kwargs: object) -> None:
        calls.append(kwargs)
        raise subprocess.TimeoutExpired("aplay", kwargs["timeout"])

    sound = tmp_path / "cue.wav"
    sound.write_bytes(b"fake")
    player = SoundPlayer(
        executable_resolver=lambda _executable: "/usr/bin/aplay",
        runner=runner,
        timeout=2.5,
    )

    with pytest.raises(SoundPlaybackError, match="Unable to play"):
        player.play_sound(sound)
    assert calls[0]["timeout"] == 2.5
