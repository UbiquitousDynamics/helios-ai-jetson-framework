"""Piper text-to-speech with in-memory WAV playback."""

from __future__ import annotations

import io
import logging
import threading
import wave
from pathlib import Path
from typing import Any, Protocol

import config

logger = logging.getLogger(__name__)


class AudioBackend(Protocol):
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


class SoundDeviceBackend:
    """Persistent blocking sounddevice stream used by the production runtime."""

    _DTYPE_BY_WIDTH = {
        1: "uint8",
        2: "int16",
        4: "int32",
    }

    def __init__(self, *, sounddevice_module: Any | None = None) -> None:
        self._sounddevice = sounddevice_module
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
        self._stream = self._module().RawOutputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype=dtype,
        )
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
    ) -> None:
        self.voice_model = Path(voice_model)
        self._voice = voice
        self._audio_backend = audio_backend or SoundDeviceBackend()

    @property
    def voice(self) -> Any:
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

    def _play_wave(self, source: Any) -> None:
        try:
            with wave.open(source, "rb") as wav_file:
                sample_rate = wav_file.getframerate()
                channels = wav_file.getnchannels()
                sample_width = wav_file.getsampwidth()
                frames = wav_file.readframes(wav_file.getnframes())
            self._audio_backend.play(
                frames,
                sample_rate,
                channels,
                sample_width,
            )
        except AudioPlaybackError:
            raise
        except Exception as exc:
            raise AudioPlaybackError("Unable to play synthesized audio") from exc

    def speak(self, text: str) -> None:
        if text and text.strip() and not any(character.isalnum() for character in text):
            logger.debug("Skipping punctuation-only speech fragment")
            return
        logger.debug("Synthesizing %s character(s) of speech", len(text))
        output = self.synthesize_wave(text)
        self._play_wave(output)

    def play_audio(self, filename: str | Path) -> None:
        """Play a WAV file while retaining the legacy public method."""

        path = Path(filename)
        if not path.is_file():
            raise AudioPlaybackError(f"Audio file not found: {path}")
        self._play_wave(str(path))

    def close(self) -> None:
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
