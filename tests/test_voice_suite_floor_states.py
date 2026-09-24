"""Task 01 local floor contract; typed signals are not acoustic observations.

The JSON is a separately declared oracle, including all three candidate return
states. No private production transition table is read. Thread timeouts below
are test deadlock watchdogs, not measured or calibrated performance thresholds.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import FrozenInstanceError, asdict, dataclass, fields
from pathlib import Path
from typing import TypeVar

import pytest

import api.conversation_control as control
from api.conversation_control import (
    ConversationEvent,
    ConversationEventKind as E,
    ConversationFloor,
    ConversationFloorSnapshot,
    ConversationFloorState as S,
    InvalidConversationTransition,
)

pytestmark = pytest.mark.voice_local

SPEC_PATH = Path(__file__).parent / "voice_suite" / "task01-transitions.json"
SPEC = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
CONTEXTS = SPEC["contexts"]
MATRIX = SPEC["matrix"]
CASES = [
    pytest.param(source, kind, id=f"{source}--{kind}")
    for source in CONTEXTS
    for kind in SPEC["event_kinds"]
]
LEGAL_CASES = [
    pytest.param(source, kind, id=f"{source}--{kind}")
    for source in CONTEXTS
    for kind, target in MATRIX[source].items()
    if target is not None
]
ILLEGAL_CASES = [
    pytest.param(source, kind, id=f"{source}--{kind}")
    for source in CONTEXTS
    for kind, target in MATRIX[source].items()
    if target is None
]
WATCHDOG_SECONDS = json.loads(
    (SPEC_PATH.parent / "task01-profile.json").read_text(encoding="utf-8")
)["local_test_bounds"]["thread_join_seconds"]
T = TypeVar("T")


def _metadata(snapshot: ConversationFloorSnapshot) -> tuple[str, str | None]:
    return snapshot.state.value, (
        snapshot.candidate_return_state.value if snapshot.candidate_return_state else None
    )


def _expected_metadata(context_id: str) -> tuple[str, str | None]:
    context = CONTEXTS[context_id]
    return context["state"], context["candidate_return_state"]


def _at(context_id: str) -> ConversationFloor:
    floor = ConversationFloor()
    for kind in CONTEXTS[context_id]["event_prefix"]:
        floor.apply(ConversationEvent(E(kind)))
    snapshot = floor.snapshot()
    assert _metadata(snapshot) == _expected_metadata(context_id)
    assert snapshot.revision == CONTEXTS[context_id]["expected_revision"]
    return floor


def _assert_transition(floor: ConversationFloor, source: str, kind: str) -> str:
    before = floor.snapshot()
    assert _metadata(before) == _expected_metadata(source)
    target = MATRIX[source][kind]
    event = ConversationEvent(E(kind))
    if target is None:
        with pytest.raises(InvalidConversationTransition):
            floor.apply(event)
        assert floor.snapshot() is before
        return source

    after = floor.apply(event)
    assert floor.snapshot() is after
    assert _metadata(after) == _expected_metadata(target)
    changed = _expected_metadata(source) != _expected_metadata(target)
    assert after.revision == before.revision + int(changed)
    assert (after is before) is (not changed)
    return target


def _worker(action: Callable[[], T]) -> tuple[threading.Thread, Future[T]]:
    """Daemon workers allow a failed lock test to terminate with a test failure."""
    result: Future[T] = Future()

    def run() -> None:
        try:
            result.set_result(action())
        except BaseException as error:
            result.set_exception(error)

    thread = threading.Thread(target=run, name="voice-task01-local", daemon=True)
    thread.start()
    return thread, result


def _results(workers: list[tuple[threading.Thread, Future[T]]]) -> list[T]:
    try:
        return [result.result(timeout=WATCHDOG_SECONDS) for _, result in workers]
    finally:
        for thread, _ in workers:
            thread.join(timeout=WATCHDOG_SECONDS)
        assert all(not thread.is_alive() for thread, _ in workers), "local worker leaked"


def test_declared_contract_covers_each_reachable_context_and_event() -> None:
    assert SPEC["schema_version"] == 1
    assert SPEC["contract_version"] == "1.0.0"
    assert SPEC["test_task"] == "01"
    assert len(SPEC["event_kinds"]) == len(set(SPEC["event_kinds"])) == 19
    assert set(SPEC["event_kinds"]) == {kind.value for kind in E}
    assert set(context["state"] for context in CONTEXTS.values()) == {state.value for state in S}
    assert set(MATRIX) == set(CONTEXTS)
    assert len(CONTEXTS) == 12
    assert len(CASES) == 228 and len(LEGAL_CASES) == 136 and len(ILLEGAL_CASES) == 92
    assert {
        context["candidate_return_state"]
        for context in CONTEXTS.values()
        if context["state"] == "barge_in_candidate"
    } == {"armed", "thinking", "assistant_speaking"}
    assert len({_expected_metadata(context_id) for context_id in CONTEXTS}) == 12
    for context_id, context in CONTEXTS.items():
        assert set(context) == {
            "state",
            "candidate_return_state",
            "event_prefix",
            "expected_revision",
        }
        assert set(MATRIX[context_id]) == set(SPEC["event_kinds"])
        assert all(target is None or target in CONTEXTS for target in MATRIX[context_id].values())
        assert all(kind in SPEC["event_kinds"] for kind in context["event_prefix"])
        assert type(context["expected_revision"]) is int
        assert context["expected_revision"] == len(context["event_prefix"])


@pytest.mark.parametrize("context_id", CONTEXTS)
def test_public_event_prefix_reaches_exact_snapshot(context_id: str) -> None:
    _at(context_id)


@pytest.mark.parametrize("source,kind", CASES)
def test_every_context_event_pair_has_declared_outcome(source: str, kind: str) -> None:
    _assert_transition(_at(source), source, kind)


@pytest.mark.parametrize("source,kind", LEGAL_CASES)
def test_duplicate_events_obey_contract_and_preserve_noop_identity(source: str, kind: str) -> None:
    floor = _at(source)
    for _ in range(4):
        source = _assert_transition(floor, source, kind)


@pytest.mark.parametrize("source,kind", ILLEGAL_CASES)
def test_rejected_event_does_not_prevent_recovery(source: str, kind: str) -> None:
    floor = _at(source)
    _assert_transition(floor, source, kind)
    source = _assert_transition(floor, source, "suspend")
    source = _assert_transition(floor, source, "resume")
    source = _assert_transition(floor, source, "provisional_speech")
    assert source == "user_speaking"


@pytest.mark.parametrize("source", CONTEXTS)
@pytest.mark.parametrize("terminal,restart", [("end_session", "activate"), ("suspend", "resume")])
def test_each_context_recovers_through_explicit_session_control(
    source: str, terminal: str, restart: str
) -> None:
    floor = _at(source)
    for kind in (
        terminal,
        "generation_completed",
        "playback_completed",
        "response_finished",
        "generation_cancelled",
        "playback_failed",
        restart,
        "provisional_speech",
        "final_speech",
        "generation_started",
        "playback_started",
        "generation_completed",
        "playback_completed",
        "response_finished",
    ):
        source = _assert_transition(floor, source, kind)
    assert source == "armed"


@pytest.mark.parametrize("source", ["thinking", "assistant_speaking"])
@pytest.mark.parametrize("finish", [False, True], ids=["playback_gap", "response_finished"])
def test_candidate_rejection_restores_latest_underlying_floor(source: str, finish: bool) -> None:
    floor = _at(source)
    source = _assert_transition(floor, source, "interruption_candidate")
    for kind in (
        "interruption_candidate",
        "provisional_speech",
        "silence",
        "generation_completed",
        "playback_started",
        "playback_completed",
        "playback_started",
        "playback_completed",
    ):
        source = _assert_transition(floor, source, kind)
    if finish:
        for kind in ("response_finished", "response_finished", "playback_completed"):
            source = _assert_transition(floor, source, kind)
        assert MATRIX[source]["playback_started"] is None
        _assert_transition(floor, source, "playback_started")
    source = _assert_transition(floor, source, "interruption_rejected")
    assert source == ("armed" if finish else "thinking")
    _assert_transition(floor, source, "interruption_rejected")


@pytest.mark.parametrize(
    "source", ["candidate_thinking", "candidate_assistant_speaking", "candidate_armed"]
)
def test_confirmed_candidate_accepts_new_speech_after_late_terminals(source: str) -> None:
    floor = _at(source)
    assert MATRIX[source]["final_speech"] is None
    _assert_transition(floor, source, "final_speech")
    for kind in (
        "interruption_confirmed",
        "interruption_confirmed",
        "generation_cancelled",
        "playback_cancelled",
        "generation_failed",
        "playback_failed",
        "response_finished",
        "provisional_speech",
        "final_speech",
        "generation_started",
    ):
        source = _assert_transition(floor, source, kind)
    assert source == "thinking"


@pytest.mark.parametrize(
    "source",
    [
        "thinking",
        "assistant_speaking",
        "candidate_thinking",
        "candidate_assistant_speaking",
        "candidate_armed",
    ],
)
@pytest.mark.parametrize(
    "kind", [E.GENERATION_CANCELLED, E.GENERATION_FAILED, E.PLAYBACK_CANCELLED, E.PLAYBACK_FAILED]
)
def test_concurrent_duplicate_failure_is_one_atomic_recovery(source: str, kind: E) -> None:
    floor = _at(source)
    before = floor.snapshot()
    ready = threading.Barrier(8, timeout=WATCHDOG_SECONDS)

    def fail() -> ConversationFloorSnapshot:
        ready.wait()
        return floor.apply(ConversationEvent(kind))

    snapshots = _results([_worker(fail) for _ in range(8)])
    assert all(snapshot is snapshots[0] for snapshot in snapshots)
    assert snapshots[0] == ConversationFloorSnapshot(S.INTERRUPTED, before.revision + 1)
    current = "interrupted"
    for event in ("final_speech", "generation_started", "playback_started", "response_finished"):
        current = _assert_transition(floor, current, event)
    assert current == "armed"


def test_concurrent_generation_and_playback_eof_do_not_finish_response() -> None:
    floor = _at("assistant_speaking")
    before = floor.snapshot()
    ready = threading.Barrier(8, timeout=WATCHDOG_SECONDS)

    def complete(kind: E) -> ConversationFloorSnapshot:
        ready.wait()
        return floor.apply(ConversationEvent(kind))

    kinds = [E.GENERATION_COMPLETED, E.PLAYBACK_COMPLETED] * 4
    snapshots = _results([_worker(lambda kind=kind: complete(kind)) for kind in kinds])
    after = floor.snapshot()
    assert after == ConversationFloorSnapshot(S.THINKING, before.revision + 1)
    assert all(snapshot is before or snapshot is after for snapshot in snapshots)
    for kind, snapshot in zip(kinds, snapshots):
        if kind is E.PLAYBACK_COMPLETED:
            assert snapshot is after
    _assert_transition(floor, "thinking", "response_finished")


def test_concurrent_readers_observe_each_whole_candidate_snapshot() -> None:
    floor = ConversationFloor()
    published = threading.Barrier(4, timeout=WATCHDOG_SECONDS)
    observed = threading.Barrier(4, timeout=WATCHDOG_SECONDS)
    # Explicit expected metadata and revisions; no scheduler-dependent sampling.
    stages = [
        (E.ACTIVATE, S.ARMED, 1, None),
        (E.PROVISIONAL_SPEECH, S.USER_SPEAKING, 2, None),
        (E.SILENCE, S.USER_PAUSED, 3, None),
        (E.PROVISIONAL_SPEECH, S.USER_SPEAKING, 4, None),
        (E.FINAL_SPEECH, S.FINALIZING, 5, None),
        (E.GENERATION_STARTED, S.THINKING, 6, None),
        (E.PLAYBACK_STARTED, S.ASSISTANT_SPEAKING, 7, None),
        (E.INTERRUPTION_CANDIDATE, S.BARGE_IN_CANDIDATE, 8, S.ASSISTANT_SPEAKING),
        (E.PLAYBACK_COMPLETED, S.BARGE_IN_CANDIDATE, 9, S.THINKING),
        (E.PLAYBACK_STARTED, S.BARGE_IN_CANDIDATE, 10, S.ASSISTANT_SPEAKING),
        (E.GENERATION_COMPLETED, S.BARGE_IN_CANDIDATE, 10, S.ASSISTANT_SPEAKING),
        (E.RESPONSE_FINISHED, S.BARGE_IN_CANDIDATE, 11, S.ARMED),
        (E.PLAYBACK_COMPLETED, S.BARGE_IN_CANDIDATE, 11, S.ARMED),
        (E.INTERRUPTION_REJECTED, S.ARMED, 12, None),
        (E.SUSPEND, S.SUSPENDED, 13, None),
        (E.RESUME, S.ARMED, 14, None),
        (E.END_SESSION, S.IDLE, 15, None),
    ]
    expected = [
        ConversationFloorSnapshot(state, revision, underlying)
        for _, state, revision, underlying in stages
    ]

    def write() -> list[ConversationFloorSnapshot]:
        snapshots = []
        for kind, _, _, _ in stages:
            snapshots.append(floor.apply(ConversationEvent(kind)))
            published.wait()
            observed.wait()
        return snapshots

    def read() -> list[ConversationFloorSnapshot]:
        snapshots = []
        for wanted in expected:
            published.wait()
            current = floor.snapshot()
            assert current == wanted
            snapshots.append(current)
            observed.wait()
        return snapshots

    writer, *readers = _results([_worker(write), *[_worker(read) for _ in range(3)]])
    assert writer == expected
    assert all(
        all(actual is committed for actual, committed in zip(reader, writer)) for reader in readers
    )
    assert writer[9] is writer[10] and writer[11] is writer[12]
    assert floor.snapshot() is writer[-1]


@pytest.mark.parametrize("fail_construction", [False, True], ids=["publish", "recover"])
def test_snapshot_publication_and_failure_release_waiting_reader(
    monkeypatch, fail_construction: bool
) -> None:
    floor = _at("candidate_assistant_speaking")
    before = floor.snapshot()
    constructing = threading.Event()
    release = threading.Event()
    reader_started = threading.Event()
    original_snapshot = control.ConversationFloorSnapshot

    def construct(*args) -> ConversationFloorSnapshot:
        constructing.set()
        assert release.wait(timeout=WATCHDOG_SECONDS)
        if fail_construction:
            raise RuntimeError("injected local construction failure")
        return original_snapshot(*args)

    def write() -> ConversationFloorSnapshot:
        if fail_construction:
            with pytest.raises(RuntimeError, match="^injected local construction failure$"):
                floor.apply(ConversationEvent(E.RESPONSE_FINISHED))
            return before
        return floor.apply(ConversationEvent(E.RESPONSE_FINISHED))

    def read() -> ConversationFloorSnapshot:
        reader_started.set()
        return floor.snapshot()

    with monkeypatch.context() as patch:
        patch.setattr(control, "ConversationFloorSnapshot", construct)
        writer = _worker(write)
        workers = [writer]
        try:
            assert constructing.wait(timeout=WATCHDOG_SECONDS)
            reader = _worker(read)
            workers.append(reader)
            assert reader_started.wait(timeout=WATCHDOG_SECONDS)
            assert not reader[1].done()
        finally:
            release.set()
            snapshots = _results(workers)
        assert snapshots[0] is snapshots[1]
        assert floor.snapshot() is snapshots[0]
    if fail_construction:
        assert floor.snapshot() is before
        recovered = _results([_worker(lambda: floor.apply(ConversationEvent(E.RESPONSE_FINISHED)))])
        assert recovered[0] == ConversationFloorSnapshot(
            S.BARGE_IN_CANDIDATE, before.revision + 1, S.ARMED
        )
    else:
        assert snapshots[0] == ConversationFloorSnapshot(
            S.BARGE_IN_CANDIDATE, before.revision + 1, S.ARMED
        )
    _assert_transition(floor, "candidate_armed", "interruption_rejected")


def test_reused_events_do_not_share_floor_state_or_retain_correlation_payloads(
    caplog, capsys
) -> None:
    first, second = ConversationFloor(), ConversationFloor()
    shared = ConversationEvent(E.ACTIVATE)
    first_armed = first.apply(shared)
    second_armed = second.apply(shared)
    assert first_armed == second_armed and first_armed is not second_armed
    first.apply(ConversationEvent(E.FINAL_SPEECH))
    assert second.snapshot() is second_armed
    second.apply(ConversationEvent(E.END_SESSION))
    assert first.snapshot() == ConversationFloorSnapshot(S.FINALIZING, 2)
    assert second.snapshot() == ConversationFloorSnapshot(S.IDLE, 2)
    # Run/case/turn correlation is external. The floor cannot accept or log a
    # speech payload, and this check does not claim runtime stale-turn rejection.
    assert [field.name for field in fields(ConversationEvent)] == ["kind"]
    assert [field.name for field in fields(ConversationFloorSnapshot)] == [
        "state",
        "revision",
        "candidate_return_state",
    ]
    assert asdict(shared) == {"kind": E.ACTIVATE}
    assert not hasattr(shared, "__dict__")
    assert not hasattr(first_armed, "__dict__")
    assert not caplog.records
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("kind", list(E))
def test_events_carry_only_immutable_closed_kind(kind: E) -> None:
    event = ConversationEvent(kind)
    assert json.loads(json.dumps(asdict(event))) == {"kind": kind.value}
    with pytest.raises(FrozenInstanceError):
        event.kind = E.END_SESSION  # type: ignore[misc]
    assert event.kind is kind


@pytest.mark.parametrize("source", CONTEXTS)
def test_published_snapshots_remain_immutable_after_session_end(source: str) -> None:
    floor = _at(source)
    snapshot = floor.snapshot()
    preserved = asdict(snapshot)
    for name, value in (("state", S.IDLE), ("revision", 999), ("candidate_return_state", S.ARMED)):
        with pytest.raises(FrozenInstanceError):
            setattr(snapshot, name, value)
    floor.apply(ConversationEvent(E.END_SESSION))
    assert asdict(snapshot) == preserved


@pytest.mark.parametrize("bad", [None, True, 1, "activate", "synthetic-private-payload", object()])
def test_untyped_signals_fail_without_logging_or_echoing_payload(
    bad: object, caplog, capsys
) -> None:
    floor = _at("thinking")
    before = floor.snapshot()
    with pytest.raises(TypeError, match="^kind must be a ConversationEventKind$"):
        ConversationEvent(bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="^event must be a ConversationEvent$"):
        floor.apply(bad)  # type: ignore[arg-type]
    assert floor.snapshot() is before
    assert not caplog.records
    assert capsys.readouterr() == ("", "")


def test_event_subclass_cannot_smuggle_speech_into_floor(caplog, capsys) -> None:
    @dataclass(frozen=True, slots=True)
    class PayloadEvent(ConversationEvent):
        transcript: str

    floor = ConversationFloor()
    before = floor.snapshot()
    event = PayloadEvent(E.ACTIVATE, "synthetic-private-payload")
    with pytest.raises(TypeError, match="^event must be a ConversationEvent$"):
        floor.apply(event)
    assert floor.snapshot() is before
    assert not caplog.records
    assert capsys.readouterr() == ("", "")
