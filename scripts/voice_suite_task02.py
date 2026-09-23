"""Task 02 local authority evidence; acoustic HIL remains a separate required gate.

The runner uses the real revision aggregator and authority boundary. It does not
turn declared synthetic text into audio or claim microphone recognition.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.transcripts import (  # noqa: E402
    AuthoritativeUtterance, ProvisionalRevision, TranscriptBoundaryError,
    TranscriptRevisionAggregator, authoritative_text,
)
from scripts.voice_test_suite import (  # noqa: E402
    SPEC, SpecificationError, artifact_root, environment,
    implementation_manifest, read_json, require, sha256, summarize,
)

_METRICS = frozenset({"authority_mismatches", "final_count_mismatches",
                      "provisional_sink_admissions"})
_SCENARIOS = frozenset({
    "en_revised_final", "it_revised_final", "stale_capture_and_segment",
    "stale_revision_and_empty_final", "provisional_only_timeout",
    "en_identical_distinct_segments", "it_identical_distinct_captures",
    "capture_restart_resets_segment_revision", "stale_partial_after_new_capture",
    "final_without_prior_partial",
})
_EXPECTED_EVENTS = {"provisional": 10, "final": 14, "ignored": 12}


def validate(contract: dict[str, Any], profile: dict[str, Any]) -> list[dict[str, Any]]:
    require(contract.get("schema_version") == 1 and
            contract.get("contract_version") == "1.0.0" and
            contract.get("test_task") == "02",
            "invalid_contract_version")
    scenarios = contract.get("scenarios")
    require(isinstance(scenarios, list) and len(scenarios) >= 10, "insufficient_scenarios")
    require(type(profile.get("minimum_scenarios")) is int and
            profile["minimum_scenarios"] == 10 and len(scenarios) >= 10,
            "invalid_scenario_threshold")
    require(type(profile.get("maximum_events")) is int and
            0 < profile["maximum_events"] <= 1024, "invalid_event_bound")
    require(isinstance(profile.get("invariants"), dict) and
            set(profile["invariants"]) == _METRICS, "invariant_coverage")
    for rule in profile["invariants"].values():
        require(isinstance(rule, dict) and type(rule.get("maximum")) is int and
                rule["maximum"] == 0 and type(rule.get("minimum_samples")) is int and
                rule["minimum_samples"] == 10, "logical_invariant_cannot_be_relaxed")
    ids = set()
    total_events = 0
    languages = set()
    declared_events = {key: 0 for key in _EXPECTED_EVENTS}
    for scenario in scenarios:
        require(isinstance(scenario, dict), "invalid_scenario")
        identifier = scenario.get("id")
        require(isinstance(identifier, str) and identifier.isascii() and
                identifier.replace("-", "").replace("_", "").isalnum() and
                identifier not in ids, "invalid_scenario_id")
        ids.add(identifier)
        language = scenario.get("language")
        require(language in {"it", "en"}, "invalid_scenario_language")
        languages.add(language)
        events = scenario.get("events")
        require(isinstance(events, list) and bool(events), "scenario_events_missing")
        total_events += len(events)
        require(type(scenario.get("expected_final_count")) is int and
                0 <= scenario["expected_final_count"] <= len(events), "invalid_final_count")
        require(scenario["expected_final_count"] == sum(
            event.get("expected") == "final" for event in events if isinstance(event, dict)),
            "declared_final_count_mismatch")
        for event in events:
            require(isinstance(event, dict) and isinstance(event.get("text"), str) and
                    type(event.get("is_final")) is bool and
                    event.get("expected") in {"provisional", "final", "ignored"},
                    "invalid_declared_event")
            for key in ("capture_id", "segment_id", "revision"):
                require(key in event, "missing_segment_identity")
                value = event.get(key)
                require(value is None or (type(value) is int and value > 0),
                        "invalid_segment_identity")
            declared_events[event["expected"]] += 1
    require(ids == _SCENARIOS and declared_events == _EXPECTED_EVENTS,
            "contract_coverage_missing")
    require(languages == {"it", "en"}, "language_coverage")
    require(total_events <= profile["maximum_events"], "event_capacity_exceeded")
    return scenarios


def run_local(contract: dict[str, Any], profile: dict[str, Any]) -> tuple[dict, list, list]:
    scenarios = validate(contract, profile)
    run_id = uuid.uuid4()
    host_id = str(uuid.uuid5(run_id, "pc"))
    clock_id = str(uuid.uuid5(run_id, "pc-perf-counter"))
    samples = {key: [] for key in _METRICS}
    cases = []
    rows = []
    for case in scenarios:
        promoter = TranscriptRevisionAggregator()
        correlation_id = str(uuid.uuid5(run_id, case["id"]))
        final_count = 0
        failures = []
        for event in case["events"]:
            value = promoter.observe(event["text"], is_final=event["is_final"],
                                     capture_id=event["capture_id"],
                                     segment_id=event["segment_id"],
                                     revision=event["revision"])
            observed = ("final" if isinstance(value, AuthoritativeUtterance) else
                        "provisional" if isinstance(value, ProvisionalRevision) else "ignored")
            if observed != event["expected"]:
                failures.append("authority_mismatches")
            samples["authority_mismatches"].append(int(observed != event["expected"]))
            if isinstance(value, ProvisionalRevision):
                admitted = False
                try:
                    authoritative_text(value)
                except TranscriptBoundaryError:
                    pass
                else:
                    admitted = True
                    failures.append("provisional_sink_admissions")
                samples["provisional_sink_admissions"].append(int(admitted))
            elif isinstance(value, AuthoritativeUtterance):
                authoritative_text(value)
                final_count += 1
            rows.append({"schema_version": 1, "task_id": "02", "evidence_layer": "local_logic",
                         "run_id": str(run_id), "correlation_id": correlation_id,
                         "fixture_id": case["id"], "event_id": len(rows) + 1,
                         "host_id": host_id, "clock_id": clock_id,
                         "monotonic_ns": time.perf_counter_ns(),
                         "declared_final": event["is_final"], "observed_authority": observed,
                         "capture_id": event["capture_id"],
                         "segment_id": event["segment_id"], "revision": event["revision"]})
        if final_count != case["expected_final_count"]:
            failures.append("final_count_mismatches")
        samples["final_count_mismatches"].append(
            int(final_count != case["expected_final_count"]))
        cases.append({"fixture_id": case["id"], "correlation_id": correlation_id,
                      "status": "failed" if failures else "passed", "final_count": final_count,
                      "failure_codes": sorted(set(failures))})
    metric_results = {}
    for name in sorted(_METRICS):
        rule = profile["invariants"][name]
        metric_results[name] = summarize(samples[name], {
            "direction": "max", "minimum_samples": rule["minimum_samples"],
            "status": "invariant", "statistic": "maximum", "value": rule["maximum"],
        })
    statuses = {entry["status"] for entry in metric_results.values()}
    local_status = "failed" if "failed" in statuses else (
        "passed" if statuses == {"passed"} else "blocked")
    return ({"status": local_status,
             "run_id": str(run_id), "sample_count": len(scenarios), "event_count": len(rows),
             "languages": ["en", "it"],
             "metrics": {name: sum(values) for name, values in samples.items()},
             "metric_summaries": metric_results}, cases, rows)


def run(output: Path, profile_name: str, hardware_path: Path) -> dict[str, Any]:
    contract_path = SPEC / "task02-contract.json"
    profile_path = SPEC / "task02-profile.json"
    contract = read_json(contract_path)
    profiles = read_json(profile_path)
    require(profiles.get("schema_version") == 1 and
            profiles.get("profile_version") == "1.0.0" and
            profiles.get("test_task") == "02", "invalid_profile_version")
    summary, cases, rows = run_local(contract, profiles["profiles"][profile_name])
    hardware = read_json(hardware_path)
    env = environment()
    env.update(clock="time.perf_counter_ns",
               clock_resolution_seconds=time.get_clock_info("perf_counter").resolution)
    # A local authority check cannot establish acoustic recognition or egress,
    # even if a caller supplies a populated hardware manifest.
    reasons = []
    if any(hardware.get("devices", {}).get(key) is None for key in
           ("pc_input", "pc_output", "helios_input", "helios_output")):
        reasons.append("explicit_devices_missing")
    if hardware.get("calibration", {}).get("status") != "calibrated":
        reasons.append("acoustic_calibration_unverified")
    if not all(hardware.get("isolation", {}).get(key) is True for key in
               ("generated_speech_only", "isolated_speakers_or_headphones",
                "monitoring_and_loopback_disabled", "no_people_in_capture_area")):
        reasons.append("capture_isolation_unconfirmed")
    if any(env["dependencies"].get(name) is None for name in
           ("piper-tts", "vosk", "sounddevice", "PyAudio", "onnxruntime")):
        reasons.append("native_dependencies_missing")
    reasons.append("acoustic_authority_path_not_executed")
    report = {"schema_version": 1, "task_id": "02", "run_id": summary["run_id"],
              "status": "failed" if summary["status"] == "failed" else "blocked",
              "local_authority": summary,
              "hil": {"status": "blocked", "required_for_task": True, "reasons": reasons,
                      "devices_opened": 0, "acoustic_exchanges": 0},
              "environment": env, "implementation": implementation_manifest(ROOT),
              "threshold_profile": profile_name, "threshold_version": profiles["profile_version"],
              "thresholds": profiles["profiles"][profile_name]["invariants"],
              "spec_sha256": {"task02-contract.json": sha256(contract_path),
                              "task02-profile.json": sha256(profile_path),
                              "hardware_manifest": sha256(hardware_path)},
              "harness_sha256": sha256(Path(__file__)),
              "cleanup": {"status": "passed", "workers_started": 0,
                          "audio_streams_opened": 0, "audio_files_created": 0,
                          "transcripts_created": 0},
              "acoustic_metrics": {"status": "unverified", "sample_count": 0,
                                   "median": None, "p95": None, "maximum": None,
                                   "wer": None, "cer": None},
              "next_task": "03", "next_task_started": False,
              "artifacts": ["results.json", "cases.jsonl", "events.jsonl"]}
    for name in ("api/transcripts.py", "api/conversation.py", "api/providers/contracts.py",
                 "document/rag_system.py", "recognizer/speech_recognizer.py"):
        report["implementation"]["files"][name] = sha256(ROOT / name)
    directory = artifact_root(output) / summary["run_id"]
    directory.mkdir(exist_ok=False)
    for name, samples in (("cases", cases), ("events", rows)):
        (directory / f"{name}.jsonl").write_text(
            "".join(json.dumps(sample) + "\n" for sample in samples), encoding="utf-8")
    (directory / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--profile", choices=("desktop", "jetson"), default="desktop")
    parser.add_argument("--hardware-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.artifact_dir, args.profile, args.hardware_manifest)
    except (SpecificationError, OSError, ValueError, KeyError, TypeError, AttributeError):
        print(json.dumps({"schema_version": 1, "task_id": "02", "status": "failed",
                          "reason": "task02_validation_failed"}))
        return 1
    print(json.dumps(result, indent=2))
    return 1 if result["status"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
