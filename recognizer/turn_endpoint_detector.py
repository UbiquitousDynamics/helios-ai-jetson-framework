"""Content-free endpoint decisions for one utterance, owned by one caller.

A pause or duration bound can request a recognizer flush, never promote a
partial. Only a nonempty recognizer final can produce FINALIZE. The future
capture controller must execute requests and supply ordered activity/events.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from enum import Enum
from typing import TYPE_CHECKING

from api.transcripts import TranscriptSegment

if TYPE_CHECKING:
    from recognizer.speech_recognizer import RecognitionResult


def _finite(value: float, name: str, *, minimum: float = 0) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
    ):
        raise ValueError(f"{name} must be finite and within bounds")


@dataclass(frozen=True, slots=True)
class TurnEndpointConfig:
    short_pause_seconds: float = 0.35
    finalization_seconds: float = 1.2
    inactivity_seconds: float = 10.0
    maximum_utterance_seconds: float = 30.0
    revision_stability_seconds: float = 0.35
    final_result_timeout_seconds: float = 1.0
    minimum_confidence: float = 0.6
    minimum_speech_seconds: float = 0.08

    def __post_init__(self) -> None:
        for item in fields(self):
            _finite(getattr(self, item.name), item.name)
        if not 0 < self.short_pause_seconds < self.finalization_seconds:
            raise ValueError("short pause must be positive and shorter than finalization")
        if self.inactivity_seconds < self.finalization_seconds:
            raise ValueError("inactivity must not be shorter than finalization")
        if self.maximum_utterance_seconds < self.finalization_seconds:
            raise ValueError("maximum utterance must not be shorter than finalization")
        if self.final_result_timeout_seconds <= 0:
            raise ValueError("final result timeout must be positive")
        if self.minimum_confidence > 1:
            raise ValueError("minimum confidence must not exceed one")


@dataclass(frozen=True, slots=True)
class EndpointObservation:
    """Local VAD/activity and Vosk metadata, deliberately excluding text."""

    speech_active: bool = False
    has_text: bool = False
    is_final: bool = False
    segment: TranscriptSegment | None = None
    revision: int | None = None
    energy_reemit: bool = False
    confidence: float | None = None
    speech_duration_seconds: float | None = None

    def __post_init__(self) -> None:
        for name in ("speech_active", "has_text", "is_final", "energy_reemit"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")
        if self.segment is not None and not isinstance(self.segment, TranscriptSegment):
            raise TypeError("segment must be a TranscriptSegment")
        if self.revision is not None and (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("revision must be a positive integer")
        if self.confidence is not None:
            _finite(self.confidence, "confidence")
            if self.confidence > 1:
                raise ValueError("confidence must not exceed one")
        if self.speech_duration_seconds is not None:
            _finite(self.speech_duration_seconds, "speech duration")

    @classmethod
    def from_recognition(
        cls, result: RecognitionResult, *, speech_active: bool
    ) -> EndpointObservation:
        if not isinstance(result.text, str):
            raise TypeError("recognition text must be a string")
        segment = (
            TranscriptSegment(result.capture_id, result.segment_id)
            if result.capture_id is not None and result.segment_id is not None
            else None
        )
        return cls(
            speech_active=speech_active,
            has_text=bool(result.text.strip()),
            is_final=result.is_final,
            segment=segment,
            revision=result.revision,
            energy_reemit=result.energy_reemit,
            confidence=result.confidence,
            speech_duration_seconds=result.speech_duration_seconds,
        )


class EndpointState(str, Enum):
    ARMED = "armed"
    SPEAKING = "speaking"
    PAUSED = "paused"
    FINALIZING = "finalizing"
    ENDED = "ended"


class EndpointAction(str, Enum):
    CONTINUE = "continue"
    PAUSE = "pause"
    REQUEST_FINAL_RESULT = "request_final_result"
    FINALIZE = "finalize"
    DISCARD = "discard"
    EXPIRE = "expire"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class EndpointDecision:
    action: EndpointAction
    reason: str


@dataclass(frozen=True, slots=True)
class EndpointSnapshot:
    state: EndpointState
    started_at: float | None
    last_activity_at: float
    stable_since: float | None
    final_result_deadline: float | None


class TurnEndpointDetector:
    """Pure, constant-space policy; no capture, dispatch, threads, or logging.

    Call ``update`` on silence ticks as well as recognition/activity changes.
    Each instance handles one utterance. Terminal actions are emitted once;
    create a new detector when the controller explicitly starts another turn.
    Speech resumed after a flush request belongs to the future controller's
    next capture; a requested flush is irreversible for this instance.
    """

    def __init__(
        self,
        config: TurnEndpointConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if config is not None and not isinstance(config, TurnEndpointConfig):
            raise TypeError("config must be a TurnEndpointConfig")
        self.config = config or TurnEndpointConfig()
        self._clock = clock
        now = clock()
        _finite(now, "clock")
        self._last_now = self._last_activity = now
        self._started_at: float | None = None
        self._stable_since: float | None = None
        self._deadline: float | None = None
        self._state = EndpointState.ARMED
        self._segment: TranscriptSegment | None = None
        self._revision = 0
        self._has_text = False
        self._confidence: float | None = None
        self._duration: float | None = None

    def snapshot(self) -> EndpointSnapshot:
        return EndpointSnapshot(
            self._state, self._started_at, self._last_activity, self._stable_since, self._deadline
        )

    def _end(self, action: EndpointAction, reason: str) -> EndpointDecision:
        self._state = EndpointState.ENDED
        return EndpointDecision(action, reason)

    def _flush(self, now: float, reason: str) -> EndpointDecision:
        deadline = now + self.config.final_result_timeout_seconds
        _finite(deadline, "final result deadline")
        self._state = EndpointState.FINALIZING
        self._deadline = deadline
        return EndpointDecision(EndpointAction.REQUEST_FINAL_RESULT, reason)

    def update(self, observation: EndpointObservation | None = None) -> EndpointDecision:
        if observation is not None and not isinstance(observation, EndpointObservation):
            raise TypeError("observation must be an EndpointObservation")
        observed = observation or EndpointObservation()
        now = self._clock()
        _finite(now, "clock")
        if now < self._last_now:
            raise ValueError("clock must not move backwards")
        self._last_now = now
        if self._state is EndpointState.ENDED:
            return EndpointDecision(EndpointAction.NONE, "terminal")
        if self._deadline is not None and now >= self._deadline:
            return self._end(EndpointAction.DISCARD, "final_result_timeout")

        # Stale metadata cannot finalize, extend activity, or reset stability.
        stale = (
            observed.segment is not None
            and self._segment is not None
            and (
                observed.segment < self._segment
                or (
                    observed.segment == self._segment
                    and observed.revision is not None
                    and observed.revision <= self._revision
                )
            )
        )
        if stale:
            observed = EndpointObservation()
        if observed.is_final and observed.has_text:
            return self._end(EndpointAction.FINALIZE, "recognizer_final")
        if self._state is EndpointState.FINALIZING:
            if observed.is_final:
                return self._end(EndpointAction.DISCARD, "empty_final")
            return EndpointDecision(EndpointAction.NONE, "awaiting_final_result")

        # Continuous activity cannot defeat the maximum utterance bound.
        if (
            self._started_at is not None
            and now - self._started_at >= self.config.maximum_utterance_seconds
        ):
            return self._flush(now, "maximum_utterance")
        credible_text = (
            observed.has_text
            and observed.confidence is not None
            and (
                observed.confidence >= self.config.minimum_confidence
                and observed.speech_duration_seconds is not None
                and observed.speech_duration_seconds >= self.config.minimum_speech_seconds
            )
        )
        if self._started_at is None and (observed.speech_active or credible_text):
            self._started_at = now
            self._last_activity = now
            self._state = EndpointState.SPEAKING
        if observed.speech_active:
            self._last_activity = now
            self._state = EndpointState.SPEAKING
        if observed.has_text and self._started_at is not None:
            if observed.segment is not None:
                if observed.segment != self._segment:
                    self._revision = 0
                self._segment = observed.segment
                self._revision = observed.revision or self._revision + 1
            if not observed.energy_reemit or self._stable_since is None:
                self._stable_since = now
            self._has_text = True
            self._confidence = observed.confidence
            self._duration = observed.speech_duration_seconds

        quiet = now - self._last_activity
        if quiet >= self.config.inactivity_seconds:
            if self._started_at is None:
                return self._end(EndpointAction.EXPIRE, "inactivity")
            return self._flush(now, "inactivity")
        if self._started_at is None:
            return EndpointDecision(EndpointAction.CONTINUE, "armed")
        confidence_ok = (
            self._confidence is None or self._confidence >= self.config.minimum_confidence
        )
        duration_ok = self._duration is None or self._duration >= self.config.minimum_speech_seconds
        stable = self._stable_since is not None and (
            now - self._stable_since >= self.config.revision_stability_seconds
        )
        if (
            not observed.speech_active
            and quiet >= self.config.finalization_seconds
            and self._has_text
            and stable
            and confidence_ok
            and duration_ok
        ):
            return self._flush(now, "stable_pause")
        if not observed.speech_active and quiet >= self.config.short_pause_seconds:
            self._state = EndpointState.PAUSED
            return EndpointDecision(EndpointAction.PAUSE, "short_pause")
        return EndpointDecision(EndpointAction.CONTINUE, "activity")
