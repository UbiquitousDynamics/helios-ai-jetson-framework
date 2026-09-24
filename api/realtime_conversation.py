"""Single floor, capture ownership and correlated response lifecycle.

No worker or queue is created here. Capture callers own endpoint updates;
response workers may publish content-free events with their immutable ID.
"""

from __future__ import annotations

from api.control_intents import SpeechOutputControl

import inspect
import math
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any

from api.conversation_control import (
    ConversationEvent,
    ConversationEventKind as E,
    ConversationFloor,
    ConversationFloorSnapshot,
    ConversationFloorState as S,
)
from api.transcripts import (
    AuthoritativeUtterance,
    ProvisionalRevision,
    TranscriptRevisionAggregator,
)
from recognizer.turn_endpoint_detector import (
    EndpointAction,
    EndpointObservation,
    TurnEndpointConfig,
    TurnEndpointDetector,
)


class RealtimeBusyError(RuntimeError):
    """An existing capture or response must finish before another can start."""


class ResponseEvent(str, Enum):
    GENERATION_COMPLETED = "generation_completed"
    SYNTHESIS_STARTED = "synthesis_started"
    SYNTHESIS_COMPLETED = "synthesis_completed"
    SYNTHESIS_FAILED = "synthesis_failed"
    PLAYBACK_STARTED = "playback_started"
    PLAYBACK_COMPLETED = "playback_completed"
    PLAYBACK_FAILED = "playback_failed"


class SpeechStopSignal:
    """Event-compatible cancellation view, without a polling worker."""

    def __init__(self, cancelled: Callable[[], bool]):
        if not callable(cancelled):
            raise TypeError("cancellation predicate must be callable")
        self._cancelled = cancelled
        self._local = threading.Event()

    def set(self) -> None:
        self._local.set()

    def is_set(self) -> bool:
        return self._local.is_set() or self._cancelled()

    def wait(self, timeout: float | None = None) -> bool:
        if timeout is not None and (
            not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
        ):
            raise ValueError("speech cancellation wait must be finite")
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = 0.01 if deadline is None else min(0.01, deadline - time.monotonic())
            if remaining <= 0:
                return self.is_set()
            self._local.wait(remaining)
        return True


def observed_speech_call(
    function: Callable[..., Any],
    text: str,
    observer: Callable[[ResponseEvent], None] | None,
    *,
    cancellation_event: Any = None,
) -> Any:
    """Use precise stages when supported; legacy backends expose one phase."""
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        parameters = {}
    kwargs = {}
    if cancellation_event is not None:
        if cancellation_event.is_set():
            return None
        if "cancellation_event" in parameters:
            kwargs["cancellation_event"] = cancellation_event
    if "on_lifecycle" in parameters:
        return function(text, on_lifecycle=observer, **kwargs)
    if observer:
        observer(ResponseEvent.PLAYBACK_STARTED)
    try:
        result = function(text, **kwargs)
    except BaseException:
        if observer:
            observer(ResponseEvent.PLAYBACK_FAILED)
        raise
    if observer:
        observer(ResponseEvent.PLAYBACK_COMPLETED)
    return result


@dataclass(frozen=True, slots=True)
class RealtimeSnapshot:
    floor: ConversationFloorSnapshot
    capture_active: bool
    response_id: int | None
    synthesizing: bool
    generation_complete: bool
    stopped: bool


