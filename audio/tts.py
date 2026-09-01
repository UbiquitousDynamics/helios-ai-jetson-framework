"""Piper text-to-speech with in-memory WAV playback."""

from __future__ import annotations

import io
import inspect
import logging
import threading
import time
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import config

logger = logging.getLogger(__name__)


class AudioBackend(Protocol):
    """Minimum playback contract.

    Backends may additionally provide ``play_interruptibly``. Keeping that
    method optional preserves compatibility with simple injected backends.
    """

    def play(
        self,
        frames: bytes,
        sample_rate: int,
        channels: int,
        sample_width: int,
    ) -> None: ...


class TTSError(RuntimeError):
    """Base class for TTS failures."""


class AudioSynthesisError(TTSError):
    """Raised when Piper cannot synthesize speech."""


class AudioPlaybackError(TTSError):
    """Raised when synthesized audio cannot be played."""


@dataclass(frozen=True, slots=True)
class SynthesizedFragment:
    """Audio rendered ahead of playback, plus the cost of rendering it.

    Splitting synthesis from playback lets a caller render the next fragment
    while the current one is still audible. ``synthesis_ms`` is carried here so
    the eventual :class:`SpeechTiming` stays comparable with the synchronous
    path.
    """

    text: str
    wave_bytes: bytes
    synthesis_ms: float


@dataclass(frozen=True, slots=True)
class SpeechTiming:
    """Content-free timing returned by the production TTS implementation."""

    synthesis_ms: float
    playback_ms: float
    audio_duration_ms: float
    audio_started_at: float


