"""Task 01 correlation/trace tests; deterministic signals, no acoustic evidence."""

from __future__ import annotations

import copy
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from itertools import count

import pytest

from api.conversation_control import (
    ConversationEvent,
    ConversationEventKind as E,
    ConversationFloor,
)
from api.realtime_conversation import RealtimeConversationController, ResponseEvent as R
from recognizer.turn_endpoint_detector import TurnEndpointConfig
from scripts import voice_suite_task01 as task

pytestmark = pytest.mark.voice_local
PROFILE = task.read_json(task.SPEC / "task01-profile.json")
JOIN_TIMEOUT = PROFILE["local_test_bounds"]["thread_join_seconds"]


def controller():
    return RealtimeConversationController(endpointing=TurnEndpointConfig(), activity_energy=0.08)


def test_matrix_runner_reports_every_legal_and_illegal_pair_without_audio():
    contract = task.read_json(task.SPEC / "task01-transitions.json")
    summary, cases, events = task.run_matrix(contract, PROFILE["profiles"]["desktop"])
    assert summary["status"] == "passed"
    assert summary["sample_count"] == len(cases) == len(events) == 228
    assert summary["legal_cases"] == 136 and summary["illegal_cases"] == 92
    assert set(summary["metrics"].values()) == {0}
    assert len({row["correlation_id"] for row in events}) == 228
    assert all(
        case["correlation_id"] == event["correlation_id"] for case, event in zip(cases, events)
    )
    assert all(case["fixture_id"] == event["fixture_id"] for case, event in zip(cases, events))
    fields = task.read_json(task.SPEC / "metrics.json")["correlation"]["required_fields"]
    assert all(set(fields) <= row.keys() for row in events)
    assert {row["evidence_layer"] for row in events} == {"local_logic"}


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("bad_revision", "context_revision_mismatch"),
        ("list_contexts", "context_mapping_required"),
        ("duplicate_context", "distinct_context_coverage"),
        ("missing_event", "event_contract_incomplete"),
        ("missing_cell", "transition_contract_incomplete"),
        ("empty_invariants", "invariant_coverage"),
        ("relaxed_invariant", "logical_invariant_cannot_be_relaxed"),
        ("insufficient_samples", "invariant_sample_count"),
    ],
)
def test_malformed_contract_or_profile_cannot_produce_partial_pass(mutation, error):
    contract = task.read_json(task.SPEC / "task01-transitions.json")
    profile = copy.deepcopy(PROFILE["profiles"]["desktop"])
    if mutation == "bad_revision":
        contract["contexts"]["idle"]["expected_revision"] = 999999
    elif mutation == "list_contexts":
        contract["contexts"] = contract["matrix"] = []
    elif mutation == "duplicate_context":
        contract["contexts"]["armed"] = copy.deepcopy(contract["contexts"]["idle"])
    elif mutation == "missing_event":
        contract["event_kinds"].pop()
    elif mutation == "missing_cell":
        del contract["matrix"]["idle"]["activate"]
    elif mutation == "empty_invariants":
        profile["invariants"] = {}
    elif mutation == "relaxed_invariant":
        profile["invariants"]["revision_errors"]["maximum"] = 2
    elif mutation == "insufficient_samples":
        profile["invariants"]["revision_errors"]["minimum_samples"] = 1
    with pytest.raises(task.SpecificationError, match=f"^{error}$"):
        task.run_matrix(contract, profile)


@pytest.mark.parametrize("phase", list(R))
@pytest.mark.parametrize("old_termination", ["finished", "interrupted"])
def test_late_lifecycle_is_bound_to_old_response_and_cannot_mutate_current(phase, old_termination):
    control = controller()
    trace = task.EventTrace({"old", "current"}, maximum_records=4)
    old = control.begin_response()
    if old_termination == "finished":
        control.finish_response(old)
    else:
        control.interruption()
    current = control.begin_response()
    try:
        before = control.snapshot()
        control.response_event(old, phase)
        control.finish_response(old, failed=True)
        assert control.snapshot() == before
        trace.record("old", phase, before.floor, response_id=old, outcome="ignored")
        control.response_event(current, R.PLAYBACK_STARTED)
        trace.record(
            "current",
            E.PLAYBACK_STARTED,
            control.snapshot().floor,
            response_id=current,
            outcome="accepted",
        )
        rows = trace.rows()
        assert rows[0]["response_id"] != rows[1]["response_id"]
        assert rows[0]["event_kind"] == phase.value and rows[0]["event_scope"] == "response"
        assert rows[0]["correlation_id"] != rows[1]["correlation_id"]
        assert rows[1]["floor_state"] == "assistant_speaking"
    finally:
        control.stop()


