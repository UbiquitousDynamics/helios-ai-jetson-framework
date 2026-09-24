"""Content-free conversation control types, independent of runtime orchestration.

This model does not capture audio, dispatch requests, or cancel work. A future
controller must order and correlate signals from its owned workers before
applying them. In particular, a generation completion is not a response
completion: queued audio may still need to play.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum


class ConversationFloorState(str, Enum):
    IDLE = "idle"
    ARMED = "armed"
    USER_SPEAKING = "user_speaking"
    USER_PAUSED = "user_paused"
    FINALIZING = "finalizing"
    THINKING = "thinking"
    ASSISTANT_SPEAKING = "assistant_speaking"
    BARGE_IN_CANDIDATE = "barge_in_candidate"
    INTERRUPTED = "interrupted"
    SUSPENDED = "suspended"


class ConversationEventKind(str, Enum):
    ACTIVATE = "activate"
    PROVISIONAL_SPEECH = "provisional_speech"
    FINAL_SPEECH = "final_speech"
    SILENCE = "silence"
    GENERATION_STARTED = "generation_started"
    GENERATION_COMPLETED = "generation_completed"
    GENERATION_CANCELLED = "generation_cancelled"
    GENERATION_FAILED = "generation_failed"
    PLAYBACK_STARTED = "playback_started"
    PLAYBACK_COMPLETED = "playback_completed"
    PLAYBACK_CANCELLED = "playback_cancelled"
    PLAYBACK_FAILED = "playback_failed"
    RESPONSE_FINISHED = "response_finished"
    INTERRUPTION_CANDIDATE = "interruption_candidate"
    INTERRUPTION_REJECTED = "interruption_rejected"
    INTERRUPTION_CONFIRMED = "interruption_confirmed"
    SUSPEND = "suspend"
    RESUME = "resume"
    END_SESSION = "end_session"


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    """A control signal, deliberately containing no speech or provider payload.

    Speech events describe provisional/final observations only. They confer no
    authority to dispatch a transcript; that boundary belongs to Task 02.
    """

    kind: ConversationEventKind

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ConversationEventKind):
            raise TypeError("kind must be a ConversationEventKind")


@dataclass(frozen=True, slots=True)
class ConversationFloorSnapshot:
    """Immutable state metadata; revision counts changes, not input events."""

    state: ConversationFloorState
    revision: int = 0
    candidate_return_state: ConversationFloorState | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ConversationFloorState):
            raise TypeError("state must be a ConversationFloorState")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise TypeError("revision must be an integer")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if self.state is ConversationFloorState.BARGE_IN_CANDIDATE:
            if not isinstance(self.candidate_return_state, ConversationFloorState):
                raise TypeError("a candidate requires a typed return state")
            if self.candidate_return_state not in {
                ConversationFloorState.ARMED,
                ConversationFloorState.THINKING,
                ConversationFloorState.ASSISTANT_SPEAKING,
            }:
                raise ValueError("invalid candidate return state")
        elif self.candidate_return_state is not None:
            raise ValueError("only a candidate may have a return state")


class InvalidConversationTransition(ValueError):
    """The signal is not legal in the current conversation floor state."""


# This is a vocabulary/transition contract, not an endpoint or intent policy.
# Only explicit interruption signals create/confirm a response-time candidate.
_S = ConversationFloorState
_E = ConversationEventKind
_RESPONSE_STATES = (_S.THINKING, _S.ASSISTANT_SPEAKING, _S.BARGE_IN_CANDIDATE)
_TRANSITIONS = {
    _E.ACTIVATE: {_S.IDLE: _S.ARMED, _S.ARMED: _S.ARMED},
    _E.PROVISIONAL_SPEECH: {
        _S.ARMED: _S.USER_SPEAKING,
        _S.USER_SPEAKING: _S.USER_SPEAKING,
        _S.USER_PAUSED: _S.USER_SPEAKING,
        _S.INTERRUPTED: _S.USER_SPEAKING,
        _S.BARGE_IN_CANDIDATE: _S.BARGE_IN_CANDIDATE,
    },
    _E.FINAL_SPEECH: {
        state: _S.FINALIZING
        for state in (_S.ARMED, _S.USER_SPEAKING, _S.USER_PAUSED, _S.INTERRUPTED, _S.FINALIZING)
    },
    _E.SILENCE: {
        _S.IDLE: _S.IDLE,
        _S.ARMED: _S.ARMED,
        _S.USER_SPEAKING: _S.USER_PAUSED,
        _S.USER_PAUSED: _S.USER_PAUSED,
        **{state: state for state in _RESPONSE_STATES},
    },
    _E.GENERATION_STARTED: {_S.FINALIZING: _S.THINKING},
    _E.GENERATION_COMPLETED: {
        state: state
        for state in (*_RESPONSE_STATES, _S.IDLE, _S.ARMED, _S.INTERRUPTED, _S.SUSPENDED)
    },
    _E.PLAYBACK_STARTED: {
        state: _S.ASSISTANT_SPEAKING
        for state in (_S.ARMED, _S.FINALIZING, _S.THINKING, _S.ASSISTANT_SPEAKING)
    },
    _E.PLAYBACK_COMPLETED: {
        _S.ASSISTANT_SPEAKING: _S.THINKING,
        _S.THINKING: _S.THINKING,
        **{state: state for state in (_S.IDLE, _S.ARMED, _S.INTERRUPTED, _S.SUSPENDED)},
    },
    _E.RESPONSE_FINISHED: {
        **{state: _S.ARMED for state in (_S.ARMED, _S.THINKING, _S.ASSISTANT_SPEAKING)},
        **{state: state for state in (_S.IDLE, _S.INTERRUPTED, _S.SUSPENDED)},
    },
    _E.INTERRUPTION_CANDIDATE: {state: _S.BARGE_IN_CANDIDATE for state in _RESPONSE_STATES},
    _E.INTERRUPTION_REJECTED: {
        state: state for state in (_S.ARMED, _S.THINKING, _S.ASSISTANT_SPEAKING)
    },
    _E.INTERRUPTION_CONFIRMED: {
        state: _S.INTERRUPTED for state in (*_RESPONSE_STATES, _S.INTERRUPTED)
    },
    _E.SUSPEND: {state: _S.SUSPENDED for state in _S},
    _E.RESUME: {_S.SUSPENDED: _S.ARMED, _S.ARMED: _S.ARMED},
    _E.END_SESSION: {state: _S.IDLE for state in _S},
}
for _kind in (
    _E.GENERATION_CANCELLED,
    _E.GENERATION_FAILED,
    _E.PLAYBACK_CANCELLED,
    _E.PLAYBACK_FAILED,
):
    _TRANSITIONS[_kind] = {
        **{state: _S.INTERRUPTED for state in (*_RESPONSE_STATES, _S.INTERRUPTED)},
        **{state: state for state in (_S.IDLE, _S.ARMED, _S.SUSPENDED)},
    }


class ConversationFloor:
    """Small, thread-safe state model with no callbacks, workers, or I/O.

    Callers supply already-decided control signals. The model does not measure
    silence or choose barge-in thresholds. Repeated signals that leave state
    unchanged return the same snapshot. It keeps no event queue or history.

    Worker/turn correlation, and rejection of stale signals across turns, are
    deliberately left to the future runtime controller (Task 05).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = ConversationFloorSnapshot(_S.IDLE)

    def snapshot(self) -> ConversationFloorSnapshot:
        with self._lock:
            return self._snapshot

    def apply(self, event: ConversationEvent) -> ConversationFloorSnapshot:
        if type(event) is not ConversationEvent:
            raise TypeError("event must be a ConversationEvent")
        with self._lock:
            current = self._snapshot
            state = current.state
            return_state = current.candidate_return_state
            kind = event.kind
            if state is _S.BARGE_IN_CANDIDATE and kind in {
                _E.INTERRUPTION_REJECTED,
                _E.PLAYBACK_STARTED,
                _E.PLAYBACK_COMPLETED,
                _E.RESPONSE_FINISHED,
            }:
                if kind is _E.INTERRUPTION_REJECTED:
                    assert return_state is not None
                    target, return_state = return_state, None
                else:
                    # A candidate may outlive a playback gap or response EOF.
                    # Rejection must restore the latest underlying floor.
                    target = state
                    if return_state is _S.ARMED and kind is _E.PLAYBACK_STARTED:
                        raise InvalidConversationTransition(
                            "playback cannot start after the candidate's response finished"
                        )
                    return_state = {
                        _E.PLAYBACK_STARTED: _S.ASSISTANT_SPEAKING,
                        _E.PLAYBACK_COMPLETED: (
                            _S.ARMED if return_state is _S.ARMED else _S.THINKING
                        ),
                        _E.RESPONSE_FINISHED: _S.ARMED,
                    }[kind]
            else:
                target = _TRANSITIONS.get(kind, {}).get(state)
                if target is None:
                    raise InvalidConversationTransition(
                        f"{kind.name} is not allowed in {state.name}"
                    )
                if target is _S.BARGE_IN_CANDIDATE and state is not target:
                    return_state = state
                elif target is not _S.BARGE_IN_CANDIDATE:
                    return_state = None
            if target is state and return_state is current.candidate_return_state:
                return current
            updated = ConversationFloorSnapshot(target, current.revision + 1, return_state)
            self._snapshot = updated
            return updated


__all__ = [
    "ConversationEvent",
    "ConversationEventKind",
    "ConversationFloor",
    "ConversationFloorSnapshot",
    "ConversationFloorState",
    "InvalidConversationTransition",
]