class CaptureLease:
    """One caller's stop signal and endpoint policy, released after cleanup."""

    def __init__(self, owner: RealtimeConversationController, *, response: bool, stop: Any):
        self._owner = owner
        self.response = response
        self._external_stop = stop
        self._stop = threading.Event()
        self.decoder_reset = threading.Event()
        self.reset_supported = False
        self.on_soft_stop: Callable[[], bool] | None = None
        self.finished = threading.Event()
        self.endpoint_ended = False
        self._endpoint_active = False
        self._endpoint: TurnEndpointDetector | None = None

    def reset_endpoint(self) -> None:
        self.endpoint_ended = self._endpoint_active = False
        self._endpoint = None

    def is_set(self) -> bool:
        if self._stop.is_set():
            return True
        if self._external_stop is None or not self._external_stop.is_set():
            return False
        return not (self.on_soft_stop is not None and self.on_soft_stop())

    def force_stop(self) -> None:
        self._stop.set()

    def on_frame(self, result: Any, energy: float | None) -> EndpointAction:
        if self._stop.is_set():
            return EndpointAction.DISCARD
        # Response-time silence must not retire capture before the response.
        if self.response and result is None and not self._endpoint_active:
            return EndpointAction.CONTINUE
        if self._endpoint is None:
            self._endpoint = TurnEndpointDetector(self._owner.endpointing, clock=self._owner.clock)
        active = energy is not None and energy >= self._owner.activity_energy
        observed = (
            EndpointObservation.from_recognition(result, speech_active=active)
            if result is not None
            else EndpointObservation(speech_active=active)
        )
        self._endpoint_active |= observed.has_text or active
        decision = self._endpoint.update(observed)
        if decision.action is EndpointAction.REQUEST_FINAL_RESULT:
            self.endpoint_ended = True
        if not self.response:
            self._owner.endpoint_event(self, decision.action)
        if decision.action is EndpointAction.FINALIZE:
            self._endpoint = None
            self._endpoint_active = False
        elif decision.action in {EndpointAction.DISCARD, EndpointAction.EXPIRE}:
            self.endpoint_ended = True
        return decision.action


