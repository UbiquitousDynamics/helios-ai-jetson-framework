from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict

import pytest

import api.conversation_control as control
from api.conversation import ConversationSession, ConversationTurnStatus
from api.conversation_control import (
    ConversationEvent,
    ConversationEventKind as E,
    ConversationFloor,
    ConversationFloorSnapshot,
    ConversationFloorState as S,
    InvalidConversationTransition,
)


def signal(floor: ConversationFloor, *kinds: E) -> ConversationFloorSnapshot:
    result = floor.snapshot()
    for kind in kinds:
        result = floor.apply(ConversationEvent(kind))
    return result


_THINKING = (E.ACTIVATE, E.FINAL_SPEECH, E.GENERATION_STARTED)
_SPEAKING = (*_THINKING, E.PLAYBACK_STARTED)
_PATHS = {
    S.IDLE: (),
    S.ARMED: (E.ACTIVATE,),
    S.USER_SPEAKING: (E.ACTIVATE, E.PROVISIONAL_SPEECH),
    S.USER_PAUSED: (E.ACTIVATE, E.PROVISIONAL_SPEECH, E.SILENCE),
    S.FINALIZING: (E.ACTIVATE, E.FINAL_SPEECH),
    S.THINKING: _THINKING,
    S.ASSISTANT_SPEAKING: _SPEAKING,
    S.BARGE_IN_CANDIDATE: (*_SPEAKING, E.INTERRUPTION_CANDIDATE),
    S.INTERRUPTED: (*_SPEAKING, E.INTERRUPTION_CONFIRMED),
    S.SUSPENDED: (E.SUSPEND,),
}
_STATES = dict(zip("IAUPFTSBXZ", S))
# Public transition contract. Columns are IDLE, ARMED, USER_SPEAKING,
# USER_PAUSED, FINALIZING, THINKING, ASSISTANT_SPEAKING, CANDIDATE,
# INTERRUPTED, SUSPENDED; '-' is a rejected signal. No implementation table
# is imported: changing one accepted edge requires changing this contract.
_CONTRACT = {
    E.ACTIVATE: "AA--------",
    E.PROVISIONAL_SPEECH: "-UUU---BU-",
    E.FINAL_SPEECH: "-FFFF---F-",
    E.SILENCE: "IAPP-TSB--",
    E.GENERATION_STARTED: "----T-----",
    E.GENERATION_COMPLETED: "IA---TSBXZ",
    E.GENERATION_CANCELLED: "IA---XXXXZ",
    E.GENERATION_FAILED: "IA---XXXXZ",
    E.PLAYBACK_STARTED: "-S--SSSB--",
    E.PLAYBACK_COMPLETED: "IA---TTBXZ",
    E.PLAYBACK_CANCELLED: "IA---XXXXZ",
    E.PLAYBACK_FAILED: "IA---XXXXZ",
    E.RESPONSE_FINISHED: "IA---AABXZ",
    E.INTERRUPTION_CANDIDATE: "-----BBB--",
    E.INTERRUPTION_REJECTED: "-A---TSS--",
    E.INTERRUPTION_CONFIRMED: "-----XXXX-",
    E.SUSPEND: "ZZZZZZZZZZ",
    E.RESUME: "-A-------A",
    E.END_SESSION: "IIIIIIIIII",
}


def test_contract_covers_every_state_and_event() -> None:
    assert len(S) == 10
    assert set(_CONTRACT) == set(E)
    assert set(_PATHS) == set(S)
    assert all(len(row) == len(S) for row in _CONTRACT.values())


@pytest.mark.parametrize(
    "source,kind,target",
    [(state, kind, row[index]) for kind, row in _CONTRACT.items() for index, state in enumerate(S)],
)
def test_every_state_event_pair_obeys_the_contract(source: S, kind: E, target: str) -> None:
    floor = ConversationFloor()
    before = signal(floor, *_PATHS[source])
    assert before.state is source
    if target == "-":
        with pytest.raises(InvalidConversationTransition):
            signal(floor, kind)
        assert floor.snapshot() is before
    else:
        assert signal(floor, kind).state is _STATES[target]


def test_pause_resume_final_and_overlapped_response_lifecycle() -> None:
    floor = ConversationFloor()
    assert signal(floor, E.ACTIVATE, E.PROVISIONAL_SPEECH).state is S.USER_SPEAKING
    assert signal(floor, E.SILENCE).state is S.USER_PAUSED
    assert signal(floor, E.PROVISIONAL_SPEECH).state is S.USER_SPEAKING
    assert signal(floor, E.FINAL_SPEECH).state is S.FINALIZING
    assert signal(floor, E.GENERATION_STARTED, E.PLAYBACK_STARTED).state is S.ASSISTANT_SPEAKING
    speaking = floor.snapshot()
    assert signal(floor, E.GENERATION_COMPLETED) is speaking
    # Playback gaps must not implicitly end the response or require a new turn.
    assert signal(floor, E.PLAYBACK_COMPLETED).state is S.THINKING
    assert signal(floor, E.PLAYBACK_STARTED).state is S.ASSISTANT_SPEAKING
    assert signal(floor, E.PLAYBACK_COMPLETED, E.RESPONSE_FINISHED).state is S.ARMED


