"""Model-free barge-in detection from PCM energy or recognizer events."""

from __future__ import annotations

import math
import sys
from array import array
from typing import Protocol

from recognizer.echo_suppression_policy import (
    EchoSuppressionPolicy,
    NoEchoSuppressionPolicy,
)

_PCM16_SCALE = 32_768.0


class RecognitionEvent(Protocol):
    """Structural subset shared by partial and final recognition events."""

    text: str
    is_final: bool


def pcm16_rms(frame: bytes) -> float:
    """Return normalized RMS energy for little-endian signed 16-bit mono PCM."""

    if len(frame) % 2:
        raise ValueError("PCM frame must contain complete 16-bit samples")
    if not frame:
        return 0.0

    samples = array("h")
    samples.frombytes(frame)
    if sys.byteorder != "little":  # pragma: no cover - Jetson and CI are little-endian
        samples.byteswap()
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return math.sqrt(mean_square) / _PCM16_SCALE


class BargeInDetector:
    """One-shot detector for speech candidates observed while TTS is playing.

    ``process_frame`` requires sustained PCM energy before consulting the
    suppression policy. ``process_recognition`` treats any non-empty Vosk
    partial or final result as a speech candidate. Once either path detects a
    barge-in, further calls return false until ``reset`` is called.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        energy_threshold: float = 0.035,
        minimum_active_seconds: float = 0.12,
        recognition_event_energy: float = 0.1,
        suppression_policy: EchoSuppressionPolicy | None = None,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be greater than zero")
        if not math.isfinite(energy_threshold) or not 0 <= energy_threshold <= 1:
            raise ValueError("energy_threshold must be finite and between 0 and 1")
        if not math.isfinite(minimum_active_seconds) or minimum_active_seconds < 0:
            raise ValueError("minimum_active_seconds must be finite and non-negative")
        if not math.isfinite(recognition_event_energy) or not 0 <= recognition_event_energy <= 1:
            raise ValueError("recognition_event_energy must be finite and between 0 and 1")

        self.sample_rate = sample_rate
        self.energy_threshold = energy_threshold
        self.minimum_active_seconds = minimum_active_seconds
        self.recognition_event_energy = recognition_event_energy
        self.suppression_policy = (
            suppression_policy if suppression_policy is not None else NoEchoSuppressionPolicy()
        )
        self._active_samples = 0
        self._detected = False

    @property
    def detected(self) -> bool:
        """Whether a barge-in has been emitted since the last reset."""

        return self._detected

    def reset(self) -> None:
        """Arm the detector for a new playback interval."""

        self._active_samples = 0
        self._detected = False

    def process_frame(
        self,
        frame: bytes,
        *,
        elapsed_since_tts_start: float,
    ) -> bool:
        """Process one PCM frame and return true only for a new barge-in."""

        energy = pcm16_rms(frame)
        if self._detected:
            return False
        if energy < self.energy_threshold:
            self._active_samples = 0
            return False

        self._active_samples += len(frame) // 2
        active_seconds = self._active_samples / self.sample_rate
        if active_seconds < self.minimum_active_seconds:
            return False
        return self._accept_candidate(energy, elapsed_since_tts_start)

    def process_recognition(
        self,
        result: RecognitionEvent,
        *,
        elapsed_since_tts_start: float,
        frame_energy: float | None = None,
    ) -> bool:
        """Process a Vosk partial/final event and return true for a new barge-in."""

        candidate_energy = self.recognition_event_energy if frame_energy is None else frame_energy
        if not math.isfinite(candidate_energy) or not 0 <= candidate_energy <= 1:
            raise ValueError("frame_energy must be finite and between 0 and 1")
        if self._detected or not result.text.strip():
            return False
        return self._accept_candidate(candidate_energy, elapsed_since_tts_start)

    def _accept_candidate(
        self,
        frame_energy: float,
        elapsed_since_tts_start: float,
    ) -> bool:
        if not math.isfinite(elapsed_since_tts_start) or elapsed_since_tts_start < 0:
            raise ValueError("elapsed_since_tts_start must be finite and non-negative")
        if self.suppression_policy.should_suppress(
            frame_energy,
            elapsed_since_tts_start,
        ):
            self._active_samples = 0
            return False
        self._detected = True
        return True