class RealtimeConversationController:
    def __init__(
        self,
        *,
        endpointing: TurnEndpointConfig,
        activity_energy: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(endpointing, TurnEndpointConfig):
            raise TypeError("endpointing must be a TurnEndpointConfig")
        if (
            isinstance(activity_energy, bool)
            or not isinstance(activity_energy, (int, float))
            or not math.isfinite(activity_energy)
            or not 0 <= activity_energy <= 1
        ):
            raise ValueError("activity energy must be finite and between zero and one")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.endpointing, self.activity_energy, self.clock = endpointing, activity_energy, clock
        self.transcripts = TranscriptRevisionAggregator()
        self.speech_output = SpeechOutputControl()
        self._floor = ConversationFloor()
        self._lock = threading.RLock()
        self._capture: CaptureLease | None = None
        self._response: int | None = None
        self._sequence = 0
        self._synthesizing = self._generation_complete = self._stopped = False

    def _apply(self, kind: E) -> None:
        self._floor.apply(ConversationEvent(kind))

    def snapshot(self) -> RealtimeSnapshot:
        with self._lock:
            return RealtimeSnapshot(
                self._floor.snapshot(),
                self._capture is not None,
                self._response,
                self._synthesizing,
                self._generation_complete,
                self._stopped,
            )

    def arm(self) -> None:
        with self._lock:
            if (
                self._stopped
                or self._response is not None
                or self._floor.snapshot().state is S.SUSPENDED
            ):
                return
            self._apply(E.END_SESSION)
            self._apply(E.ACTIVATE)

    def idle(self) -> None:
        with self._lock:
            if self._response is None and self._floor.snapshot().state is not S.SUSPENDED:
                self._apply(E.END_SESSION)

    def suspend_session(self) -> None:
        with self._lock:
            self._apply(E.SUSPEND)

    def resume_session(self) -> None:
        with self._lock:
            if self._floor.snapshot().state is S.SUSPENDED:
                self._apply(E.RESUME)

    def end_session(self) -> None:
        with self._lock:
            self.transcripts.clear_pending()
            self._apply(E.END_SESSION)

    def observe(self, result: Any):
        with self._lock:
            if self._stopped:
                return None
            value = self.transcripts.observe(
                result.text,
                is_final=result.is_final,
                capture_id=result.capture_id,
                segment_id=result.segment_id,
                revision=result.revision,
            )
            if self._response is None and self._floor.snapshot().state not in {
                S.BARGE_IN_CANDIDATE,
                S.SUSPENDED,
            }:
                if isinstance(value, (ProvisionalRevision, AuthoritativeUtterance)):
                    if self._floor.snapshot().state is S.IDLE:
                        self._apply(E.ACTIVATE)
                    self._apply(
                        E.FINAL_SPEECH
                        if isinstance(value, AuthoritativeUtterance)
                        else E.PROVISIONAL_SPEECH
                    )
            return value

    def endpoint_event(self, lease: CaptureLease, action: EndpointAction) -> None:
        with self._lock:
            if (
                self._stopped
                or self._capture is not lease
                or self._response is not None
                or self._floor.snapshot().state is S.SUSPENDED
            ):
                return
            if action is EndpointAction.PAUSE:
                self._apply(E.SILENCE)

    @contextmanager
    def capture(self, *, response: bool = False, stop: Any = None):
        with self._lock:
            if self._stopped:
                raise RealtimeBusyError("realtime controller is stopped")
            if self._capture is not None:
                raise RealtimeBusyError("microphone capture already has an owner")
            if not response and self._response is not None:
                raise RealtimeBusyError("primary capture cannot start during a response")
            lease = CaptureLease(self, response=response, stop=stop)
            self._capture = lease
            if not response:
                self.arm()
        try:
            yield lease
        finally:
            with self._lock:
                self.transcripts.clear_pending()
                if self._capture is lease:
                    self._capture = None
                lease.finished.set()

    def begin_response(self) -> int:
        with self._lock:
            if self._stopped or self._response is not None:
                raise RealtimeBusyError("response cannot start while stopped or busy")
            if self._floor.snapshot().state is S.SUSPENDED:
                raise RealtimeBusyError("session is suspended")
            if self._capture is not None and not self._capture.response:
                raise RealtimeBusyError("primary capture must release before dispatch")
            if self._floor.snapshot().state is not S.FINALIZING:
                self.arm()
                self._apply(E.FINAL_SPEECH)
            self._sequence += 1
            self._response = self._sequence
            self.speech_output.begin_response()
            self._synthesizing = self._generation_complete = False
            self._apply(E.GENERATION_STARTED)
            return self._response

    def observer(self, response_id: int) -> Callable[[ResponseEvent], None]:
        return lambda event: self.response_event(response_id, event)

    def response_event(self, response_id: int, event: ResponseEvent) -> None:
        if not isinstance(event, ResponseEvent):
            raise TypeError("response event must be typed")
        with self._lock:
            if self._stopped or response_id != self._response:
                return
            if self._floor.snapshot().state in {S.INTERRUPTED, S.SUSPENDED, S.IDLE}:
                return
            if event is ResponseEvent.SYNTHESIS_STARTED:
                self._synthesizing = True
            elif event is ResponseEvent.SYNTHESIS_COMPLETED:
                self._synthesizing = False
            elif event is ResponseEvent.GENERATION_COMPLETED:
                self._generation_complete = True
                self._apply(E.GENERATION_COMPLETED)
            elif event is ResponseEvent.SYNTHESIS_FAILED:
                self._synthesizing = False
                self._apply(E.PLAYBACK_FAILED)
            else:
                self._apply(E(event.value))

    def finish_response(self, response_id: int, *, failed: bool = False) -> None:
        with self._lock:
            if response_id != self._response:
                return
            self._response = None
            self._synthesizing = False
            if not self._stopped:
                if failed and self._floor.snapshot().state is not S.INTERRUPTED:
                    self._apply(E.GENERATION_FAILED)
                self._apply(E.RESPONSE_FINISHED)
                if self._floor.snapshot().state is S.INTERRUPTED:
                    self.arm()

    def candidate(self, *, rejected: bool = False) -> None:
        with self._lock:
            state = self._floor.snapshot().state
            if rejected and state is S.BARGE_IN_CANDIDATE:
                self._apply(E.INTERRUPTION_REJECTED)
            elif (
                not rejected
                and self._response is not None
                and state
                in {
                    S.THINKING,
                    S.ASSISTANT_SPEAKING,
                    S.BARGE_IN_CANDIDATE,
                }
            ):
                self._apply(E.INTERRUPTION_CANDIDATE)

    def interruption(self) -> None:
        with self._lock:
            if self._stopped:
                return
            state = self._floor.snapshot().state
            if state in {S.THINKING, S.ASSISTANT_SPEAKING, S.BARGE_IN_CANDIDATE}:
                self._apply(E.INTERRUPTION_CONFIRMED)
            self._response = None
            self._synthesizing = False

    def final_speech(self) -> None:
        with self._lock:
            if self._stopped or self._response is not None:
                return
            if self._floor.snapshot().state is S.IDLE:
                self._apply(E.ACTIVATE)
            self._apply(E.FINAL_SPEECH)

    def stop(self) -> CaptureLease | None:
        with self._lock:
            self._stopped = True
            self._response = None
            self._synthesizing = False
            self.transcripts.clear_pending()
            self._apply(E.END_SESSION)
            if self._capture is not None:
                self._capture.force_stop()
            return self._capture

    def restart(self) -> None:
        with self._lock:
            if self._capture is not None or self._response is not None:
                raise RealtimeBusyError("cannot restart active realtime work")
            self._stopped = False