@pytest.mark.parametrize("start", [S.THINKING, S.ASSISTANT_SPEAKING])
def test_candidate_rejection_restores_the_underlying_floor(start: S) -> None:
    floor = ConversationFloor()
    signal(floor, *_PATHS[start], E.INTERRUPTION_CANDIDATE)
    candidate = floor.snapshot()
    assert candidate.candidate_return_state is start
    assert signal(floor, E.PROVISIONAL_SPEECH, E.SILENCE) is candidate
    rejected = signal(floor, E.INTERRUPTION_REJECTED)
    assert rejected.state is start
    assert rejected.candidate_return_state is None
    assert signal(floor, E.INTERRUPTION_REJECTED) is rejected


def test_candidate_tracks_playback_changes_and_response_completion() -> None:
    floor = ConversationFloor()
    signal(floor, *_THINKING, E.INTERRUPTION_CANDIDATE)
    assert signal(floor, E.PLAYBACK_STARTED).candidate_return_state is S.ASSISTANT_SPEAKING
    assert signal(floor, E.PLAYBACK_COMPLETED).candidate_return_state is S.THINKING
    completed = signal(floor, E.RESPONSE_FINISHED)
    assert completed.state is S.BARGE_IN_CANDIDATE
    assert completed.candidate_return_state is S.ARMED
    assert signal(floor, E.PLAYBACK_COMPLETED, E.GENERATION_COMPLETED) is completed
    with pytest.raises(InvalidConversationTransition):
        signal(floor, E.PLAYBACK_STARTED)
    assert floor.snapshot() is completed
    assert signal(floor, E.INTERRUPTION_REJECTED).state is S.ARMED


def test_confirmed_interruption_accepts_a_final_follow_up() -> None:
    floor = ConversationFloor()
    signal(floor, *_SPEAKING, E.INTERRUPTION_CANDIDATE)
    with pytest.raises(InvalidConversationTransition):
        signal(floor, E.FINAL_SPEECH)
    assert signal(floor, E.INTERRUPTION_CONFIRMED).state is S.INTERRUPTED
    assert signal(floor, E.GENERATION_CANCELLED, E.PLAYBACK_CANCELLED).state is S.INTERRUPTED
    assert signal(floor, E.FINAL_SPEECH, E.GENERATION_STARTED).state is S.THINKING


@pytest.mark.parametrize(
    "kind",
    [
        E.GENERATION_COMPLETED,
        E.GENERATION_CANCELLED,
        E.GENERATION_FAILED,
        E.PLAYBACK_COMPLETED,
        E.PLAYBACK_CANCELLED,
        E.PLAYBACK_FAILED,
        E.RESPONSE_FINISHED,
        E.INTERRUPTION_CONFIRMED,
        E.SUSPEND,
        E.END_SESSION,
    ],
)
def test_repeated_terminal_signals_are_idempotent(kind: E) -> None:
    floor = ConversationFloor()
    signal(floor, *_SPEAKING)
    terminal = signal(floor, kind)
    for _ in range(5):
        assert signal(floor, kind) is terminal


@pytest.mark.parametrize("kind", [E.END_SESSION, E.SUSPEND, E.RESPONSE_FINISHED])
def test_late_worker_terminals_do_not_reactivate_a_terminal_floor(kind: E) -> None:
    floor = ConversationFloor()
    terminal = signal(floor, *_SPEAKING, kind)
    assert (
        signal(
            floor,
            E.GENERATION_CANCELLED,
            E.PLAYBACK_FAILED,
            E.GENERATION_COMPLETED,
            E.PLAYBACK_COMPLETED,
            E.RESPONSE_FINISHED,
        )
        is terminal
    )
    if kind is not E.RESPONSE_FINISHED:
        with pytest.raises(InvalidConversationTransition):
            signal(floor, E.FINAL_SPEECH)


def test_two_instances_and_canonical_history_are_independent() -> None:
    session = ConversationSession()
    turn = session.begin_turn("synthetic-private-history")
    before = session.snapshot()
    floor, other = ConversationFloor(), ConversationFloor()
    signal(floor, *_SPEAKING, E.END_SESSION)
    assert other.snapshot() == ConversationFloorSnapshot(S.IDLE)
    assert session.snapshot() == before
    assert turn.status is ConversationTurnStatus.PENDING
    session.fail_turn(turn, interrupted=True)


@pytest.mark.parametrize("bad", [None, True, 1, "activate", "synthetic-private-input", object()])
def test_event_rejects_untyped_inputs_without_echoing_them(bad: object) -> None:
    with pytest.raises(TypeError, match="^kind must be a ConversationEventKind$"):
        ConversationEvent(bad)  # type: ignore[arg-type]
    floor = ConversationFloor()
    before = floor.snapshot()
    with pytest.raises(TypeError, match="^event must be a ConversationEvent$"):
        floor.apply(bad)  # type: ignore[arg-type]
    assert floor.snapshot() is before


