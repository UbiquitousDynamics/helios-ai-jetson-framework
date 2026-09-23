"""Task 01 local state/event evidence and read-only HIL prerequisite reporting.

Local signals are deterministic control inputs, not observed audio. No device
is opened by this increment. Task 01 requires separate acoustic evidence before
its exhaustive gate can pass; missing devices/calibration produce blocked HIL.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.conversation_control import (  # noqa: E402
    ConversationEvent, ConversationEventKind as E, ConversationFloor,
    ConversationFloorSnapshot, ConversationFloorState as S, InvalidConversationTransition,
)
from api.realtime_conversation import ResponseEvent as R  # noqa: E402
from scripts.voice_test_suite import (  # noqa: E402
    SPEC, SpecificationError, artifact_root, environment, finite_number,
    implementation_manifest, read_json, require, sha256,
)


class EventTrace:
    """Bounded, thread-safe, content-free record of local control observations.

    Correlation is added at the test adapter boundary; the production floor
    deliberately carries no request ID. A trace is evidence of local logic only.
    """

    def __init__(self, case_ids: set[str], *, maximum_records: int, clock=time.perf_counter_ns):
        require(type(maximum_records) is int and maximum_records > 0, "invalid_trace_bound")
        require(bool(case_ids) and all(isinstance(value, str) for value in case_ids),
                "invalid_case_catalog")
        self.run_id = uuid.uuid4()
        self._correlations = {key: str(uuid.uuid5(self.run_id, key)) for key in case_ids}
        self._fixtures = {key: f"task01-{index:03d}" for index, key in enumerate(sorted(case_ids), 1)}
        self._host_id = str(uuid.uuid5(self.run_id, "pc"))
        self._clock_id = str(uuid.uuid5(self.run_id, "pc-perf-counter"))
        self._maximum = maximum_records
        self._clock = clock
        self._rows: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, case_id: str, event: E | R, snapshot: ConversationFloorSnapshot, *,
               outcome: str, response_id: int | None = None) -> None:
        require(case_id in self._correlations, "unknown_trace_case")
        require(type(event) in {E, R}, "untyped_trace_event")
        require(type(snapshot) is ConversationFloorSnapshot, "untyped_trace_snapshot")
        require(outcome in {"accepted", "rejected", "ignored"}, "invalid_trace_outcome")
        require(response_id is None or (type(response_id) is int and response_id > 0),
                "invalid_trace_response_id")
        with self._lock:
            require(len(self._rows) < self._maximum, "trace_capacity_exceeded")
            stamp = self._clock()
            require(type(stamp) is int and stamp >= 0, "invalid_trace_timestamp")
            require(not self._rows or stamp >= self._rows[-1]["monotonic_ns"],
                    "trace_clock_reversed")
            self._rows.append({
                "schema_version": 1, "task_id": "01", "evidence_layer": "local_logic",
                "run_id": str(self.run_id), "correlation_id": self._correlations[case_id],
                "fixture_id": self._fixtures[case_id], "host_id": self._host_id,
                "event_id": len(self._rows) + 1, "event_kind": event.value,
                "event_scope": "floor" if type(event) is E else "response",
                "clock_id": self._clock_id, "monotonic_ns": stamp,
                "outcome": outcome, "response_id": response_id,
                "floor_state": snapshot.state.value, "revision": snapshot.revision,
                "candidate_return_state": (snapshot.candidate_return_state.value
                                           if snapshot.candidate_return_state else None),
            })

    def rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._rows]

    def correlation(self, case_id: str) -> str:
        require(case_id in self._correlations, "unknown_trace_case")
        return self._correlations[case_id]

    def fixture_id(self, case_id: str) -> str:
        require(case_id in self._fixtures, "unknown_trace_case")
        return self._fixtures[case_id]


def matrix_cases(contract: dict[str, Any]) -> list[tuple[str, str]]:
    contexts = contract["contexts"]
    matrix = contract["matrix"]
    kinds = {kind.value for kind in E}
    require(contract.get("schema_version") == 1 and contract.get("contract_version") == "1.0.0"
            and contract.get("test_task") == "01", "unsupported_transition_contract")
    require(isinstance(contexts, dict) and isinstance(matrix, dict), "context_mapping_required")
    require(isinstance(contract["event_kinds"], list), "event_list_required")
    require(set(contract["event_kinds"]) == kinds and len(contract["event_kinds"]) == len(kinds),
            "event_contract_incomplete")
    require(set(matrix) == set(contexts), "context_contract_incomplete")
    expected_contexts = {(state.value, None) for state in S if state is not S.BARGE_IN_CANDIDATE}
    expected_contexts |= {(S.BARGE_IN_CANDIDATE.value, state.value)
                          for state in (S.ARMED, S.THINKING, S.ASSISTANT_SPEAKING)}
    observed_contexts = set()
    for context in contexts.values():
        require(isinstance(context, dict), "context_object_required")
        require(type(context.get("expected_revision")) is int and context["expected_revision"] >= 0,
                "invalid_expected_revision")
        require(isinstance(context.get("event_prefix"), list) and
                all(isinstance(kind, str) and kind in kinds for kind in context["event_prefix"]),
                "invalid_context_prefix")
        metadata = (context["state"], context["candidate_return_state"])
        require(metadata in expected_contexts, "invalid_context_metadata")
        observed_contexts.add(metadata)
    require(observed_contexts == expected_contexts and len(contexts) == len(expected_contexts),
            "distinct_context_coverage")
    for source, row in matrix.items():
        require(isinstance(row, dict), "transition_row_required")
        require(set(row) == kinds, "transition_contract_incomplete")
        require(all(value is None or value in contexts for value in row.values()),
                "transition_target_missing")
    return [(source, kind) for source in contexts for kind in contract["event_kinds"]]


def validate_profile(profile: dict[str, Any], case_count: int) -> None:
    required = {"transition_verdict_errors", "transition_state_errors", "revision_errors",
                "rejected_event_mutations", "snapshot_identity_errors"}
    require(type(profile["required_matrix_cases"]) is int and
            profile["required_matrix_cases"] == case_count, "matrix_sample_count")
    require(type(profile["maximum_trace_records"]) is int and
            profile["maximum_trace_records"] >= case_count, "insufficient_trace_bound")
    invariants = profile["invariants"]
    require(isinstance(invariants, dict) and set(invariants) == required, "invariant_coverage")
    for rule in invariants.values():
        require(isinstance(rule, dict) and rule.get("unit") == "cases" and
                rule.get("mandatory") is True, "invalid_invariant_definition")
        require(type(rule.get("maximum")) is int and rule["maximum"] == 0,
                "logical_invariant_cannot_be_relaxed")
        require(type(rule.get("minimum_samples")) is int and
                rule["minimum_samples"] == case_count, "invariant_sample_count")


def run_matrix(contract: dict[str, Any], profile: dict[str, Any]) -> tuple[dict, list, list]:
    """Execute independent declared expectations against the real pure floor."""
    cases = matrix_cases(contract)
    validate_profile(profile, len(cases))
    ids = {f"{source}:{kind}" for source, kind in cases}
    trace = EventTrace(ids, maximum_records=profile["maximum_trace_records"])
    metrics = {key: 0 for key in profile["invariants"]}
    results = []
    legal = illegal = 0
    for source, kind in cases:
        case_id = f"{source}:{kind}"
        floor = ConversationFloor()
        for prefix in contract["contexts"][source]["event_prefix"]:
            floor.apply(ConversationEvent(E(prefix)))
        before = floor.snapshot()
        source_context = contract["contexts"][source]
        require(before.state.value == source_context["state"] and
                (before.candidate_return_state.value if before.candidate_return_state else None)
                == source_context["candidate_return_state"], "context_prefix_mismatch")
        require(before.revision == source_context["expected_revision"], "context_revision_mismatch")
        target_id = contract["matrix"][source][kind]
        expected_legal = target_id is not None
        legal += int(expected_legal)
        illegal += int(not expected_legal)
        rejected = False
        try:
            floor.apply(ConversationEvent(E(kind)))
        except InvalidConversationTransition:
            rejected = True
        after = floor.snapshot()
        violations = []
        if expected_legal == rejected:
            violations.append("transition_verdict_errors")
        if expected_legal:
            target = contract["contexts"][target_id]
            return_state = after.candidate_return_state.value if after.candidate_return_state else None
            if after.state.value != target["state"] or return_state != target["candidate_return_state"]:
                violations.append("transition_state_errors")
            changed = (after.state, after.candidate_return_state) != (
                before.state, before.candidate_return_state)
            if after.revision != before.revision + int(changed):
                violations.append("revision_errors")
            if (after is before) != (not changed):
                violations.append("snapshot_identity_errors")
        elif after is not before:
            violations.append("rejected_event_mutations")
        for violation in violations:
            metrics[violation] += 1
        trace.record(case_id, E(kind), after, outcome="rejected" if rejected else "accepted")
        results.append({"case_id": case_id, "correlation_id": trace.correlation(case_id),
                        "fixture_id": trace.fixture_id(case_id),
                        "expected_legal": expected_legal, "status": "failed" if violations else "passed",
                        "failures": violations})
    thresholds = profile["invariants"]
    passed = all(metrics[key] <= thresholds[key]["maximum"] for key in metrics)
    return {"status": "passed" if passed else "failed", "sample_count": len(cases),
            "legal_cases": legal, "illegal_cases": illegal, "metrics": metrics,
            "run_id": str(trace.run_id)}, results, trace.rows()


def hil_prerequisites(manifest: dict[str, Any], dependencies: dict[str, str | None]) -> dict:
    """Inspect prerequisites without enumerating, selecting or opening devices.

    A complete manifest is not proof of measured calibration or acoustic success.
    Until the actual runner can be validated on the selected hardware, HIL stays
    blocked even if a caller fills in this declaration.
    """
    reasons = []
    devices = manifest.get("devices")
    roles = ("pc_output", "pc_input", "helios_input", "helios_output")
    valid_devices = isinstance(devices, dict) and set(devices) == set(roles)
    if valid_devices:
        for device in devices.values():
            if not isinstance(device, dict):
                valid_devices = False
                break
            valid_devices &= (
                type(device.get("index")) is int and device["index"] >= 0
                and isinstance(device.get("host_api"), str) and bool(device["host_api"].strip())
                and isinstance(device.get("name"), str) and bool(device["name"].strip())
                and device["name"].strip().casefold() != "default"
                and type(device.get("sample_rate_hz")) is int and device["sample_rate_hz"] > 0
                and type(device.get("channels")) is int and device["channels"] > 0
            )
    if not valid_devices:
        reasons.append("explicit_devices_missing_or_invalid")
    isolation = manifest.get("isolation", {})
    if not isinstance(isolation, dict) or not all(isolation.get(key) is True for key in (
        "generated_speech_only", "isolated_speakers_or_headphones",
        "monitoring_and_loopback_disabled", "no_people_in_capture_area",
    )):
        reasons.append("synthetic_only_isolation_unconfirmed")
    calibration = manifest.get("calibration", {})
    if not isinstance(calibration, dict) or calibration.get("status") != "verified":
        reasons.append("acoustic_calibration_unverified")
    else:
        # A self-declared status cannot validate a file or its device binding.
        reasons.append("calibration_artifact_not_verified_by_runner")
    limits = manifest.get("limits", {})
    if not isinstance(limits, dict) or not all(
        finite_number(limits.get(key)) and limits[key] > 0 for key in (
            "exchange_timeout_seconds", "session_timeout_seconds",
            "shutdown_timeout_seconds", "maximum_self_triggers",
        )
    ):
        reasons.append("bounded_execution_limits_missing")
    missing = [name for name in ("piper-tts", "vosk", "sounddevice", "PyAudio", "onnxruntime")
               if dependencies.get(name) is None]
    if missing:
        reasons.append("pc_native_dependencies_missing")
    reasons.append("acoustic_execution_and_event_mapping_not_verified")
    return {"status": "blocked", "required_for_task": True, "reasons": reasons,
            "missing_packages": missing, "device_inventory_performed": False,
            "devices_opened": 0, "acoustic_exchanges": 0,
            "runner_status": "pending_selected_devices_and_calibration",
            "prohibited_inference": "Local floor events do not establish observed playback/capture mapping."}


def run(output: Path, profile_name: str, hardware_path: Path) -> dict[str, Any]:
    contract_path = SPEC / "task01-transitions.json"
    profiles_path = SPEC / "task01-profile.json"
    contract, profiles = read_json(contract_path), read_json(profiles_path)
    profile = profiles["profiles"][profile_name]
    summary, cases, events = run_matrix(contract, profile)
    env = environment()
    env.update(clock="time.perf_counter_ns", clock_resolution_seconds=
               time.get_clock_info("perf_counter").resolution)
    hardware = read_json(hardware_path)
    hil = hil_prerequisites(hardware, env["dependencies"])
    report = {
        "schema_version": 1, "task_id": "01", "run_id": summary["run_id"],
        "status": "failed" if summary["status"] == "failed" else "blocked",
        "local_matrix": summary, "hil": hil, "environment": env,
        "implementation": implementation_manifest(ROOT),
        "threshold_profile": profile_name, "threshold_version": profiles["profile_version"],
        "thresholds": profile["invariants"],
        "spec_sha256": {"task01-transitions.json": sha256(contract_path),
                        "task01-profile.json": sha256(profiles_path),
                        "hardware_manifest": sha256(hardware_path)},
        "harness_sha256": sha256(Path(__file__)),
        "cleanup": {"status": "passed", "audio_streams_opened": 0,
                    "workers_started": 0, "audio_files_created": 0, "transcripts_created": 0},
        "acoustic_metrics": {"sample_count": 0, "status": "unverified", "median": None,
                             "p95": None, "maximum": None, "wer": None, "cer": None},
        "next_task": "02", "next_task_started": False,
        "artifacts": ["results.json", "cases.jsonl", "events.jsonl"],
    }
    # The pure control model is central to Task 01; record it in addition to
    # the shared baseline identity list, which predates this runtime test group.
    report["implementation"]["files"]["api/conversation_control.py"] = sha256(
        ROOT / "api/conversation_control.py")
    directory = artifact_root(output) / summary["run_id"]
    directory.mkdir(exist_ok=False)
    for name, rows in (("cases", cases), ("events", events)):
        (directory / f"{name}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (directory / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--profile", choices=("desktop", "jetson"), default="desktop")
    parser.add_argument("--hardware-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = run(args.artifact_dir, args.profile, args.hardware_manifest)
    except (SpecificationError, OSError, ValueError, KeyError, TypeError, AttributeError):
        print(json.dumps({"schema_version": 1, "task_id": "01", "status": "failed",
                          "reason": "task01_validation_failed"}))
        return 1
    print(json.dumps(report, indent=2))
    return 1 if report["status"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
