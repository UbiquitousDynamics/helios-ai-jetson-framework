"""Vosk speech-recognition boundary with deterministic audio cleanup."""

from __future__ import annotations

import json
import logging
import math
import os
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import config
from recognizer.barge_in_detector import pcm16_rms

from recognizer.turn_endpoint_detector import EndpointAction

logger = logging.getLogger(__name__)

_PARTIAL_ENERGY_REEMIT_DELTA = 0.02
# Floor for deadline-clamped microphone reads (10 ms at 16 kHz). Prevents the
# capture loop from degenerating into single-frame reads near the deadline.
_MINIMUM_READ_FRAMES = 160


def downmix_stereo_pcm16(data: bytes, mode: str) -> bytes:
    """Convert interleaved stereo PCM to mono without retaining source frames."""

    if mode not in {"average", "sum", "stronger"}:
        raise ValueError("invalid stereo downmix mode")
    if len(data) % 4:
        raise ValueError("stereo PCM16 requires whole frames")
    samples = struct.unpack(f"<{len(data) // 2}h", data)
    if mode == "stronger":
        left = sum(samples[index] ** 2 for index in range(0, len(samples), 2))
        right = sum(samples[index] ** 2 for index in range(1, len(samples), 2))
        channel = 0 if left >= right else 1
        mono = samples[channel::2]
    else:
        divisor = 2 if mode == "average" else 1
        mono = tuple(
            max(-32768, min(32767, int((samples[index] + samples[index + 1]) / divisor)))
            for index in range(0, len(samples), 2)
        )
    return struct.pack(f"<{len(mono)}h", *mono)


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
    capture_id: int | None = None
    revision: int | None = None


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
        input_device: int | str | None = None,
        rate: int = 16_000,
        # 100 ms at 16 kHz. The previous 4,000-sample (250 ms) buffer set the
        # floor for barge-in reaction time and for the RMS averaging window
        # feeding echo suppression: an interruption could not be noticed sooner
        # than the frame carrying it. Vosk accepts smaller buffers unchanged and
        # per-sample cost is identical, so only loop overhead grows.
        chunk: int = 1_600,
        clock: Callable[[], float] = time.monotonic,
        owns_audio: bool | None = None,
        input_device_strict: bool = False,
        pulse_sources: Callable[[], tuple[str, ...]] | None = None,
        channel_mode: str = "mono",
        sanity_rms_threshold: float = 0.0,
        sanity_window_seconds: float = 1.0,
        capture_stall_seconds: float = 5.0,
    ) -> None:
        if rate <= 0 or chunk <= 0:
            raise ValueError("rate and chunk must be greater than zero")
        if isinstance(input_device, bool) or (
            input_device is not None and not isinstance(input_device, (int, str))
        ):
            raise ValueError("input_device must be an integer index, a name, or None")
        if isinstance(input_device, int) and input_device < 0:
            raise ValueError("input_device index must be non-negative")
        if isinstance(input_device, str):
            input_device = input_device.strip()
            if not input_device:
                raise ValueError("input_device name cannot be empty")
        if not isinstance(input_device_strict, bool):
            raise ValueError("input_device_strict must be a boolean")
        if channel_mode not in {"mono", "average", "sum", "stronger"}:
            raise ValueError("invalid input channel mode")
        if (
            isinstance(sanity_rms_threshold, bool)
            or not isinstance(sanity_rms_threshold, (int, float))
            or not math.isfinite(sanity_rms_threshold)
            or sanity_rms_threshold < 0
            or isinstance(sanity_window_seconds, bool)
            or not isinstance(sanity_window_seconds, (int, float))
            or not math.isfinite(sanity_window_seconds)
            or sanity_window_seconds <= 0
        ):
            raise ValueError("invalid capture level sanity configuration")
        if (
            isinstance(capture_stall_seconds, bool)
            or not isinstance(capture_stall_seconds, (int, float))
            or not math.isfinite(capture_stall_seconds)
            or capture_stall_seconds <= 0
        ):
            raise ValueError("capture_stall_seconds must be positive")

        self.model_path = Path(model_path)
        self.model = model
        self.p = audio_interface
        self._recognizer_factory = recognizer_factory
        self._audio_format = audio_format
        self.input_device = input_device
        self.input_device_strict = input_device_strict
        self._pulse_sources = pulse_sources or self._system_pulse_sources
        self._selected_pulse_source: str | None = None
        self._pulse_source_checked = False
        self._prior_pulse_source: str | None = None
        self.channel_mode = channel_mode
        self.sanity_rms_threshold = float(sanity_rms_threshold)
        self.sanity_window_seconds = float(sanity_window_seconds)
        self.capture_stall_seconds = float(capture_stall_seconds)
        self.rate = rate
        self.chunk = chunk
        self._clock = clock
        self._owns_audio = audio_interface is None if owns_audio is None else owns_audio
        self._closed = False
        self._runtime_lock = threading.RLock()
        self._prepare_lock = threading.Lock()
        self._prepare_thread: threading.Thread | None = None
        self._capture_number = 0

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
        """Legacy explicit helper; the live recognizer preserves all wording."""

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

        self._prepare_pulse_source()
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

    def _resolve_input_device_index(self) -> int | None:
        """Resolve a configured microphone name without silently falling back.

        PyAudio accepts only an index. A deployment can therefore use either a
        stable index or a readable device name; name matching requires exactly
        one capture-capable device so an unplugged USB microphone cannot be
        mistaken for the system default.
        """

        configured = self.input_device
        if configured is None:
            return None
        assert self.p is not None
        get_count = getattr(self.p, "get_device_count", None)
        get_info = getattr(self.p, "get_device_info_by_index", None)
        if not callable(get_count) or not callable(get_info):
            if self.input_device_strict:
                raise SpeechRecognitionError(
                    "Audio backend cannot resolve the configured microphone"
                )
            logger.warning(
                "event=capture_device_fallback requested=%s available=unknown", configured
            )
            return None
        try:
            device_count = int(get_count())
            devices: list[tuple[int, str]] = []
            all_names: list[str] = []
            for index in range(device_count):
                info = get_info(index)
                if not isinstance(info, dict):
                    continue
                name = str(info.get("name", "")).strip()
                if name:
                    all_names.append(name)
                channels = info.get("maxInputChannels", 0)
                if not name or not isinstance(channels, (int, float)) or channels < 1:
                    continue
                devices.append((index, name))
        except Exception as exc:
            raise SpeechRecognitionError("Unable to inspect configured microphone devices") from exc
        if isinstance(configured, int):
            matches = [(index, name) for index, name in devices if index == configured]
        else:
            assert isinstance(configured, str)
            if configured.startswith("pulse:"):
                if self._selected_pulse_source is None:
                    matches = []
                else:
                    matches = [
                        (index, name) for index, name in devices if name.casefold() == "pulse"
                    ]
            else:
                target = configured.casefold()
                exact = [(index, name) for index, name in devices if name.casefold() == target]
                matches = exact or [
                    (index, name) for index, name in devices if target in name.casefold()
                ]
        if len(matches) != 1:
            logger.warning(
                "event=capture_device_fallback requested=%s available=%s",
                configured,
                tuple(all_names),
            )
            if self.input_device_strict:
                raise SpeechRecognitionError(
                    "Configured microphone device is unavailable or ambiguous"
                )
            return None
        index, name = matches[0]
        logger.info("Using configured microphone device index=%s name=%s", index, name)
        return index

    @staticmethod
    def _system_pulse_sources() -> tuple[str, ...]:
        try:
            result = subprocess.run(
                ["pactl", "list", "short", "sources"],
                capture_output=True,
                text=True,
                check=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        return tuple(
            parts[1] for line in result.stdout.splitlines() if len(parts := line.split()) >= 2
        )

    def _prepare_pulse_source(self) -> None:
        if self._pulse_source_checked:
            return
        configured = self.input_device
        if not isinstance(configured, str) or not configured.startswith("pulse:"):
            self._pulse_source_checked = True
            return
        source = configured[6:]
        available = self._pulse_sources()
        if not source or source not in available:
            logger.warning(
                "event=capture_pulse_source_unavailable requested=%s available=%s",
                source,
                available,
            )
            if self.input_device_strict:
                raise SpeechRecognitionError("Configured PulseAudio source is unavailable")
            self._pulse_source_checked = True
            return
        self._selected_pulse_source = source
        self._prior_pulse_source = os.environ.get("PULSE_SOURCE")
        os.environ["PULSE_SOURCE"] = source
        self._pulse_source_checked = True
        logger.info("event=capture_pulse_source_selected source=%s", source)

    @staticmethod
    def _pulse_identity(requested: str | None) -> tuple[str | None, str | None]:
        """Read PulseAudio routing metadata without changing host audio state."""

        source = requested
        try:
            if source is None:
                info = subprocess.run(
                    ["pactl", "info"], capture_output=True, text=True, check=True, timeout=2
                ).stdout
                source = next(
                    (
                        line.split(":", 1)[1].strip()
                        for line in info.splitlines()
                        if line.startswith("Default Source:")
                    ),
                    None,
                )
            listing = subprocess.run(
                ["pactl", "list", "sources"], capture_output=True, text=True, check=True, timeout=2
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return source, None
        current_name: str | None = None
        for line in listing.splitlines():
            stripped = line.strip()
            if stripped.startswith("Source #"):
                current_name = None
            elif stripped.startswith("Name:"):
                current_name = stripped.split(":", 1)[1].strip()
            elif current_name == source and stripped.startswith("Active Port:"):
                return source, stripped.split(":", 1)[1].strip()
        return source, None

    def _log_capture_identity(self, selected_index: int | None) -> None:
        assert self.p is not None
        index = selected_index
        info: dict[str, Any] = {}
        try:
            if index is None:
                getter = getattr(self.p, "get_default_input_device_info", None)
                if callable(getter):
                    info = getter()
                    index = info.get("index")
            elif callable(getter := getattr(self.p, "get_device_info_by_index", None)):
                info = getter(index)
        except Exception:
            logger.warning("event=capture_device_identity_unavailable")
        name = str(info.get("name", "unknown"))
        source, port = (None, None)
        if self._owns_audio and name.casefold() in {"pulse", "default"}:
            source, port = self._pulse_identity(self._selected_pulse_source)
        logger.info(
            "event=capture_device_resolved requested=%s index=%s name=%s "
            "input_channels=%s device_rate=%s capture_channels=%s capture_rate=%s "
            "downmix=%s pulse_source=%s active_port=%s",
            self.input_device if self.input_device is not None else "default",
            index if index is not None else "default",
            name,
            info.get("maxInputChannels", "unknown"),
            info.get("defaultSampleRate", "unknown"),
            1 if self.channel_mode == "mono" else 2,
            self.rate,
            self.channel_mode,
            source or "unknown",
            port or "unknown",
        )

    def listen_events(
        self,
        timeout: float | None = None,
        *,
        stop_event: threading.Event | None = None,
        on_frame: Callable[[RecognitionResult | None, float | None], EndpointAction] | None = None,
        keep_open: bool = False,
        reset_event: threading.Event | None = None,
        on_segment_reset: Callable[[], None] | None = None,
    ) -> Iterator[RecognitionResult]:
        """Yield distinct partial and final recognition events.

        When ``stop_event`` is set, capture ends after the current microphone
        read and Vosk's pending text is flushed through ``FinalResult``. This
        lets a coordinating thread stop one continuous recognition session
        without repeatedly closing and reopening the input stream.

        The microphone stream is always stopped and closed, including when a
        consumer stops after the first final result.
        """

        if not isinstance(keep_open, bool):
            raise TypeError("keep_open must be a boolean")
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if stop_event is not None and stop_event.is_set():
            return
        self._ensure_runtime()
        assert self.p is not None
        assert self._recognizer_factory is not None

        with self._runtime_lock:
            self._capture_number += 1
            capture_id = self._capture_number
        stream: Any | None = None
        last_partial = ""
        last_partial_energy: float | None = None
        last_frame_energy: float | None = None
        last_frame_started_at = start_time = self._clock()
        next_segment_id = 1
        active_segment_id: int | None = None
        active_segment_started_at: float | None = None
        active_segment_peak_energy: float | None = None
        active_revision = 0
        sanity_started_at = start_time
        sanity_peak = 0.0
        sanity_checked = False
        stall_stop = threading.Event()
        last_arrival = [time.monotonic()]
        stall_monitor: threading.Thread | None = None

        def next_revision() -> int:
            nonlocal active_revision
            active_revision += 1
            return active_revision

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
            nonlocal active_segment_peak_energy, active_revision
            last_partial = ""
            last_partial_energy = None
            active_segment_id = None
            active_segment_started_at = None
            active_segment_peak_energy = None
            active_revision = 0

        def observe_segment_energy(energy: float | None) -> float | None:
            nonlocal active_segment_peak_energy
            if energy is not None:
                active_segment_peak_energy = max(
                    energy,
                    active_segment_peak_energy or energy,
                )
            return active_segment_peak_energy

        def flush_pending() -> RecognitionResult | None:
            final_result = getattr(recognizer, "FinalResult", None)
            if not callable(final_result):
                return None
            parsed = self._parse_recognition(final_result(), "text")
            final_event = None
            if parsed.text.strip():
                segment_id, segment_started_at = ensure_active_segment(last_frame_started_at)
                final_event = RecognitionResult(
                    parsed.text.strip(),
                    is_final=True,
                    frame_energy=last_frame_energy,
                    segment_id=segment_id,
                    segment_started_at=segment_started_at,
                    confidence=parsed.confidence,
                    speech_duration_seconds=parsed.speech_duration_seconds,
                    segment_peak_energy=observe_segment_energy(last_frame_energy),
                    word_confidences=parsed.word_confidences,
                    word_timings=parsed.word_timings,
                    capture_id=capture_id,
                    revision=next_revision(),
                )
            action = (
                on_frame(final_event or RecognitionResult("", is_final=True), last_frame_energy)
                if on_frame
                else None
            )
            reset_active_segment()
            return (
                final_event
                if action not in {EndpointAction.DISCARD, EndpointAction.EXPIRE}
                else None
            )

        def reset_decoder() -> None:
            nonlocal recognizer
            reset = getattr(recognizer, "Reset", None)
            if callable(reset):
                reset()
            else:
                recognizer = self._recognizer_factory(self.model, self.rate)
                self._enable_word_metadata(recognizer)
            reset_active_segment()
            if on_segment_reset:
                on_segment_reset()

        try:
            open_arguments: dict[str, Any] = {
                "format": self._audio_format,
                "channels": 1 if self.channel_mode == "mono" else 2,
                "rate": self.rate,
                "input": True,
                "frames_per_buffer": self.chunk,
            }
            input_device_index = self._resolve_input_device_index()
            if input_device_index is not None:
                open_arguments["input_device_index"] = input_device_index
            stream = self.p.open(**open_arguments)
            stream.start_stream()
            last_arrival[0] = time.monotonic()

            def watch_stall() -> None:
                while not stall_stop.wait(self.capture_stall_seconds):
                    elapsed = time.monotonic() - last_arrival[0]
                    if elapsed >= self.capture_stall_seconds:
                        logger.warning(
                            "event=capture_stall_detected elapsed_ms=%s capture_id=%s",
                            round(elapsed * 1_000),
                            capture_id,
                        )
                        return

            stall_monitor = threading.Thread(
                target=watch_stall, name="helios-capture-stall", daemon=True
            )
            stall_monitor.start()
            self._log_capture_identity(input_device_index)
            recognizer = self._recognizer_factory(self.model, self.rate)
            self._enable_word_metadata(recognizer)
            logger.info(
                "Listening for speech (input_device_index=%s)",
                input_device_index if input_device_index is not None else "default",
            )

            while not (stop_event is not None and stop_event.is_set()):
                if reset_event is not None and reset_event.is_set():
                    reset_event.clear()
                    reset_decoder()
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
                last_arrival[0] = time.monotonic()
                if self.channel_mode != "mono":
                    data = downmix_stereo_pcm16(data, self.channel_mode)
                try:
                    last_frame_energy = pcm16_rms(data)
                except (TypeError, ValueError):
                    # Preserve compatibility with synthetic/non-PCM adapters
                    # while exposing real PCM energy to barge-in consumers.
                    last_frame_energy = None
                if not sanity_checked and last_frame_energy is not None:
                    sanity_peak = max(sanity_peak, last_frame_energy)
                    if self._clock() - sanity_started_at >= self.sanity_window_seconds:
                        sanity_checked = True
                        if self.sanity_rms_threshold and sanity_peak < self.sanity_rms_threshold:
                            logger.warning(
                                "event=capture_level_low peak_rms=%0.6f threshold_rms=%0.6f",
                                sanity_peak,
                                self.sanity_rms_threshold,
                            )
                # Keep the peak for the current Vosk endpoint interval even
                # before the first non-empty partial. Final-only utterances
                # commonly end on silence; using only that last frame would
                # discard real short commands.
                observe_segment_energy(last_frame_energy)
                frame_result: RecognitionResult | None = None
                if recognizer.AcceptWaveform(data):
                    parsed_result = self._parse_recognition(recognizer.Result(), "text")
                    clean_text = parsed_result.text.strip()
                    if clean_text:
                        segment_id, segment_started_at = ensure_active_segment(
                            last_frame_started_at
                        )
                        segment_peak_energy = observe_segment_energy(last_frame_energy)
                        frame_result = RecognitionResult(
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
                            capture_id=capture_id,
                            revision=next_revision(),
                        )
                    reset_active_segment()
                else:
                    parsed_partial = self._parse_recognition(
                        recognizer.PartialResult(),
                        "partial",
                    )
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
                        frame_result = RecognitionResult(
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
                            capture_id=capture_id,
                            revision=next_revision(),
                        )

                action = on_frame(frame_result, last_frame_energy) if on_frame else None
                if frame_result is not None and action not in {
                    EndpointAction.DISCARD,
                    EndpointAction.EXPIRE,
                }:
                    yield frame_result
                if action is EndpointAction.REQUEST_FINAL_RESULT:
                    if not keep_open:
                        break
                    final_event = flush_pending()
                    if final_event is not None:
                        yield final_event
                    reset_decoder()
                elif action in {EndpointAction.DISCARD, EndpointAction.EXPIRE}:
                    if not keep_open:
                        return
                    reset_decoder()

            final_event = flush_pending()
            if final_event is not None:
                yield final_event
        except SpeechRecognitionError:
            raise
        except Exception as exc:
            raise SpeechRecognitionError("Speech recognition failed") from exc
        finally:
            stall_stop.set()
            if stall_monitor is not None:
                stall_monitor.join(timeout=0.2)
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    logger.warning("Unable to stop microphone stream", exc_info=True)
                try:
                    stream.close()
                except Exception:
                    logger.warning("Unable to close microphone stream", exc_info=True)

    def listen_once(
        self,
        timeout: float | None = None,
        *,
        on_provisional: Callable[[RecognitionResult], object] | None = None,
        stop_event: threading.Event | None = None,
        on_frame: Callable[[RecognitionResult | None, float | None], EndpointAction] | None = None,
    ) -> RecognitionResult | None:
        """Return the first final, observing optional local revisions before it.

        The observer receives partials only, on the capture owner's thread.
        Timeout still returns a partial when Vosk has no authoritative final.
        """

        latest: RecognitionResult | None = None
        kwargs: dict[str, Any] = {"timeout": timeout}
        if stop_event is not None:
            kwargs["stop_event"] = stop_event
        if on_frame is not None:
            kwargs["on_frame"] = on_frame
        events = self.listen_events(**kwargs)
        try:
            for result in events:
                latest = result
                if result.is_final:
                    return result
                if on_provisional is not None:
                    on_provisional(result)
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
            if self._selected_pulse_source is not None:
                if self._prior_pulse_source is None:
                    os.environ.pop("PULSE_SOURCE", None)
                else:
                    os.environ["PULSE_SOURCE"] = self._prior_pulse_source
            self._closed = True

    def __enter__(self) -> SpeechRecognizer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