@pytest.mark.parametrize("bad", [-1, True, 1.5, "0", None])
def test_snapshot_rejects_invalid_revision(bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        ConversationFloorSnapshot(S.IDLE, bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [None, "idle", 0, True])
def test_snapshot_requires_a_typed_state(bad: object) -> None:
    with pytest.raises(TypeError):
        ConversationFloorSnapshot(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("return_state", [None, "thinking", S.IDLE, S.USER_SPEAKING])
def test_snapshot_rejects_invalid_candidate_return_state(return_state: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        ConversationFloorSnapshot(S.BARGE_IN_CANDIDATE, 0, return_state)  # type: ignore[arg-type]
    if return_state is not None:
        with pytest.raises(ValueError):
            ConversationFloorSnapshot(S.ARMED, 0, return_state)  # type: ignore[arg-type]


def test_events_and_snapshots_are_immutable_content_free_and_not_logged(caplog) -> None:
    floor = ConversationFloor()
    event = ConversationEvent(E.ACTIVATE)
    snapshot = floor.apply(event)
    assert asdict(event) == {"kind": E.ACTIVATE}
    assert asdict(snapshot) == {"state": S.ARMED, "revision": 1, "candidate_return_state": None}
    for value in (event, snapshot):
        assert not hasattr(value, "__dict__")
    with pytest.raises(FrozenInstanceError):
        event.kind = E.END_SESSION  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.state = S.IDLE  # type: ignore[misc]
    signal(floor, E.END_SESSION)
    assert snapshot.state is S.ARMED
    assert not caplog.records


def test_failed_snapshot_construction_preserves_state_and_releases_lock(monkeypatch) -> None:
    floor = ConversationFloor()
    before = floor.snapshot()

    def fail(*_args):
        raise RuntimeError("injected snapshot failure")

    with monkeypatch.context() as patch:
        patch.setattr(control, "ConversationFloorSnapshot", fail)
        with pytest.raises(RuntimeError, match="injected snapshot failure"):
            signal(floor, E.ACTIVATE)
        assert floor.snapshot() is before
    assert signal(floor, E.ACTIVATE).revision == 1


def test_concurrent_terminal_signals_are_linearized_once() -> None:
    floor = ConversationFloor()
    before = signal(floor, *_SPEAKING)
    barrier = threading.Barrier(8, timeout=5)

    def cancel() -> ConversationFloorSnapshot:
        barrier.wait()
        return signal(floor, E.GENERATION_CANCELLED)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(cancel) for _ in range(8)]
        snapshots = [future.result(timeout=5) for future in futures]
    assert all(item is snapshots[0] for item in snapshots)
    assert snapshots[0].state is S.INTERRUPTED
    assert snapshots[0].revision == before.revision + 1


def test_reader_waits_for_atomic_snapshot_publication(monkeypatch) -> None:
    floor = ConversationFloor()
    constructing = threading.Event()
    release = threading.Event()
    reader_started = threading.Event()
    original_snapshot = control.ConversationFloorSnapshot

    def slow_snapshot(*args):
        constructing.set()
        assert release.wait(timeout=5)
        return original_snapshot(*args)

    def read():
        reader_started.set()
        return floor.snapshot()

    monkeypatch.setattr(control, "ConversationFloorSnapshot", slow_snapshot)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(floor.apply, ConversationEvent(E.ACTIVATE))
        try:
            assert constructing.wait(timeout=5)
            reader = executor.submit(read)
            assert reader_started.wait(timeout=5)
            assert not reader.done()
        finally:
            release.set()
        assert reader.result(timeout=5) is writer.result(timeout=5)
    assert floor.snapshot() == original_snapshot(S.ARMED, revision=1)


def test_concurrent_readers_never_see_torn_snapshots() -> None:
    floor = ConversationFloor()
    barrier = threading.Barrier(5, timeout=5)

    def write() -> None:
        barrier.wait()
        for _ in range(200):
            signal(floor, *_SPEAKING, E.INTERRUPTION_CANDIDATE, E.END_SESSION)

    def read() -> None:
        barrier.wait()
        revision = 0
        for _ in range(2000):
            current = floor.snapshot()
            assert current.revision >= revision
            revision = current.revision
            phase = revision % 6
            expected = (
                S.IDLE,
                S.ARMED,
                S.FINALIZING,
                S.THINKING,
                S.ASSISTANT_SPEAKING,
                S.BARGE_IN_CANDIDATE,
            )[phase]
            assert current.state is expected
            assert current.candidate_return_state is (S.ASSISTANT_SPEAKING if phase == 5 else None)

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(write), *(executor.submit(read) for _ in range(4))]
        for future in futures:
            future.result(timeout=5)
    assert floor.snapshot() == ConversationFloorSnapshot(S.IDLE, revision=1200)
