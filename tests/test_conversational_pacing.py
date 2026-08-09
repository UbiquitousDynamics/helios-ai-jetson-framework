from __future__ import annotations

import threading
import wave
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest

import config
from assistant import VoiceAssistant
from audio.backchannel import BackchannelSession
from audio.tts import PiperTTS


EVENT_TIMEOUT_SECONDS = 2.0


class NeverPlayingTTS:
    def __init__(self) -> None:
        self.phrases: list[str] = []

    def speak_preloaded(self, phrase: str) -> bool:
        self.phrases.append(phrase)
        return True


class ScopedRaceTTS:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.allow_playback_setup = threading.Event()
        self.cancellation: threading.Event | None = None
        self.played = False
        self.global_interrupt_calls = 0

    def speak_preloaded(
        self,
        _phrase: str,
        *,
        cancellation: threading.Event,
    ) -> bool:
        self.cancellation = cancellation
        self.entered.set()
        assert self.allow_playback_setup.wait(timeout=EVENT_TIMEOUT_SECONDS)
        if cancellation.is_set():
            return False
        self.played = True
        return True

    def interrupt(self) -> bool:
        self.global_interrupt_calls += 1
        return True


class InterruptiblePreloadedTTS:
    def __init__(self) -> None:
        self.started: Queue[str] = Queue()
        self.events: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._active_phrase: str | None = None
        self._active_release: threading.Event | None = None
        self.closed = False

    def speak_preloaded(
        self,
        phrase: str,
        *,
        cancellation: threading.Event,
    ) -> bool:
        release = cancellation
        with self._lock:
            self._active_phrase = phrase
            self._active_release = release
            self.events.append(("backchannel_started", phrase))
        self.started.put(phrase)

        released = release.wait(timeout=EVENT_TIMEOUT_SECONDS)
        with self._lock:
            if released:
                self.events.append(("interrupt", phrase))
            outcome = "backchannel_stopped" if released else "backchannel_timed_out"
            self.events.append((outcome, phrase))
            if self._active_release is release:
                self._active_phrase = None
                self._active_release = None
        return released

    def interrupt(self) -> bool:
        with self._lock:
            if self._active_release is None or self._active_phrase is None:
                return False
            self._active_release.set()
            return True

    def record_real_speech(self, phrase: str) -> None:
        with self._lock:
            self.events.append(("real_speech", phrase))

    def close(self) -> None:
        self.closed = True


class FirstSpeechAPI:
    def __init__(self, tts: InterruptiblePreloadedTTS) -> None:
        self.tts = tts
        self.backchannels: list[str] = []
        self.closed = False

    def talk(
        self,
        message: str,
        context: str | None = None,
        before_first_speech: Callable[[], None] | None = None,
    ) -> str:
        assert message
        assert context is None
        assert before_first_speech is not None

        phrase = self.tts.started.get(timeout=EVENT_TIMEOUT_SECONDS)
        self.backchannels.append(phrase)
        before_first_speech()
        self.tts.record_real_speech(phrase)
        return "model response"

    def close(self) -> None:
        self.closed = True


class ClosableStub:
    def close(self) -> None:
        pass


class CountingVoice:
    def __init__(self) -> None:
        self.synthesized: list[str] = []

    def synthesize(self, text: str, wav_file: wave.Wave_write) -> None:
        self.synthesized.append(text)
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x01\x00" * 32)


class RecordingAudioBackend:
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


def test_fast_first_speech_supersedes_backchannel_without_playback() -> None:
    tts = NeverPlayingTTS()
    with ThreadPoolExecutor(max_workers=1) as executor:
        session = BackchannelSession(
            tts=tts,
            phrase="Sure.",
            delay_seconds=0.25,
            executor=executor,
        )

        session.supersede()

    assert tts.phrases == []
    assert session.triggered is False
    assert session.played is False
    assert session.is_playing is False


