"""Task 00: validate voice-suite specifications without opening audio or networks.

This is a baseline validator, not an acoustic runner. A successful local result
never changes HIL from blocked. Native synthesis and acoustic execution belong
to subsequent, separately authorized test-suite increments.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import re
import statistics
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "tests" / "voice_suite"
SPEC_FILES = (
    "fixtures.json",
    "metrics.json",
    "thresholds.json",
    "traceability.json",
    "hardware.example.json",
)
DEPENDENCIES = (
    "pytest",
    "numpy",
    "httpx",
    "ruff",
    "piper-tts",
    "vosk",
    "sounddevice",
    "PyAudio",
    "onnxruntime",
)
CATEGORIES = {
    "normal",
    "pause_correction",
    "barge_in",
    "control",
    "task_steering",
    "noise",
    "failure",
}
RECIPE_KINDS = {
    "fake_backend_task",
    "injected_failure",
    "offline_control",
    "overlapping_piper_speech",
    "piper_segments",
    "piper_speech",
    "piper_with_noise",
    "policy_failure",
    "session_sequence",
    "silence",
    "target_tts_echo",
}
REQUIRED_METRICS = {
    "end_to_end_first_audio_ms",
    "final_answer_ms",
    "stt_ms",
    "model_ttft_ms",
    "model_generation_ms",
    "tts_first_audio_ms",
    "tts_synthesis_ms",
    "interruption_ms",
    "endpointing_ms",
    "premature_finalization_rate",
    "wer",
    "cer",
    "exact_final_rate",
    "duplicate_dispatch_rate",
    "dropped_turn_rate",
    "partial_only_turn_rate",
    "false_barge_in_rate",
    "missed_barge_in_rate",
    "response_completeness_rate",
    "unexpected_overlap_rate",
    "task_transition_ms",
    "verified_completion_precision",
    "verified_completion_recall",
    "cpu_percent",
    "ram_mib",
    "temperature_c",
    "power_w",
    "queue_depth",
    "leaked_workers",
    "cleanup_ms",
    "unauthorized_egress_count",
}
REQUIRED_EVENTS = {
    "stimulus_synthesis_start",
    "stimulus_ready",
    "stimulus_playback_start",
    "stimulus_playback_end",
    "pc_microphone_capture_start",
    "pc_microphone_capture_end",
    "first_provisional_recognition",
    "authoritative_final_recognition",
    "model_dispatch",
    "model_start",
    "model_first_token",
    "model_completion",
    "tts_synthesis_start",
    "tts_synthesis_end",
    "helios_audio_playback_start",
    "pc_first_response_energy",
    "response_capture_end",
    "interruption_confirmed",
    "task_backend_acknowledgement",
}
TASKS = {f"{n:02d}" for n in range(17)}
IMPLEMENTATION = (
    "config.py",
    "assistant.py",
    "audio/tts.py",
    "audio/speech_pipeline.py",
    "audio/backchannel.py",
    "recognizer/speech_recognizer.py",
    "recognizer/turn_endpoint_detector.py",
    "recognizer/barge_in_detector.py",
    "recognizer/echo_suppression_policy.py",
    "api/realtime_conversation.py",
    "api/control_intents.py",
    "api/transcripts.py",
    "api/api_client.py",
    "api/streaming.py",
    "api/metrics.py",
    "observability/resources.py",
)


class SpecificationError(ValueError):
    """Only fixed content-free error codes may escape the validator."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise SpecificationError(code)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: require(False, "nonfinite_json"),
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise SpecificationError("unreadable_json") from None
    require(isinstance(data, dict), "object_required")
    require(
        type(data.get("schema_version")) is int and data["schema_version"] == 1,
        "unsupported_schema",
    )
    return data


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def repo_path(root: Path, relative: str) -> Path:
    require(isinstance(relative, str) and bool(relative), "invalid_repository_path")
    require(not Path(relative).is_absolute(), "invalid_repository_path")
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()), "repository_path_escape")
    require(path.is_file(), "repository_file_missing")
    return path