class SoundDeviceBackend:
    """Persistent blocking sounddevice stream used by the production runtime.

    ``device`` is deliberately optional: a desktop can keep using its default
    output, while an installed assistant can pin playback to the intended ALSA
    or PulseAudio device instead of inheriting a conflicting user default.
    """

    _PLAYBACK_CHUNK_MS = 100

    _DTYPE_BY_WIDTH = {
        1: "uint8",
        2: "int16",
        4: "int32",
    }

    def __init__(
        self,
        *,
        device: int | str | None = None,
        latency: str = "high",
        sounddevice_module: Any | None = None,
    ) -> None:
        if isinstance(device, bool) or (device is not None and not isinstance(device, (int, str))):
            raise ValueError("device must be an integer index, a name, or None")
        if isinstance(device, int) and device < 0:
            raise ValueError("device index must be non-negative")
        if isinstance(device, str):
            device = device.strip()
            if not device:
                raise ValueError("device name cannot be empty")
        normalized_latency = latency.strip().lower()
        if normalized_latency not in {"low", "high"}:
            raise ValueError("latency must be 'low' or 'high'")
        self._sounddevice = sounddevice_module
        self._device = device
        self._latency = normalized_latency
        self._stream: Any | None = None
        self._stream_format: tuple[int, int, str] | None = None
        self._lock = threading.RLock()

    def _module(self) -> Any:
        if self._sounddevice is None:
            try:
                import sounddevice as sd
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise AudioPlaybackError("sounddevice is required for TTS playback") from exc
            self._sounddevice = sd
        return self._sounddevice

    def _close_stream(self) -> None:
        stream = self._stream
        self._stream = None
        self._stream_format = None
        if stream is not None:
            stream.close()

    def _get_stream(
        self,
        sample_rate: int,
        channels: int,
        dtype: str,
    ) -> Any:
        stream_format = (sample_rate, channels, dtype)
        if self._stream is not None and self._stream_format == stream_format:
            return self._stream
        self._close_stream()
        arguments: dict[str, Any] = {
            "samplerate": sample_rate,
            "channels": channels,
            "dtype": dtype,
            # Jetson's ALSA/Pulse bridge has occasionally under-run with the
            # backend's implicit low-latency buffer. The conservative default
            # trades a little response latency for stable spoken output.
            "latency": self._latency,
        }
        if self._device is not None:
            arguments["device"] = self._device
        self._stream = self._module().RawOutputStream(**arguments)
        self._stream_format = stream_format
        return self._stream

    def play(
        self,
        frames: bytes,
        sample_rate: int,
        channels: int,
        sample_width: int,
    ) -> None:
        try:
            dtype = self._DTYPE_BY_WIDTH[sample_width]
        except KeyError:
            raise AudioPlaybackError(
                f"Unsupported PCM sample width: {sample_width} byte(s)"
            ) from None

        with self._lock:
            try:
                stream = self._get_stream(sample_rate, channels, dtype)
                stream.start()
                underflowed = stream.write(frames)
                stream.stop()
                if underflowed:
                    logger.warning("Audio output underflow while playing TTS")
            except AudioPlaybackError:
                raise
            except Exception as exc:
                try:
                    self._close_stream()
                except Exception:
                    pass
                raise AudioPlaybackError("Unable to play synthesized audio") from exc

    def play_interruptibly(
        self,
        frames: bytes,
        sample_rate: int,
        channels: int,
        sample_width: int,
        interrupt_event: threading.Event,
        *,
        pause_event: threading.Event | None = None,
    ) -> int:
        """Play PCM in bounded chunks and return the number of frames written.

        ``pause_event`` is a response-scoped TTS duck. Unlike interruption it
        preserves the current frame offset, allowing playback to resume when a
        provisional STT candidate is rejected. An interrupt always wins and
        wakes a paused playback within one short polling interval.
        """

        try:
            dtype = self._DTYPE_BY_WIDTH[sample_width]
        except KeyError:
            raise AudioPlaybackError(
                f"Unsupported PCM sample width: {sample_width} byte(s)"
            ) from None

        bytes_per_frame = channels * sample_width
        chunk_frame_count = max(1, sample_rate * self._PLAYBACK_CHUNK_MS // 1_000)
        chunk_size = chunk_frame_count * bytes_per_frame
        frames_written = 0

        with self._lock:
            stream = None
            started = False
            try:
                stream = self._get_stream(sample_rate, channels, dtype)
                if interrupt_event.is_set():
                    return 0
                for offset in range(0, len(frames), chunk_size):
                    if interrupt_event.is_set():
                        break
                    if pause_event is not None and pause_event.is_set():
                        if started:
                            stream.stop()
                            started = False
                        while pause_event.is_set() and not interrupt_event.wait(0.02):
                            pass
                        if interrupt_event.is_set():
                            break
                    if not started:
                        stream.start()
                        started = True
                    chunk = frames[offset : offset + chunk_size]
                    underflowed = stream.write(chunk)
                    frames_written += len(chunk) // bytes_per_frame
                    if underflowed:
                        logger.warning("Audio output underflow while playing TTS")
                if started:
                    stream.stop()
                    started = False
                return frames_written
            except AudioPlaybackError:
                raise
            except Exception as exc:
                if started and stream is not None:
                    try:
                        stream.stop()
                    except Exception:
                        pass
                try:
                    self._close_stream()
                except Exception:
                    pass
                raise AudioPlaybackError("Unable to play synthesized audio") from exc

    def close(self) -> None:
        with self._lock:
            try:
                self._close_stream()
            except Exception as exc:
                raise AudioPlaybackError("Unable to close the audio output") from exc


class PiperTTS:
    """Synthesize speech with Piper and play PCM frames without a temp file."""

    def __init__(
        self,
        voice_model: str | Path = config.TTS_MODEL,
        *,
        voice: Any | None = None,
        audio_backend: AudioBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.voice_model = Path(voice_model)
        self._voice = voice
        self._audio_backend = audio_backend or SoundDeviceBackend()
        self._clock = clock
        self._close_lock = threading.Lock()
        self._speech_lock = threading.RLock()
        self._synthesis_lock = threading.RLock()
        self._playback_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._preloaded_waves: dict[str, bytes] = {}
        self._active_speech_interrupt: threading.Event | None = None
        self._active_interrupt: threading.Event | None = None
        self._active_playback_started_at: float | None = None
        self._active_playback_text: str | None = None
        self._playback_pause = threading.Event()
        self._last_playback_started_at: float | None = None
        self._last_playback_ended_at: float | None = None
        self._last_playback_text: str | None = None
        self._last_playback_frame_count = 0
        self._last_playback_total_frames = 0
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise TTSError("Piper TTS is closed")

    @property
    def last_playback_frame_count(self) -> int:
        """Number of PCM frames submitted by the most recent playback."""

        with self._state_lock:
            return self._last_playback_frame_count

    @property
    def last_playback_total_frames(self) -> int:
        """Total PCM frames available to the most recent playback."""

        with self._state_lock:
            return self._last_playback_total_frames

    @property
    def last_playback_was_interrupted(self) -> bool:
        """Whether the most recent playback ended before its full buffer."""

        with self._state_lock:
            return self._last_playback_frame_count < self._last_playback_total_frames

    @property
    def is_speaking(self) -> bool:
        """Whether an audio buffer is currently being played."""

        with self._state_lock:
            return self._active_interrupt is not None

    @property
    def active_playback_started_at(self) -> float | None:
        """Monotonic start time of the active buffer, if any."""

        with self._state_lock:
            return self._active_playback_started_at

    @property
    def active_playback_text(self) -> str | None:
        """Text represented by the active TTS buffer, if known."""

        with self._state_lock:
            return self._active_playback_text

    @property
    def last_playback_text(self) -> str | None:
        """Text represented by the most recently started TTS buffer, if known."""

        with self._state_lock:
            return self._last_playback_text

    @property
    def last_playback_window(self) -> tuple[float, float | None] | None:
        """Most recent monotonic playback start/end times."""

        with self._state_lock:
            started_at = self._last_playback_started_at
            ended_at = self._last_playback_ended_at
        if started_at is None:
            return None
        return started_at, ended_at

    def interrupt(self) -> bool:
        """Cancel current synthesis/playback, returning whether speech was active."""

        with self._state_lock:
            events = {
                event
                for event in (self._active_speech_interrupt, self._active_interrupt)
                if event is not None
            }
            if not events:
                # A future/current playback may be waiting in the response-
                # scoped duck even when no backend buffer is active yet.
                self._playback_pause.clear()
                return False
            for event in events:
                event.set()
            self._playback_pause.clear()
            return True

    def duck(self) -> bool:
        """Pause response playback without cancelling synthesis or model work.

        This is intentionally reversible: a provisional barge-in partial may
        mute the loudspeaker so Vosk can finish the user's utterance, while a
        rejected candidate can resume the exact PCM buffer from its prior
        frame offset.
        """

        with self._state_lock:
            self._ensure_open()
            self._playback_pause.set()
        return True

    def resume(self) -> bool:
        """Resume playback paused by :meth:`duck`."""

        was_paused = self._playback_pause.is_set()
        self._playback_pause.clear()
        return was_paused

    @property
    def voice(self) -> Any:
        with self._synthesis_lock:
            self._ensure_open()
            if self._voice is None:
                try:
                    from piper.voice import PiperVoice
                except ModuleNotFoundError as exc:  # pragma: no cover - deployment dependency
                    if exc.name == "piper":
                        raise AudioSynthesisError(
                            "The 'piper-tts' package is required for speech synthesis"
                        ) from exc
                    raise AudioSynthesisError(
                        f"Unable to import Piper because dependency {exc.name!r} is missing: {exc}"
                    ) from exc
                except ImportError as exc:  # pragma: no cover - deployment dependency
                    raise AudioSynthesisError(
                        f"Unable to import Piper or one of its native dependencies: {exc}"
                    ) from exc
                try:
                    self._voice = PiperVoice.load(str(self.voice_model))
                except Exception as exc:
                    raise AudioSynthesisError(
                        f"Unable to load Piper voice model: {self.voice_model}"
                    ) from exc
                logger.info("Loaded Piper voice model %s", self.voice_model)
            return self._voice

    def synthesize_wave(self, text: str) -> io.BytesIO:
        if not text or not text.strip():
            raise ValueError("text cannot be empty")

        self._ensure_open()
        with self._synthesis_lock:
            self._ensure_open()
            return self._synthesize_wave_unlocked(text)

    def _synthesize_wave_unlocked(self, text: str) -> io.BytesIO:
        """Synthesize one WAV while the Piper voice is exclusively owned."""

        output = io.BytesIO()
        try:
            voice = self.voice
            wav_file = wave.open(output, "wb")
            try:
                # Piper 1.3 changed ``synthesize`` into a lazy AudioChunk
                # iterator and added ``synthesize_wav`` for Wave_write
                # destinations.  Piper 1.2 only exposes the original
                # ``synthesize(text, wav_file)`` API.
                synthesize_wav = getattr(voice, "synthesize_wav", None)
                if callable(synthesize_wav):
                    synthesize_wav(text, wav_file)
                else:
                    voice.synthesize(text, wav_file)
            except BaseException:
                # Wave_write.close() raises its own "channels not specified"
                # error when synthesis failed before writing a header. Preserve
                # the original Piper/import failure instead.
                try:
                    wav_file.close()
                except Exception:
                    pass
                raise
            else:
                wav_file.close()
        except TTSError:
            raise
        except Exception as exc:
            raise AudioSynthesisError("Piper failed to synthesize speech") from exc
        output.seek(0)
        return output

    def preload_phrases(
        self,
        phrases: Iterable[str],
        *,
        stop_event: threading.Event | None = None,
    ) -> tuple[str, ...]:
        """Synthesize phrases until complete or cooperative cancellation is requested.

        Native Piper synthesis cannot be interrupted safely while it is in
        progress.  The stop event therefore bounds cancellation at phrase
        boundaries and prevents an in-flight result from being cached after a
        shutdown request.
        """

        loaded: list[str] = []
        for value in phrases:
            if stop_event is not None and stop_event.is_set():
                break
            phrase = str(value).strip()
            if not phrase:
                raise ValueError("preloaded phrases cannot be empty")
            with self._cache_lock:
                if phrase in self._preloaded_waves:
                    loaded.append(phrase)
                    continue
            wave_bytes = self.synthesize_wave(phrase).getvalue()
            if stop_event is not None and stop_event.is_set():
                break
            with self._close_lock:
                self._ensure_open()
                with self._cache_lock:
                    self._preloaded_waves.setdefault(phrase, wave_bytes)
            loaded.append(phrase)
        return tuple(loaded)

    def has_preloaded_phrase(self, phrase: str) -> bool:
        """Return whether a phrase can be played without running Piper."""

        with self._cache_lock:
            return phrase in self._preloaded_waves

    def speak_preloaded(
        self,
        phrase: str,
        *,
        cancellation: threading.Event | None = None,
    ) -> bool:
        """Play a cached phrase, returning false instead of synthesizing on demand."""

        self._ensure_open()
        with self._cache_lock:
            wave_bytes = self._preloaded_waves.get(phrase)
        if wave_bytes is None or (cancellation is not None and cancellation.is_set()):
            return False
        self._play_wave(
            io.BytesIO(wave_bytes),
            interrupt_event=cancellation,
            playback_text=phrase,
        )
        return True

    def _play_wave(
        self,
        source: Any,
        *,
        interrupt_event: threading.Event | None = None,
        playback_text: str | None = None,
    ) -> tuple[float, float, float]:
        try:
            self._ensure_open()
            with wave.open(source, "rb") as wav_file:
                sample_rate = wav_file.getframerate()
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                frame_count = wav_file.getnframes()
                frames = wav_file.readframes(frame_count)
            audio_duration_ms = frame_count / sample_rate * 1_000
            with self._playback_lock:
                self._ensure_open()
                playback_interrupt = interrupt_event or threading.Event()
                audio_started_at = self._clock()
                with self._state_lock:
                    self._active_interrupt = playback_interrupt
                    self._active_playback_started_at = audio_started_at
                    self._active_playback_text = playback_text
                    self._last_playback_started_at = audio_started_at
                    self._last_playback_ended_at = None
                    self._last_playback_text = playback_text
                    self._last_playback_frame_count = 0
                    self._last_playback_total_frames = frame_count
                try:
                    play_interruptibly = getattr(
                        self._audio_backend,
                        "play_interruptibly",
                        None,
                    )
                    if callable(play_interruptibly):
                        try:
                            signature = inspect.signature(play_interruptibly)
                            supports_pause = "pause_event" in signature.parameters or any(
                                parameter.kind is inspect.Parameter.VAR_KEYWORD
                                for parameter in signature.parameters.values()
                            )
                        except (TypeError, ValueError):
                            supports_pause = False
                        if supports_pause:
                            frames_written = play_interruptibly(
                                frames,
                                sample_rate,
                                channels,
                                sample_width,
                                playback_interrupt,
                                pause_event=self._playback_pause,
                            )
                        else:
                            # Compatibility for injected/legacy backends. They
                            # cannot pause an already active write, but a duck
                            # established before playback still blocks safely.
                            while self._playback_pause.is_set():
                                if playback_interrupt.wait(0.02):
                                    break
                            frames_written = (
                                0
                                if playback_interrupt.is_set()
                                else play_interruptibly(
                                    frames,
                                    sample_rate,
                                    channels,
                                    sample_width,
                                    playback_interrupt,
                                )
                            )
                        if frames_written is None:
                            frames_written = frame_count
                    elif playback_interrupt.is_set():
                        frames_written = 0
                    else:
                        self._audio_backend.play(
                            frames,
                            sample_rate,
                            channels,
                            sample_width,
                        )
                        frames_written = frame_count
                finally:
                    playback_ended_at = self._clock()
                    with self._state_lock:
                        if self._active_interrupt is playback_interrupt:
                            self._active_interrupt = None
                            self._active_playback_started_at = None
                            self._active_playback_text = None
                            self._last_playback_ended_at = playback_ended_at
                with self._state_lock:
                    self._last_playback_frame_count = min(
                        max(0, int(frames_written)),
                        frame_count,
                    )
                playback_ms = (playback_ended_at - audio_started_at) * 1_000
            return playback_ms, audio_duration_ms, audio_started_at
        except AudioPlaybackError:
            raise
        except Exception as exc:
            raise AudioPlaybackError("Unable to play synthesized audio") from exc

    def synthesize_fragment(self, text: str) -> SynthesizedFragment | None:
        """Render one fragment to audio without playing it.

        Stage one of the two-stage speech path. This intentionally does not take
        ``_speech_lock``: holding a single lock across synthesis and playback
        (as :meth:`speak_with_timing` must, to keep its synchronous contract)
        forced the next fragment to wait for the current one to finish playing,
        which is audible as a gap between sentences.
        """

        if text and text.strip() and not any(character.isalnum() for character in text):
            logger.debug("Skipping punctuation-only speech fragment")
            return None
        self._ensure_open()
        started_at = self._clock()
        wave_bytes = self.synthesize_wave(text).getvalue()
        return SynthesizedFragment(
            text=text,
            wave_bytes=wave_bytes,
            synthesis_ms=(self._clock() - started_at) * 1_000,
        )

    def play_fragment(self, fragment: SynthesizedFragment) -> SpeechTiming:
        """Play audio produced by :meth:`synthesize_fragment`.

        Stage two of the two-stage speech path. ``_speech_lock`` is held only
        for playback, which is what actually has to be serialized to keep
        fragments in order.
        """

        with self._speech_lock:
            self._ensure_open()
            speech_interrupt = threading.Event()
            with self._state_lock:
                self._active_speech_interrupt = speech_interrupt
            try:
                playback_ms, audio_duration_ms, audio_started_at = self._play_wave(
                    io.BytesIO(fragment.wave_bytes),
                    interrupt_event=speech_interrupt,
                    playback_text=fragment.text,
                )
                return SpeechTiming(
                    synthesis_ms=fragment.synthesis_ms,
                    playback_ms=playback_ms,
                    audio_duration_ms=audio_duration_ms,
                    audio_started_at=audio_started_at,
                )
            finally:
                with self._state_lock:
                    if self._active_speech_interrupt is speech_interrupt:
                        self._active_speech_interrupt = None

    def speak_with_timing(self, text: str) -> SpeechTiming | None:
        """Speak text and return content-free synthesis/playback timing."""

        if text and text.strip() and not any(character.isalnum() for character in text):
            logger.debug("Skipping punctuation-only speech fragment")
            return
        with self._speech_lock:
            self._ensure_open()
            speech_interrupt = threading.Event()
            with self._state_lock:
                self._active_speech_interrupt = speech_interrupt
            try:
                logger.debug("Synthesizing %s character(s) of speech", len(text))
                synthesis_started_at = self._clock()
                output = self.synthesize_wave(text)
                synthesis_ms = (self._clock() - synthesis_started_at) * 1_000
                playback_ms, audio_duration_ms, audio_started_at = self._play_wave(
                    output,
                    interrupt_event=speech_interrupt,
                    playback_text=text,
                )
                return SpeechTiming(
                    synthesis_ms=synthesis_ms,
                    playback_ms=playback_ms,
                    audio_duration_ms=audio_duration_ms,
                    audio_started_at=audio_started_at,
                )
            finally:
                with self._state_lock:
                    if self._active_speech_interrupt is speech_interrupt:
                        self._active_speech_interrupt = None

    def speak(self, text: str) -> None:
        """Speak text while retaining the historical ``None`` return value."""

        self.speak_with_timing(text)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self.interrupt()
        # Wait for every operation that could synthesize, populate the cache,
        # or touch the backend before terminal teardown.
        with self._speech_lock, self._synthesis_lock, self._playback_lock:
            with self._cache_lock:
                self._preloaded_waves.clear()
            close = getattr(self._audio_backend, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> PiperTTS:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# Compatibility alias: despite the historical name, this implementation uses
# Piper and never imports pyttsx3.
Pyttsx3TTS = PiperTTS
