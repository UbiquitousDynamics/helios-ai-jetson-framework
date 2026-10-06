"""Offline review of operator evidence. A verdict never enables automation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from automation.contracts import object_json

STAGES = (
    "home_assistant_discovery",
    "live_reads",
    "voice_confirmation",
    "voice_writes",
    "reboot_persistence",
)
SCENARIOS = (
    "false_wake",
    "wrong_target",
    "playback_echo",
    "missed_denial",
    "interruption_before_dispatch",
    "interruption_after_dispatch",
    "remote_failure_fallback",
)


@dataclass(frozen=True, slots=True)
class GateVerdict:
    stage: str
    status: str
    reason_code: str


def _count(value, *, positive=False):
    return type(value) is int and value >= (1 if positive else 0)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _read_bounded(path):
    with path.open("rb") as handle:
        payload = handle.read(65537)
    if len(payload) > 65536:
        raise ValueError("Evidence too large")
    return payload


def _acoustic_evidence(manifest):
    hardware = manifest.get("hardware_results", {})
    if not isinstance(hardware, dict):
        return False
    if (
        hardware.get("calibration_verified") is not True
        or hardware.get("thresholds_predeclared") is not True
        or not re.fullmatch(r"[a-f0-9]{64}", str(hardware.get("calibration_artifact_sha256", "")))
        or not isinstance(hardware.get("target_profile"), str)
        or not hardware["target_profile"].strip()
        or not _count(hardware.get("samples"), positive=True)
        or type(hardware.get("failures")) is not int
        or hardware["failures"] != 0
    ):
        return False
    metrics = [hardware.get(key) for key in ("median_ms", "p95_ms", "max_ms")]
    limits = hardware.get("thresholds")
    if not all(_number(value) for value in metrics) or not isinstance(limits, dict):
        return False
    if not all(_number(limits.get(key)) for key in ("p95_ms", "max_ms")):
        return False
    median, p95, maximum = metrics
    if not median <= p95 <= maximum or p95 > limits["p95_ms"] or maximum > limits["max_ms"]:
        return False
    scenarios = hardware.get("scenarios", {})
    if not isinstance(scenarios, dict):
        return False
    samples = 0
    for name in SCENARIOS:
        result = scenarios.get(name)
        if (
            not isinstance(result, dict)
            or not _count(result.get("samples"), positive=True)
            or type(result.get("failures")) is not int
            or result["failures"] != 0
        ):
            return False
        samples += result["samples"]
    return samples <= hardware["samples"]


def check_rollout(path: Path, stage: str) -> GateVerdict:
    """Inspect bounded local evidence; never connect, record audio, or dispatch."""

    def verdict(reason, *, passed=False):
        return GateVerdict(stage, "passed" if passed else "blocked", reason)

    if stage not in STAGES:
        return verdict("unknown_stage")
    try:
        path = Path(path)
        manifest = json.loads(object_json(_read_bounded(path).decode("utf-8")))
        if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
            return verdict("invalid_manifest")
        if manifest.get("voice_writes_enabled") is not False:
            return verdict("activation_not_supported")
        stages = manifest["stages"]
        if stages["model_free"]["status"] not in {
            "passed",
            "passed_windows_python_3_12",
            "passed_linux_windows_python_3_10_3_12",
        }:
            return verdict("model_free_evidence_missing")
        for name in STAGES[: STAGES.index(stage) + 1]:
            record = stages.get(name, {})
            if record.get("status") != "passed":
                return verdict(name + "_evidence_missing")
            if name == "home_assistant_discovery":
                reference = Path(record["evidence_file"])
                if reference.is_absolute() or ".." in reference.parts:
                    return verdict("discovery_evidence_invalid")
                root = path.parent.resolve()
                evidence_path = (root / reference).resolve()
                if not evidence_path.is_relative_to(root):
                    return verdict("discovery_evidence_invalid")
                payload = _read_bounded(evidence_path)
                if len(payload) > 65536 or hashlib.sha256(payload).hexdigest() != record.get(
                    "evidence_sha256"
                ):
                    return verdict("discovery_evidence_invalid")
                evidence = json.loads(object_json(payload.decode("utf-8")))
                if (
                    evidence.get("status") != "discovery_only"
                    or not re.fullmatch(
                        r"20\d{2}\.\d{1,2}\.\d+", str(evidence.get("core_version", ""))
                    )
                    or not _count(evidence.get("tool_count"), positive=True)
                    or type(evidence.get("scoped_tool_count")) is not int
                    or evidence["scoped_tool_count"] != 0
                    or evidence.get("safety", {}).get("tools_call_performed") is not False
                ):
                    return verdict("discovery_evidence_invalid")
            elif name == "live_reads":
                if not all(
                    record.get(key) is True
                    for key in (
                        "exact_target_mapping_verified",
                        "exposure_verified",
                        "permission_verified",
                    )
                ) or not _count(record.get("successful_read_count"), positive=True):
                    return verdict("live_reads_scope_unverified")
            elif name == "voice_confirmation":
                if not _acoustic_evidence(manifest):
                    return verdict("acoustic_evidence_missing_or_failed")
            elif name == "voice_writes":
                if record.get("exact_low_risk_target_verified") is not True or not _count(
                    record.get("successful_test_write_count"), positive=True
                ):
                    return verdict("test_write_evidence_missing")
            elif not all(
                record.get(key) is True
                for key in (
                    "runtime_assembly_verified",
                    "reboot_verified",
                    "capture_verified",
                    "rollback_verified",
                )
            ):
                return verdict("deployment_evidence_missing")
        return verdict("required_evidence_present", passed=True)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
        OverflowError,
    ):
        return verdict("invalid_manifest")
