"""Vosk speech-recognition boundary with deterministic audio cleanup."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config
from recognizer.barge_in_detector import pcm16_rms

logger = logging.getLogger(__name__)

_PARTIAL_ENERGY_REEMIT_DELTA = 0.02
# Floor for deadline-clamped microphone reads (10 ms at 16 kHz). Prevents the
# capture loop from degenerating into single-frame reads near the deadline.
_MINIMUM_READ_FRAMES = 160


class SpeechRecognitionError(RuntimeError):
    """Raised when microphone capture or Vosk recognition fails."""


@dataclass(frozen=True)
class RecognitionResult:
    """A partial or final recognition event."""

    text: str
    is_final: bool
    frame_energy: float | None = None
    segment_id: int | None = None
    segment_started_at: float | None = None
    energy_reemit: bool = False
    confidence: float | None = None
    speech_duration_seconds: float | None = None
    segment_peak_energy: float | None = None
    word_confidences: tuple[float | None, ...] = ()
    word_timings: tuple[tuple[float, float] | None, ...] = ()


@dataclass(frozen=True)
class _ParsedRecognition:
    """Content-free metadata extracted from one Vosk JSON payload."""

    text: str
    confidence: float | None
    speech_duration_seconds: float | None
    word_confidences: tuple[float | None, ...]
    word_timings: tuple[tuple[float, float] | None, ...]


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
        # 100 ms at 16 kHz. The previous 4,000-sample (250 ms) buffer set the
        # floor for barge-in reaction time and for the RMS averaging window
        # feeding echo suppression: an interruption could not be noticed sooner
        # than the frame carrying it. Vosk accepts smaller buffers unchanged and
        # per-sample cost is identical, so only loop overhead grows.
        chunk: int = 1_600,
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

    @staticmethod
    def _deduplicate_parsed(parsed: _ParsedRecognition) -> _ParsedRecognition:
        """Deduplicate text and word metadata with identical index transforms."""

        words = parsed.text.split()
        if not words:
            return parsed
        kept_indices = [0]
        for index in range(1, len(words)):
            if words[index] != words[kept_indices[-1]]:
                kept_indices.append(index)
        text = " ".join(words[index] for index in kept_indices)
        confidences = tuple(
            parsed.word_confidences[index]
            for index in kept_indices
            if index < len(parsed.word_confidences)
        )
        timings = tuple(
            parsed.word_timings[index] for index in kept_indices if index < len(parsed.word_timings)
        )
        known_confidences = tuple(value for value in confidences if value is not None)
        known_timings = tuple(value for value in timings if value is not None)
        return _ParsedRecognition(
            text=text,
            confidence=(
                sum(known_confidences) / len(known_confidences) if known_confidences else None
            ),
            speech_duration_seconds=(
                max(end for _, end in known_timings) - min(start for start, _ in known_timings)
                if known_timings
                else None
            ),
            word_confidences=confidences,
            word_timings=timings,
        )

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
    def _parse_recognition(payload: str, key: str) -> _ParsedRecognition:
        try:
            parsed = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SpeechRecognitionError("Recognizer returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise SpeechRecognitionError("Recognizer returned invalid JSON")

        text = str(parsed.get(key) or "")
        detail_key = "partial_result" if key == "partial" else "result"
        raw_words = parsed.get(detail_key)
        if not isinstance(raw_words, list):
            return _ParsedRecognition(text, None, None, (), ())

        confidences: list[float] = []
        starts: list[float] = []
        ends: list[float] = []
        word_confidences: list[float | None] = []
        word_timings: list[tuple[float, float] | None] = []
        for raw_word in raw_words:
            if not isinstance(raw_word, dict):
                continue
            confidence = raw_word.get("conf")
            if (
                isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and math.isfinite(float(confidence))
                and 0 <= float(confidence) <= 1
            ):
                parsed_confidence: float | None = float(confidence)
                confidences.append(parsed_confidence)
            else:
                parsed_confidence = None
            word_confidences.append(parsed_confidence)
            start = raw_word.get("start")
            end = raw_word.get("end")
            if (
                isinstance(start, (int, float))
                and not isinstance(start, bool)
                and math.isfinite(float(start))
                and float(start) >= 0
                and isinstance(end, (int, float))
                and not isinstance(end, bool)
                and math.isfinite(float(end))
                and float(end) >= float(start)
            ):
                parsed_timing: tuple[float, float] | None = (
                    float(start),
                    float(end),
                )
                starts.append(parsed_timing[0])
                ends.append(parsed_timing[1])
            else:
                parsed_timing = None
            word_timings.append(parsed_timing)

        confidence = sum(confidences) / len(confidences) if confidences else None
        speech_duration_seconds = max(ends) - min(starts) if starts and ends else None
        return _ParsedRecognition(
            text,
            confidence,
            speech_duration_seconds,
            tuple(word_confidences),
            tuple(word_timings),
        )

    @staticmethod
    def _parse_result(payload: str, key: str) -> str:
        """Preserve the legacy text-only parser used by injected adapters."""

        return SpeechRecognizer._parse_recognition(payload, key).text

    @staticmethod
    def _enable_word_metadata(recognizer: Any) -> None:
        """Request optional Vosk word metadata without breaking older adapters."""

        for method_name in ("SetWords", "SetPartialWords"):
            configure = getattr(recognizer, method_name, None)
            if not callable(configure):
                continue
            try:
                configure(True)
            except Exception:
                # Word metadata improves barge-in validation, but recognition
                # must remain compatible with older Vosk/injected adapters.
                logger.debug(
                    "Recognizer does not support optional metadata method=%s",
                    method_name,
                )

    def listen_events(
        self,
        timeout: float | None = None,
        *,
        stop_event: threading.Event | None = None,
    ) -> Iterator[RecognitionResult]:
        """Yield distinct partial and final recognition events.

        When ``stop_event`` is set, capture ends after the current microphone
        read and Vosk's pending text is flushed through ``FinalResult``. This
        lets a coordinating thread stop one continuous recognition session
        without repeatedly closing and reopening the input stream.

        The microphone stream is always stopped and closed, including when a
        consumer stops after the first final result.
        """

        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if stop_event is not None and stop_event.is_set():
            return
        self._ensure_runtime()
        assert self.p is not None
        assert self._recognizer_factory is not None

        stream: Any | None = None
        last_partial = ""
        last_partial_energy: float | None = None
        last_frame_energy: float | None = None
        last_frame_started_at = start_time = self._clock()
        next_segment_id = 1
        active_segment_id: int | None = None
        active_segment_started_at: float | None = None
        active_segment_peak_energy: float | None = None

        def ensure_active_segment(started_at: float) -> tuple[int, float]:
            nonlocal next_segment_id, active_segment_id, active_segment_started_at
            if active_segment_id is None:
                active_segment_id = next_segment_id
                next_segment_id += 1
                active_segment_started_at = started_at
            assert active_segment_started_at is not None
            return active_segment_id, active_segment_started_at

        def reset_active_segment() -> None:
            nonlocal last_partial, last_partial_energy
            nonlocal active_segment_id, active_segment_started_at
            nonlocal active_segment_peak_energy
            last_partial = ""
            last_partial_energy = None
            active_segment_id = None
            active_segment_started_at = None
            active_segment_peak_energy = None

        def observe_segment_energy(energy: float | None) -> float | None:
            nonlocal active_segment_peak_energy
            if energy is not None:
                active_segment_peak_energy = max(
                    energy,
                    active_segment_peak_energy or energy,
                )
            return active_segment_peak_energy

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
            self._enable_word_metadata(recognizer)
            logger.info("Listening for speech")

            while not (stop_event is not None and stop_event.is_set()):
                last_frame_started_at = self._clock()
                if timeout is not None and last_frame_started_at - start_time >= timeout:
                    break
                # ``stream.read`` blocks for the duration of the frames it is
                # asked for, so checking the deadline only before the call let
                # the effective timeout overshoot by a whole chunk. Shrink the
                # final read to whatever time is actually left instead.
                frames_to_read = self.chunk
                if timeout is not None:
                    remaining = timeout - (last_frame_started_at - start_time)
                    frames_to_read = max(
                        _MINIMUM_READ_FRAMES,
                        min(self.chunk, int(remaining * self.rate)),
                    )
                data = stream.read(frames_to_read, exception_on_overflow=False)
                try:
                    last_frame_energy = pcm16_rms(data)
                except (TypeError, ValueError):
                    # Preserve compatibility with synthetic/non-PCM adapters
                    # while exposing real PCM energy to barge-in consumers.
                    last_frame_energy = None
                # Keep the peak for the current Vosk endpoint interval even
                # before the first non-empty partial. Final-only utterances
                # commonly end on silence; using only that last frame would
                # discard real short commands.
                observe_segment_energy(last_frame_energy)
                if recognizer.AcceptWaveform(data):
                    parsed_result = self._parse_recognition(recognizer.Result(), "text")
                    parsed_result = self._deduplicate_parsed(parsed_result)
                    clean_text = parsed_result.text.strip()
                    if clean_text:
                        segment_id, segment_started_at = ensure_active_segment(
                            last_frame_started_at
                        )
                        segment_peak_energy = observe_segment_energy(last_frame_energy)
                        yield RecognitionResult(
                            clean_text,
                            is_final=True,
                            frame_energy=last_frame_energy,
                            segment_id=segment_id,
                            segment_started_at=segment_started_at,
                            confidence=parsed_result.confidence,
                            speech_duration_seconds=(parsed_result.speech_duration_seconds),
                            segment_peak_energy=segment_peak_energy,
                            word_confidences=parsed_result.word_confidences,
                            word_timings=parsed_result.word_timings,
                        )
                    reset_active_segment()
                else:
                    parsed_partial = self._parse_recognition(
                        recognizer.PartialResult(),
                        "partial",
                    )
                    parsed_partial = self._deduplicate_parsed(parsed_partial)
                    clean_partial = parsed_partial.text.strip()
                    energy_advanced = (
                        clean_partial == last_partial
                        and last_frame_energy is not None
                        and last_partial_energy is not None
                        and last_frame_energy - last_partial_energy >= _PARTIAL_ENERGY_REEMIT_DELTA
                    )
                    if clean_partial and (clean_partial != last_partial or energy_advanced):
                        segment_id, segment_started_at = ensure_active_segment(
                            last_frame_started_at
                        )
                        segment_peak_energy = observe_segment_energy(last_frame_energy)
                        last_partial = clean_partial
                        last_partial_energy = last_frame_energy
                        yield RecognitionResult(
                            clean_partial,
                            is_final=False,
                            frame_energy=last_frame_energy,
                            segment_id=segment_id,
                            segment_started_at=segment_started_at,
                            energy_reemit=energy_advanced,
                            confidence=parsed_partial.confidence,
                            speech_duration_seconds=(parsed_partial.speech_duration_seconds),
                            segment_peak_energy=segment_peak_energy,
                            word_confidences=parsed_partial.word_confidences,
                            word_timings=parsed_partial.word_timings,
                        )

            final_result = getattr(recognizer, "FinalResult", None)
            if callable(final_result):
                parsed_result = self._parse_recognition(final_result(), "text")
                parsed_result = self._deduplicate_parsed(parsed_result)
                clean_text = parsed_result.text.strip()
                if clean_text:
                    segment_id, segment_started_at = ensure_active_segment(last_frame_started_at)
                    segment_peak_energy = observe_segment_energy(last_frame_energy)
                    yield RecognitionResult(
                        clean_text,
                        is_final=True,
                        frame_energy=last_frame_energy,
                        segment_id=segment_id,
                        segment_started_at=segment_started_at,
                        confidence=parsed_result.confidence,
                        speech_duration_seconds=(parsed_result.speech_duration_seconds),
                        segment_peak_energy=segment_peak_energy,
                        word_confidences=parsed_result.word_confidences,
                        word_timings=parsed_result.word_timings,
                    )
                reset_active_segment()
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