@pytest.mark.parametrize("stage", [R.SYNTHESIS_FAILED, R.PLAYBACK_FAILED])
def test_stage_failure_has_correlated_recovery_and_late_start_cannot_resurrect(stage):
    control = controller()
    response = control.begin_response()
    trace = task.EventTrace({"exchange"}, maximum_records=4)
    try:
        control.response_event(response, R.PLAYBACK_STARTED)
        trace.record(
            "exchange",
            E.PLAYBACK_STARTED,
            control.snapshot().floor,
            response_id=response,
            outcome="accepted",
        )
        control.response_event(response, stage)
        interrupted = control.snapshot()
        assert interrupted.floor.state.value == "interrupted"
        trace.record("exchange", stage, interrupted.floor, response_id=response, outcome="accepted")
        control.response_event(response, R.PLAYBACK_STARTED)
        assert control.snapshot() == interrupted
        control.finish_response(response, failed=True)
        assert control.snapshot().floor.state.value == "armed"
        trace.record(
            "exchange",
            E.RESPONSE_FINISHED,
            control.snapshot().floor,
            response_id=response,
            outcome="accepted",
        )
        assert len({row["correlation_id"] for row in trace.rows()}) == 1
    finally:
        control.stop()


def test_capture_lease_snapshots_are_local_ownership_evidence_not_microphone_events():
    control = controller()
    with control.capture() as lease:
        snapshot = control.snapshot()
        assert snapshot.capture_active and snapshot.floor.state.value == "armed"
        assert not lease.finished.is_set()
    assert lease.finished.is_set()
    assert not control.snapshot().capture_active
    assert set(asdict(snapshot)) == {
        "floor",
        "capture_active",
        "response_id",
        "synthesizing",
        "generation_complete",
        "stopped",
    }
    control.stop()


def test_trace_is_content_free_bounded_and_detached_from_mutable_exports():
    trace = task.EventTrace({"synthetic secret should not be exported"}, maximum_records=1)
    floor = ConversationFloor()
    trace.record(
        "synthetic secret should not be exported",
        E.ACTIVATE,
        floor.apply(ConversationEvent(E.ACTIVATE)),
        outcome="accepted",
    )
    rows = trace.rows()
    assert "synthetic secret" not in json.dumps(rows)
    assert uuid.UUID(rows[0]["correlation_id"])
    rows[0]["floor_state"] = "tampered"
    assert trace.rows()[0]["floor_state"] == "armed"
    with pytest.raises(task.SpecificationError, match="trace_capacity_exceeded"):
        trace.record(
            "synthetic secret should not be exported",
            E.SILENCE,
            floor.snapshot(),
            outcome="accepted",
        )
    assert len(trace.rows()) == 1


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("event", "private input", "untyped_trace_event"),
        ("snapshot", {"text": "private input"}, "untyped_trace_snapshot"),
        ("case_id", "private input", "unknown_trace_case"),
        ("outcome", "private input", "invalid_trace_outcome"),
        ("response_id", True, "invalid_trace_response_id"),
        ("response_id", -1, "invalid_trace_response_id"),
    ],
)
def test_invalid_trace_fields_never_echo_input(field, value, error):
    trace = task.EventTrace({"case"}, maximum_records=1)
    arguments = dict(
        case_id="case",
        event=E.SILENCE,
        snapshot=ConversationFloor().snapshot(),
        outcome="accepted",
        response_id=None,
    )
    arguments[field] = value
    with pytest.raises(task.SpecificationError, match=f"^{error}$"):
        trace.record(**arguments)
    assert trace.rows() == []


def test_clock_reversal_is_rejected_without_partial_record():
    timestamps = iter([10, 9])
    trace = task.EventTrace({"case"}, maximum_records=2, clock=lambda: next(timestamps))
    floor = ConversationFloor()
    trace.record("case", E.SILENCE, floor.snapshot(), outcome="accepted")
    with pytest.raises(task.SpecificationError, match="trace_clock_reversed"):
        trace.record("case", E.SILENCE, floor.snapshot(), outcome="accepted")
    assert len(trace.rows()) == 1


