from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import config
from assistant import VoiceAssistant
from recognizer.barge_in_detector import BargeInDetector
from recognizer.speech_recognizer import RecognitionResult


class BlockingTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.interrupt_calls = 0
        self._speaking = threading.Event()
        self._release_first_playback = threading.Event()

    @property
    def is_speaking(self) -> bool:
        return self._speaking.is_set()

    def speak(self, text: str) -> None:
        self.spoken.append(text)
        self._speaking.set()
        try:
            if len(self.spoken) == 1:
                assert self._release_first_playback.wait(timeout=2)
        finally:
            self._speaking.clear()

    def interrupt(self) -> bool:
        self.interrupt_calls += 1
        was_speaking = self._speaking.is_set()
        self._release_first_playback.set()
        return was_speaking

    def close(self) -> None:
        pass


class CancellableAPI:
    def __init__(self, tts: BlockingTTS) -> None:
        self.tts = tts
        self.messages: list[str] = []
        self.cancel_calls = 0
        self._lock = threading.Lock()
        self._active: threading.Event | None = None
        self.response_finished = threading.Event()

    def talk(self, message: str, context: str | None = None) -> str:
        assert context is None
        cancellation = threading.Event()
        with self._lock:
            self._active = cancellation
        self.messages.append(message)
        try:
            self.tts.speak(f"response for {message}")
            if cancellation.is_set():
                raise RuntimeError("request cancelled")
            self.tts.speak("queued fragment")
            return f"complete response for {message}"
        finally:
            with self._lock:
                if self._active is cancellation:
                    self._active = None
            self.response_finished.set()

    def cancel_current(self) -> None:
        self.cancel_calls += 1
        with self._lock:
            active = self._active
        if active is not None:
            active.set()

    def close(self) -> None:
        pass


class BargeInRecognizer:
    def __init__(self, api: CancellableAPI) -> None:
        self.api = api
        self._barge_events_emitted = False

    def listen_once(self, timeout: float) -> RecognitionResult:
        assert timeout > 0
        return RecognitionResult("Emilia, prima domanda", is_final=True)

    def listen_events(
        self,
        timeout: float | None,
        *,
        stop_event: object,
    ):
        assert timeout is None
        if self._barge_events_emitted:
            return
        self._barge_events_emitted = True
        yield RecognitionResult("nuova", is_final=False)
        assert self.api.response_finished.wait(timeout=1)
        # Cancelling the first response must not stop/flush the microphone
        # before the user finishes the interruption utterance.
        assert getattr(stop_event, "is_set")() is False
        yield RecognitionResult("nuova domanda", is_final=True)

    def close(self) -> None:
        pass


class SilentSoundPlayer:
    def play_sound(self, _sound_file: str) -> None:
        pass

    def close(self) -> None:
        pass


def test_barge_in_interrupts_audio_and_stream_then_processes_follow_up() -> None:
    tts = BlockingTTS()
    api = CancellableAPI(tts)
    settings = config.Settings(
        project_root=config.PROJECT_ROOT,
        language="it",
        barge_in_enabled=True,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        assistant = VoiceAssistant(
            settings=settings,
            tts=tts,
            sound_player=SilentSoundPlayer(),
            api_client=api,
            speech_recognizer=BargeInRecognizer(api),
            barge_in_detector=BargeInDetector(),
            sound_executor=executor,
        )

        assert assistant.run_once()
        assert tts.interrupt_calls == 1
        assert api.cancel_calls == 1
        assistant.close()

    assert tts.interrupt_calls == 2
    assert api.cancel_calls == 2
    assert api.messages == ["prima domanda", "nuova domanda"]
    assert tts.spoken == [
        "response for prima domanda",
        "response for nuova domanda",
        "queued fragment",
    ]
