"""Vosk speech-recognition boundary with deterministic audio cleanup."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)


class SpeechRecognitionError(RuntimeError):
    """Raised when microphone capture or Vosk recognition fails."""


@dataclass(frozen=True)
class RecognitionResult:
    """A partial or final recognition event."""

    text: str
    is_final: bool


class SpeechRecognizer:
    def __init__(
        self,
        model_path: str | Path = config.VOSK_MODEL_PATH,
        *,
        model: Any | None = None,
        audio_interface: Any | None = None,
        recognizer_factory: Callable[[Any, int], Any] | None = None,
        audio_format: Any | None = None,
        rate: int = 16_000,
        chunk: int = 4_000,
        clock: Callable[[], float] = time.monotonic,
        owns_audio: bool | None = None,
    ) -> None:
        if rate <= 0 or chunk <= 0:
            raise ValueError("rate and chunk must be greater than zero")

        self.model_path = Path(model_path)
        self.model = model
        self.p = audio_interface
        self._recognizer_factory = recognizer_factory
        self._audio_format = audio_format
        self.rate = rate
        self.chunk = chunk
        self._clock = clock
        self._owns_audio = audio_interface is None if owns_audio is None else owns_audio
        self._closed = False
        self._runtime_lock = threading.RLock()
        self._prepare_lock = threading.Lock()
        self._prepare_thread: threading.Thread | None = None

    @staticmethod
    def remove_consecutive_duplicates(text: str) -> str:
        words = text.split()
        if not words:
            return text
        filtered = [words[0]]
        for word in words[1:]:
            if word != filtered[-1]:
                filtered.append(word)
        return " ".join(filtered)

    def _ensure_runtime(self) -> None:
        with self._runtime_lock:
            self._ensure_runtime_unlocked()

    def _ensure_runtime_unlocked(self) -> None:
        if self._closed:
            raise SpeechRecognitionError("Speech recognizer is closed")

        if self.model is None or self._recognizer_factory is None:
            try:
                from vosk import KaldiRecognizer, Model
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise SpeechRecognitionError(
                    "The 'vosk' package is required for speech recognition"
                ) from exc
            if self.model is None:
                try:
                    self.model = Model(str(self.model_path))
                except Exception as exc:
                    raise SpeechRecognitionError(
                        f"Unable to load Vosk model: {self.model_path}"
                    ) from exc
            if self._recognizer_factory is None:
                self._recognizer_factory = KaldiRecognizer

        if self.p is None:
            try:
                import pyaudio
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise SpeechRecognitionError(
                    "The 'PyAudio' package is required for microphone capture"
                ) from exc
            try:
                self.p = pyaudio.PyAudio()
            except Exception as exc:
                raise SpeechRecognitionError("Unable to initialize the audio input device") from exc
            self._audio_format = pyaudio.paInt16
            self._owns_audio = True

        if self._audio_format is None:
            # PyAudio's paInt16 value. Injected audio adapters can ignore it.
            self._audio_format = 8

    def prepare_async(self) -> threading.Thread | None:
        """Load Vosk and initialize PyAudio without opening an input stream."""

        with self._prepare_lock:
            if self._closed:
                return None
            if (
                self.model is not None
                and self._recognizer_factory is not None
                and self.p is not None
                and self._audio_format is not None
            ):
                return self._prepare_thread
            if self._prepare_thread is not None and self._prepare_thread.is_alive():
                return self._prepare_thread

            def prepare() -> None:
                try:
                    self._ensure_runtime()
                    logger.info("Speech recognizer prepared in background")
                except SpeechRecognitionError:
                    # Listening retries initialization synchronously and exposes
                    # the normal recoverable error through the runtime loop.
                    logger.warning("Background speech-recognizer preparation failed")

            thread = threading.Thread(
                target=prepare,
                name="helios-speech-prepare",
                daemon=True,
            )
            self._prepare_thread = thread
            thread.start()
            return thread

    @staticmethod
    def _parse_result(payload: str, key: str) -> str:
        try:
            parsed = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SpeechRecognitionError("Recognizer returned invalid JSON") from exc
        return str(parsed.get(key) or "")

    def listen_events(self, timeout: float | None = None) -> Iterator[RecognitionResult]:
        """Yield distinct partial and final recognition events.

        The microphone stream is always stopped and closed, including when a
        consumer stops after the first final result.
        """

        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self._ensure_runtime()
        assert self.p is not None
        assert self._recognizer_factory is not None

        stream: Any | None = None
        last_partial = ""
        last_final = ""
        start_time = self._clock()
        try:
            stream = self.p.open(
                format=self._audio_format,
                channels=1,
                rate=self.rate,
                input=True,
                frames_per_buffer=self.chunk,
            )
            stream.start_stream()
            recognizer = self._recognizer_factory(self.model, self.rate)
            logger.info("Listening for speech")

            while timeout is None or self._clock() - start_time < timeout:
                data = stream.read(self.chunk, exception_on_overflow=False)
                if recognizer.AcceptWaveform(data):
                    text = self._parse_result(recognizer.Result(), "text")
                    clean_text = self.remove_consecutive_duplicates(text).strip()
                    if clean_text and clean_text != last_final:
                        last_final = clean_text
                        yield RecognitionResult(clean_text, is_final=True)
                else:
                    partial = self._parse_result(recognizer.PartialResult(), "partial")
                    clean_partial = self.remove_consecutive_duplicates(partial).strip()
                    if clean_partial and clean_partial != last_partial:
                        last_partial = clean_partial
                        yield RecognitionResult(clean_partial, is_final=False)

            final_result = getattr(recognizer, "FinalResult", None)
            if callable(final_result):
                text = self._parse_result(final_result(), "text")
                clean_text = self.remove_consecutive_duplicates(text).strip()
                if clean_text and clean_text != last_final:
                    yield RecognitionResult(clean_text, is_final=True)
        except SpeechRecognitionError:
            raise
        except Exception as exc:
            raise SpeechRecognitionError("Speech recognition failed") from exc
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    logger.warning("Unable to stop microphone stream", exc_info=True)
                try:
                    stream.close()
                except Exception:
                    logger.warning("Unable to close microphone stream", exc_info=True)

    def listen_once(self, timeout: float | None = None) -> RecognitionResult | None:
        """Return on the first final result, or the latest partial at timeout."""

        latest: RecognitionResult | None = None
        events = self.listen_events(timeout=timeout)
        try:
            for result in events:
                latest = result
                if result.is_final:
                    return result
        finally:
            events.close()
        return latest

    def listen(self, timeout: float | None = None) -> Iterator[str]:
        """Compatibility generator yielding only event text."""

        events = self.listen_events(timeout=timeout)
        try:
            for result in events:
                yield result.text
        finally:
            events.close()

    def close(self) -> None:
        with self._runtime_lock:
            if self._closed:
                return
            if self.p is not None and self._owns_audio:
                try:
                    self.p.terminate()
                except Exception as exc:
                    raise SpeechRecognitionError("Unable to terminate the audio interface") from exc
            self._closed = True

    def __enter__(self) -> SpeechRecognizer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