def test_separate_runs_have_distinct_clock_and_host_ids_and_stable_fixture_ids():
    first = task.EventTrace({"case"}, maximum_records=1)
    second = task.EventTrace({"case"}, maximum_records=1)
    for trace in (first, second):
        trace.record("case", E.SILENCE, ConversationFloor().snapshot(), outcome="accepted")
    for field in ("run_id", "correlation_id", "clock_id", "host_id"):
        assert first.rows()[0][field] != second.rows()[0][field]
    assert first.rows()[0]["fixture_id"] == second.rows()[0]["fixture_id"]


def test_concurrent_trace_publication_is_atomic_and_workers_join():
    trace = task.EventTrace({"case"}, maximum_records=16, clock=count().__next__)
    floor = ConversationFloor()
    barrier = threading.Barrier(4, timeout=JOIN_TIMEOUT)
    workers = []

    def writer():
        workers.append(threading.current_thread())
        barrier.wait()
        for _ in range(4):
            trace.record("case", E.SILENCE, floor.snapshot(), outcome="accepted")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(writer) for _ in range(4)]
        for future in futures:
            future.result(timeout=JOIN_TIMEOUT)
    assert not any(worker.is_alive() for worker in workers)
    rows = trace.rows()
    assert [row["event_id"] for row in rows] == list(range(1, 17))
    assert [row["monotonic_ns"] for row in rows] == list(range(16))
    assert len({row["correlation_id"] for row in rows}) == 1


def test_hardware_example_reports_real_missing_prerequisites_without_opening_devices():
    manifest = task.read_json(task.SPEC / "hardware.example.json")
    result = task.hil_prerequisites(manifest, {})
    assert result["status"] == "blocked"
    assert result["required_for_task"]
    assert result["devices_opened"] == result["acoustic_exchanges"] == 0
    assert not result["device_inventory_performed"]
    assert "explicit_devices_missing_or_invalid" in result["reasons"]
    assert "acoustic_calibration_unverified" in result["reasons"]


def test_filled_manifest_cannot_impersonate_measured_calibration_or_acoustic_pass():
    manifest = task.read_json(task.SPEC / "hardware.example.json")
    for role in manifest["devices"]:
        manifest["devices"][role] = {
            "index": 0,
            "name": "synthetic fixture device",
            "host_api": "test",
            "sample_rate_hz": 22050,
            "channels": 1,
        }
    manifest["isolation"] = {key: True for key in manifest["isolation"]}
    manifest["calibration"]["status"] = "verified"
    dependencies = {
        name: "fixture-version"
        for name in (
            "piper-tts",
            "vosk",
            "sounddevice",
            "PyAudio",
            "onnxruntime",
        )
    }
    result = task.hil_prerequisites(manifest, dependencies)
    assert result["status"] == "blocked"
    assert "explicit_devices_missing_or_invalid" not in result["reasons"]
    assert "calibration_artifact_not_verified_by_runner" in result["reasons"]
    assert "acoustic_execution_and_event_mapping_not_verified" in result["reasons"]


def test_cli_retains_local_success_and_separate_hil_blocked_result(tmp_path, capsys):
    code = task.main(
        [
            "--artifact-dir",
            str(tmp_path),
            "--hardware-manifest",
            str(task.SPEC / "hardware.example.json"),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["status"] == "blocked"
    assert report["local_matrix"]["status"] == "passed"
    assert report["acoustic_metrics"]["sample_count"] == 0
    directory = tmp_path / report["run_id"]
    assert set(file.name for file in directory.iterdir()) == {
        "results.json",
        "cases.jsonl",
        "events.jsonl",
    }
    assert len((directory / "events.jsonl").read_text().splitlines()) == 228


def test_cli_bad_manifest_is_sanitized_failure(tmp_path, capsys):
    path = tmp_path / "hardware.json"
    path.write_text("private malformed text", encoding="utf-8")
    assert task.main(["--artifact-dir", str(tmp_path), "--hardware-manifest", str(path)]) == 1
    result = capsys.readouterr().out
    assert "private" not in result
    assert json.loads(result)["status"] == "failed"