def records(data: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    rows = data.get(key)
    require(isinstance(rows, list) and bool(rows), "records_required")
    result = {}
    for row in rows:
        require(isinstance(row, dict), "record_object_required")
        identifier = row.get("id")
        require(
            isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_-]+", identifier) is not None,
            "invalid_record_id",
        )
        require(identifier not in result, "duplicate_record_id")
        result[identifier] = row
    return result


def finite_number(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate_specs(bundle: dict[str, dict[str, Any]], root: Path = ROOT) -> dict[str, int]:
    """Fail closed on malformed specs; do not import the application or native runtimes."""
    try:
        return _validate_specs(bundle, root)
    except (KeyError, TypeError, AttributeError, IndexError):
        raise SpecificationError("malformed_specification") from None


def _validate_specs(bundle: dict[str, dict[str, Any]], root: Path) -> dict[str, int]:
    catalog = bundle["fixtures.json"]
    fixtures = records(catalog, "fixtures")
    metrics = records(bundle["metrics.json"], "metrics")
    require(REQUIRED_METRICS <= metrics.keys(), "required_metric_missing")
    trace = records(bundle["traceability.json"], "requirements")
    thresholds = bundle["thresholds.json"]
    require(
        catalog["synthesis"]["entrypoint"] == "audio.tts.PiperTTS.synthesize_wave",
        "wrong_synthesis_path",
    )
    require(
        catalog["catalog_version"] == "1.0.0" and thresholds["profile_version"] == "1.0.0",
        "unsupported_specification_version",
    )
    require(set(catalog["voices"]) == {"it", "en"}, "voice_languages")
    for voice in catalog["voices"].values():
        repo_path(root, voice["model"])
        config = json.loads(repo_path(root, voice["config"]).read_text(encoding="utf-8"))
        require(
            type(voice["sample_rate_hz"]) is int
            and voice["sample_rate_hz"] > 0
            and voice["sample_rate_hz"] == config["audio"]["sample_rate"],
            "voice_sample_rate",
        )
    covered = set()
    for row in fixtures.values():
        require(row["language"] in catalog["voices"], "fixture_language")
        require(row["category"] in CATEGORIES, "fixture_category")
        covered.add((row["language"], row["category"]))
        require(row["privacy"] == "synthetic_public", "fixture_privacy")
        require(row["execution"] in {"local_only", "remote_allowed"}, "fixture_execution")
        for field in ("text", "expected_authoritative_transcript", "expected_intent"):
            require(isinstance(row[field], str), "fixture_text_type")
        require(bool(row["expected_intent"]), "fixture_intent")
        require(
            isinstance(row["response_constraints"], list)
            and bool(row["response_constraints"])
            and all(isinstance(v, str) and bool(v) for v in row["response_constraints"]),
            "fixture_response_constraints",
        )
        require(
            isinstance(row["metric_ids"], list)
            and bool(row["metric_ids"])
            and set(row["metric_ids"]) <= metrics.keys(),
            "fixture_unknown_metric",
        )
        require(
            isinstance(row["test_tasks"], list)
            and bool(row["test_tasks"])
            and set(row["test_tasks"]) <= TASKS,
            "fixture_unknown_task",
        )
        require(
            isinstance(row["recipe"], dict) and row["recipe"].get("kind") in RECIPE_KINDS,
            "fixture_recipe",
        )
    require(
        covered == {(lang, category) for lang in ("it", "en") for category in CATEGORIES},
        "fixture_category_coverage",
    )
    events = records(bundle["metrics.json"], "events")
    require(REQUIRED_EVENTS <= events.keys(), "required_event_missing")
    for event in events.values():
        require(
            event["clock_domain"] in {"pc", "target"} and bool(event["applicability"]),
            "event_clock_domain",
        )
    for metric in metrics.values():
        for field in ("definition", "unit", "artifact_source", "sample_unit"):
            require(isinstance(metric[field], str) and bool(metric[field]), "metric_definition")
        require(metric["aggregation"] in {"distribution", "rate", "count"}, "metric_aggregation")
        require(metric["direction"] in {"max", "min"}, "metric_direction")
        require(type(metric["mandatory"]) is bool, "metric_mandatory")
    require(set(thresholds["profiles"]) == {"desktop", "jetson"}, "threshold_profiles")
    for profile in thresholds["profiles"].values():
        require(set(profile["metrics"]) == metrics.keys(), "threshold_metric_coverage")
        for key, limit in profile["metrics"].items():
            require(limit["unit"] == metrics[key]["unit"], "threshold_units")
            require(limit["direction"] == metrics[key]["direction"], "threshold_direction")
            require(
                type(limit["minimum_samples"]) is int and limit["minimum_samples"] > 0,
                "threshold_samples",
            )
            require(
                limit["status"] in {"invariant", "provisional", "uncalibrated", "calibrated"},
                "threshold_status",
            )
            require(
                limit["statistic"] in {"p95", "maximum", "minimum", "rate", "count"},
                "threshold_statistic",
            )
            if limit["status"] == "uncalibrated":
                require(limit["value"] is None, "uncalibrated_threshold_value")
            else:
                require(
                    finite_number(limit["value"]) and limit["value"] >= 0, "invalid_threshold_value"
                )
            if limit["status"] == "calibrated":
                require(
                    bool(profile.get("calibration_artifact_sha256"))
                    and re.fullmatch(r"[0-9a-f]{64}", profile["calibration_artifact_sha256"])
                    is not None,
                    "missing_calibration_evidence",
                )
    require(set(trace) == {f"FR-{i:02d}" for i in range(1, 46)}, "requirement_coverage")
    source_rows = {}
    for source in {row["source"] for row in trace.values()}:
        lines = repo_path(root, source).read_text(encoding="utf-8").splitlines()
        source_rows[source] = [line for line in lines if line.startswith("| FR-")]
    require(len(source_rows) == 1, "requirement_source_count")
    canonical_rows = next(iter(source_rows.values()))
    source_digest = hashlib.sha256(("\n".join(canonical_rows) + "\n").encode()).hexdigest()
    require(
        source_digest == bundle["traceability.json"]["requirement_source_sha256"],
        "requirement_source_changed",
    )
    for row in trace.values():
        require(
            isinstance(row["requirement"], str) and bool(row["requirement"]), "requirement_text"
        )
        matching = [line for line in canonical_rows if line.startswith("| " + row["id"] + " |")]
        require(
            len(matching) == 1 and matching[0].split("|")[3].strip() == row["requirement"],
            "requirement_text_mismatch",
        )
        require(
            isinstance(row["test_tasks"], list)
            and bool(row["test_tasks"])
            and set(row["test_tasks"]) <= TASKS,
            "requirement_unknown_task",
        )
        require(
            row["local_evidence"] == "not_run" and row["hil_evidence"] == "blocked",
            "baseline_cannot_verify_requirements",
        )
    hardware = bundle["hardware.example.json"]
    require(
        set(hardware["devices"]) == {"pc_input", "pc_output", "helios_input", "helios_output"},
        "hardware_roles",
    )
    require(
        all(value is None for value in hardware["devices"].values()),
        "example_must_not_select_devices",
    )
    require(
        hardware["status"] == "unconfigured" and hardware["calibration"]["status"] == "unverified",
        "example_must_not_claim_calibration",
    )
    require(hardware["artifacts"]["audio_retention"] in {"delete", "keep"}, "retention_policy")
    require(
        set(hardware["limits"])
        == {
            "exchange_timeout_seconds",
            "session_timeout_seconds",
            "shutdown_timeout_seconds",
            "maximum_self_triggers",
        },
        "hardware_missing_limits",
    )
    for value in hardware["limits"].values():
        require(finite_number(value) and value > 0, "hardware_unbounded_limit")
    return {
        "fixtures": len(fixtures),
        "metrics": len(metrics),
        "requirements": len(trace),
        "timestamp_events": len(events),
    }


def summarize(
    samples: list[float], limit: dict[str, Any], *, denominators: list[int] | None = None
) -> dict[str, Any]:
    """R-7 p95; retain outliers and never treat missing/calibration data as a pass.

    Rate inputs are per-case numerators with explicit denominators for WER/CER.
    Binary rates default to one opportunity per input. Signed distributions are
    allowed (e.g. temperature); future timing readers must reject invalid event
    ordering before calling this helper.
    """
    require(all(finite_number(v) for v in samples), "invalid_metric_sample")
    require(limit["direction"] in {"min", "max"}, "threshold_direction")
    require(
        type(limit["minimum_samples"]) is int and limit["minimum_samples"] > 0, "threshold_samples"
    )
    require(
        limit["status"] in {"calibrated", "invariant", "provisional", "uncalibrated"},
        "threshold_status",
    )
    statistic = limit["statistic"]
    require(statistic in {"p95", "maximum", "minimum", "rate", "count"}, "threshold_statistic")
    value = limit["value"]
    require(
        (value is None and limit["status"] == "uncalibrated")
        or (finite_number(value) and value >= 0 and limit["status"] != "uncalibrated"),
        "invalid_threshold_value",
    )
    numerator = denominator = None
    values = samples
    if statistic in {"rate", "count"}:
        require(all(v >= 0 and int(v) == v for v in samples), "invalid_count_sample")
    if statistic == "rate":
        if denominators is None:
            require(
                not samples or not str(limit.get("unit", "ratio")).startswith("edits/"),
                "rate_denominators_required",
            )
            denominators = [1] * len(samples)
        require(
            len(denominators) == len(samples)
            and all(type(v) is int and v > 0 and finite_number(v) for v in denominators),
            "invalid_rate_denominator",
        )
        if limit.get("unit", "ratio") == "ratio":
            require(all(n <= d for n, d in zip(samples, denominators)), "invalid_rate_numerator")
        numerator, denominator = sum(samples), sum(denominators)
        values = [n / d for n, d in zip(samples, denominators)]
    ordered = sorted(values)
    failures = None
    if value is not None:
        require(finite_number(value), "invalid_threshold_value")
        failures = sum(v > value if limit["direction"] == "max" else v < value for v in values)
    result = {
        "sample_count": len(samples),
        "median": None,
        "p95": None,
        "maximum": None,
        "minimum": None,
        "failure_count": failures,
        "status": "unverified",
    }
    if statistic == "rate":
        result.update(numerator=numerator, denominator=denominator, value=None)
    elif statistic == "count":
        result["value"] = None
    if ordered:
        position = (len(ordered) - 1) * 0.95
        lower = math.floor(position)
        upper = math.ceil(position)
        result.update(
            median=statistics.median(ordered),
            maximum=ordered[-1],
            minimum=ordered[0],
            p95=ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower),
        )
        aggregate = (
            numerator / denominator
            if statistic == "rate"
            else sum(ordered)
            if statistic == "count"
            else result[statistic]
        )
        require(
            all(finite_number(result[key]) for key in ("median", "maximum", "minimum", "p95"))
            and finite_number(aggregate),
            "nonfinite_metric_aggregate",
        )
        if statistic in {"rate", "count"}:
            result["value"] = aggregate
        # Even one observed hard breach is a failure, regardless of sample/calibration gaps.
        breached = value is not None and (
            aggregate > value if limit["direction"] == "max" else aggregate < value
        )
        if breached:
            result["status"] = "failed"
        elif len(samples) >= limit["minimum_samples"] and limit["status"] in {
            "calibrated",
            "invariant",
        }:
            result["status"] = "passed"
    return result