def test_slow_backchannel_is_stopped_before_real_speech() -> None:
    tts = InterruptiblePreloadedTTS()
    with ThreadPoolExecutor(max_workers=1) as executor:
        session = BackchannelSession(
            tts=tts,
            phrase="One moment.",
            delay_seconds=0.01,
            executor=executor,
        )

        phrase = tts.started.get(timeout=EVENT_TIMEOUT_SECONDS)
        session.supersede()
        tts.record_real_speech(phrase)

    assert tts.events == [
        ("backchannel_started", "One moment."),
        ("interrupt", "One moment."),
        ("backchannel_stopped", "One moment."),
        ("real_speech", "One moment."),
    ]
    assert session.triggered is True
    assert session.played is True
    assert session.is_playing is False


def test_scoped_cancellation_cannot_miss_pre_playback_setup_race() -> None:
    tts = ScopedRaceTTS()
    with ThreadPoolExecutor(max_workers=1) as executor:
        session = BackchannelSession(
            tts=tts,
            phrase="One moment.",
            delay_seconds=0.01,
            executor=executor,
        )
        assert tts.entered.wait(timeout=EVENT_TIMEOUT_SECONDS)

        superseded = threading.Thread(target=session.supersede)
        superseded.start()
        assert tts.cancellation is not None
        assert tts.cancellation.wait(timeout=EVENT_TIMEOUT_SECONDS)
        tts.allow_playback_setup.set()
        superseded.join(timeout=EVENT_TIMEOUT_SECONDS)

    assert not superseded.is_alive()
    assert tts.played is False
    assert tts.global_interrupt_calls == 0
    assert session.played is False


@pytest.mark.parametrize(
    ("language", "expected_phrases"),
    [
        ("it", ("Certo.", "Un momento.", "Vediamo.")),
        ("en", ("Sure.", "One moment.", "Let's see.")),
    ],
)
def test_voice_assistant_rotates_active_language_backchannels_before_real_speech(
    language: str,
    expected_phrases: tuple[str, ...],
) -> None:
    tts = InterruptiblePreloadedTTS()
    api = FirstSpeechAPI(tts)
    settings = config.Settings(
        project_root=config.PROJECT_ROOT,
        language=language,
        barge_in_enabled=True,
        backchannel_delay_seconds=0.01,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        assistant = VoiceAssistant(
            settings=settings,
            tts=tts,
            sound_player=ClosableStub(),
            api_client=api,
            speech_recognizer=ClosableStub(),
            barge_in_detector=object(),
            sound_executor=executor,
        )
        try:
            for turn in range(len(expected_phrases) + 1):
                assert (
                    assistant.process_command(f"Emilia, answer question {turn}") == "model response"
                )
        finally:
            assistant.close()

    expected_rotation = [*expected_phrases, expected_phrases[0]]
    assert assistant.profile.backchannel_phrases == expected_phrases
    assert api.backchannels == expected_rotation
    assert tts.events == [
        event
        for phrase in expected_rotation
        for event in (
            ("backchannel_started", phrase),
            ("interrupt", phrase),
            ("backchannel_stopped", phrase),
            ("real_speech", phrase),
        )
    ]


def test_piper_preloaded_playback_reuses_cached_wave_without_synthesis() -> None:
    voice = CountingVoice()
    backend = RecordingAudioBackend()
    tts = PiperTTS("unused.onnx", voice=voice, audio_backend=backend)

    assert tts.preload_phrases(("Sure.", "Sure.")) == ("Sure.", "Sure.")
    assert voice.synthesized == ["Sure."]
    assert tts.has_preloaded_phrase("Sure.") is True

    assert tts.speak_preloaded("Sure.") is True
    assert tts.speak_preloaded("Sure.") is True

    assert voice.synthesized == ["Sure."]
    assert backend.calls == [
        (b"\x01\x00" * 32, 16_000, 1, 2),
        (b"\x01\x00" * 32, 16_000, 1, 2),
    ]
    assert tts.speak_preloaded("Not cached.") is False
    assert voice.synthesized == ["Sure."]
