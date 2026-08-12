"""Model-free barge-in detection from PCM energy or recognizer events."""

from __future__ import annotations

import math
import sys
from array import array
from collections.abc import Callable
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
    segment_id: int | None
    energy_reemit: bool


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
    suppression policy. A final recognition result is sufficient on its own,
    while partial results must remain active across more than one recognition
    update. This prevents a stale Vosk hypothesis re-emitted solely because
    loudspeaker energy increased from cancelling TTS. Once either path detects
    a barge-in, further calls return false until ``reset`` is called.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        energy_threshold: float = 0.035,
        minimum_active_seconds: float = 0.12,
        minimum_partial_words: int = 3,
        recognition_event_energy: float = 0.1,
        suppression_policy: EchoSuppressionPolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be greater than zero")
        if not math.isfinite(energy_threshold) or not 0 <= energy_threshold <= 1:
            raise ValueError("energy_threshold must be finite and between 0 and 1")
        if not math.isfinite(minimum_active_seconds) or minimum_active_seconds < 0:
            raise ValueError("minimum_active_seconds must be finite and non-negative")
        if (
            isinstance(minimum_partial_words, bool)
            or not isinstance(minimum_partial_words, int)
            or minimum_partial_words < 1
        ):
            raise ValueError("minimum_partial_words must be a positive integer")
        if not math.isfinite(recognition_event_energy) or not 0 <= recognition_event_energy <= 1:
            raise ValueError("recognition_event_energy must be finite and between 0 and 1")

        self.sample_rate = sample_rate
        self.energy_threshold = energy_threshold
        self.minimum_active_seconds = minimum_active_seconds
        self.minimum_partial_words = minimum_partial_words
        self.recognition_event_energy = recognition_event_energy
        self.suppression_policy = (
            suppression_policy if suppression_policy is not None else NoEchoSuppressionPolicy()
        )
        self._clock = clock
        self._active_samples = 0
        self._detected = False
        self._recognition_segment_id: int | None = None
        self._recognition_started_at: float | None = None
        self._recognition_peak_energy: float | None = None
        self._recognition_partial_count = 0

    @property
    def detected(self) -> bool:
        """Whether a barge-in has been emitted since the last reset."""

        return self._detected

    @property
    def recognition_candidate_pending(self) -> bool:
        """Whether one non-echo partial is awaiting independent confirmation."""

        return self._recognition_partial_count > 0

    @property
    def recognition_candidate_segment_id(self) -> int | None:
        """Vosk segment associated with the pending partial, when available."""

        return self._recognition_segment_id

    def reset(self) -> None:
        """Arm the detector for a new playback interval."""

        self._active_samples = 0
        self._detected = False
        self._clear_recognition_candidate()

    def discard_recognition_candidate(self) -> None:
        """Forget an armed partial after a higher-level echo decision."""

        if not self._detected:
            self._clear_recognition_candidate()

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
        observed_at: float | None = None,
    ) -> bool:
        """Process a Vosk partial/final event and return true for a new barge-in."""

        candidate_energy = self.recognition_event_energy if frame_energy is None else frame_energy
        if not math.isfinite(candidate_energy) or not 0 <= candidate_energy <= 1:
            raise ValueError("frame_energy must be finite and between 0 and 1")
        if not math.isfinite(elapsed_since_tts_start) or elapsed_since_tts_start < 0:
            raise ValueError("elapsed_since_tts_start must be finite and non-negative")
        observation_time = (
            float(observed_at)
            if observed_at is not None
            else (self._clock() if self._clock is not None else elapsed_since_tts_start)
        )
        if not math.isfinite(observation_time) or observation_time < 0:
            raise ValueError("observed_at must be finite and non-negative")
        if self._detected or not result.text.strip():
            return False
        suppressed = self.suppression_policy.should_suppress(
            candidate_energy,
            elapsed_since_tts_start,
        )

        raw_segment_id = getattr(result, "segment_id", None)
        segment_id = raw_segment_id if isinstance(raw_segment_id, int) else None
        same_pending_segment = self._recognition_partial_count > 0 and (
            segment_id is None
            or self._recognition_segment_id is None
            or segment_id == self._recognition_segment_id
        )
        if result.is_final:
            if same_pending_segment and self._recognition_peak_energy is not None:
                # The pending partial already passed the policy in the epoch in
                # which the user spoke. A final commonly lands on silence, and
                # may also arrive just after a new TTS fragment re-enters the
                # stricter startup window. Inherit that prior acoustic
                # acceptance instead of reclassifying it with new coordinates.
                suppressed = False
            if suppressed:
                self._active_samples = 0
                self._clear_recognition_candidate()
                return False
            self._detected = True
            return True

        if suppressed:
            self._active_samples = 0
            if not same_pending_segment:
                self._clear_recognition_candidate()
            return False

        # SpeechRecognizer deliberately exposes an energy-only re-emission so
        # consumers can observe a louder PCM frame even when Vosk's text did not
        # change. It is not independent evidence of user speech and therefore
        # must never arm or confirm an interruption by itself.
        if bool(getattr(result, "energy_reemit", False)):
            return False

        if (
            self._recognition_partial_count == 0
            or (
                segment_id is not None
                and self._recognition_segment_id is not None
                and segment_id != self._recognition_segment_id
            )
        ):
            self._recognition_segment_id = segment_id
            self._recognition_started_at = observation_time
            self._recognition_peak_energy = candidate_energy
            self._recognition_partial_count = 1
            return False

        self._recognition_partial_count += 1
        self._recognition_peak_energy = max(
            candidate_energy,
            self._recognition_peak_energy or candidate_energy,
        )
        assert self._recognition_started_at is not None
        if len(result.text.split()) < self.minimum_partial_words:
            return False
        if (
            observation_time - self._recognition_started_at
            < self.minimum_active_seconds
        ):
            return False
        self._detected = True
        return True

    def _clear_recognition_candidate(self) -> None:
        self._recognition_segment_id = None
        self._recognition_started_at = None
        self._recognition_peak_energy = None
        self._recognition_partial_count = 0

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