def environment() -> dict[str, Any]:
    versions = {}
    for package in DEPENDENCIES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {
        "python": platform.python_version(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "dependencies": versions,
        "device_versions": None,
        "clock": "time.monotonic_ns",
        "clock_resolution_seconds": time.get_clock_info("monotonic").resolution,
    }


def implementation_manifest(root: Path) -> dict[str, Any]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        require(re.fullmatch(r"[0-9a-f]{40,64}", head) is not None, "invalid_git_head")
    except (OSError, subprocess.SubprocessError):
        head = None
    return {
        "commit": head,
        "identity": "commit_plus_file_hashes",
        "files": {name: sha256(root / name) for name in IMPLEMENTATION if (root / name).is_file()},
    }


def artifact_root(path: Path, root: Path = ROOT) -> Path:
    """Resolve links before enforcing the ignored local directory boundary."""
    destination = path.resolve()
    repository = root.resolve()
    if destination.is_relative_to(repository):
        allowed = repository / ".voice-test-artifacts"
        require(
            allowed.resolve() == allowed and destination.is_relative_to(allowed),
            "artifact_directory_must_be_temporary",
        )
    require(destination != repository, "artifact_directory_must_be_temporary")
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def run_baseline(
    spec: Path, output: Path, profile: str, require_hil: bool = False
) -> dict[str, Any]:
    started = time.monotonic_ns()
    correlation_id = str(uuid.uuid4())
    folder = artifact_root(output) / correlation_id
    folder.mkdir(exist_ok=False)
    env = environment()
    bundle = {name: read_json(spec / name) for name in SPEC_FILES}
    counts = validate_specs(bundle)
    thresholds = bundle["thresholds.json"]
    require(profile in thresholds["profiles"], "unknown_profile")
    metrics = records(bundle["metrics.json"], "metrics")
    rows = []
    for key, metric in metrics.items():
        rows.append(
            {
                "correlation_id": correlation_id,
                "task_id": "00",
                "metric_id": key,
                "profile_id": profile,
                "threshold_version": thresholds["profile_version"],
                "eligible_count": 0,
                "missing_count": 0,
                "reason": "baseline_not_measured",
                "unit": metric["unit"],
                "artifact_source": metric["artifact_source"],
                "raw_samples": [],
                "threshold": thresholds["profiles"][profile]["metrics"][key],
                **summarize([], thresholds["profiles"][profile]["metrics"][key]),
            }
        )
    voices = {}
    for language, voice in bundle["fixtures.json"]["voices"].items():
        voices[language] = {
            "model": voice["model"],
            "model_sha256": sha256(ROOT / voice["model"]),
            "config_sha256": sha256(ROOT / voice["config"]),
            "sample_rate_hz": voice["sample_rate_hz"],
            "piper_version": env["dependencies"]["piper-tts"],
            "audio_generated": False,
        }
    blockers = [
        "explicit_pc_and_helios_device_ids_missing",
        "acoustic_calibration_missing",
        "capture_isolation_unconfirmed",
        "acoustic_runner_not_implemented_in_task00",
    ]
    missing = [
        p for p in ("piper-tts", "vosk", "sounddevice", "PyAudio") if env["dependencies"][p] is None
    ]
    if missing:
        blockers.append("pc_native_dependencies_missing")
    report = {
        "schema_version": 1,
        "task_id": "00",
        "correlation_id": correlation_id,
        "status": "blocked" if require_hil else "passed",
        "local_baseline": "passed",
        "hil": {
            "status": "blocked",
            "reasons": blockers,
            "missing_packages": missing,
            "exchanges": 0,
            "devices_opened": 0,
        },
        "counts": counts,
        "languages": ["en", "it"],
        "environment": env,
        "implementation": implementation_manifest(ROOT),
        "spec_sha256": {name: sha256(spec / name) for name in SPEC_FILES},
        "harness_sha256": sha256(Path(__file__)),
        "voices": voices,
        "threshold_profile": profile,
        "threshold_version": thresholds["profile_version"],
        "validation_duration_ms": (time.monotonic_ns() - started) / 1_000_000,
        "cleanup": {
            "status": "passed",
            "workers_started": 0,
            "streams_opened": 0,
            "audio_files_created": 0,
            "transcripts_created": 0,
        },
        "artifacts": ["results.json", "metrics.jsonl"],
        "next_task": "01",
    }
    (folder / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (folder / "metrics.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--profile", choices=("desktop", "jetson"), default="desktop")
    parser.add_argument(
        "--require-hil",
        action="store_true",
        help="exit 2 for blocked HIL; this command never opens audio",
    )
    args = parser.parse_args(argv)
    try:
        report = run_baseline(SPEC, args.artifact_dir, args.profile, args.require_hil)
    except (SpecificationError, OSError, ValueError):
        # No arbitrary paths, exception strings, environment or fixture text in failures.
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": "00",
                    "status": "failed",
                    "reason": "baseline_validation_failed",
                }
            )
        )
        return 1
    print(json.dumps(report, indent=2))
    return 2 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
