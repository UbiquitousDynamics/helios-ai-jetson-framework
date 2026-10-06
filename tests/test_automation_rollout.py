import hashlib
import json
from pathlib import Path

import pytest

from automation.rollout import SCENARIOS, check_rollout
from scripts.mcp_rollout_check import main

ROOT = Path(__file__).resolve().parents[1]


def evidence(tmp_path):
    source = ROOT / "docs/mcp/home-assistant-2025.12.3-discovery.json"
    snapshot = tmp_path / "discovery.json"
    snapshot.write_bytes(source.read_bytes())
    manifest = {
        "schema_version": 1,
        "voice_writes_enabled": False,
        "stages": {
            "model_free": {"status": "passed"},
            "home_assistant_discovery": {
                "status": "passed",
                "evidence_file": snapshot.name,
                "evidence_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            },
            "live_reads": {
                "status": "passed",
                "exact_target_mapping_verified": True,
                "exposure_verified": True,
                "permission_verified": True,
                "successful_read_count": 1,
            },
            "voice_confirmation": {"status": "passed"},
            "voice_writes": {
                "status": "passed",
                "exact_low_risk_target_verified": True,
                "successful_test_write_count": 1,
            },
            "reboot_persistence": {
                "status": "passed",
                "runtime_assembly_verified": True,
                "reboot_verified": True,
                "capture_verified": True,
                "rollback_verified": True,
            },
        },
        # Fictional operator evidence exercises review; it never certifies hardware.
        "hardware_results": {
            "target_profile": "generated-fixture",
            "calibration_verified": True,
            "calibration_artifact_sha256": "a" * 64,
            "thresholds_predeclared": True,
            "samples": len(SCENARIOS),
            "failures": 0,
            "median_ms": 10,
            "p95_ms": 20,
            "max_ms": 30,
            "thresholds": {"p95_ms": 25, "max_ms": 35},
            "scenarios": {name: {"samples": 1, "failures": 0} for name in SCENARIOS},
        },
    }
    return manifest


def review(tmp_path, manifest, stage="reboot_persistence"):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return check_rollout(path, stage)


def test_operator_evidence_never_enables_automation(tmp_path):
    manifest = evidence(tmp_path)
    assert review(tmp_path, manifest).status == "passed"
    assert manifest["voice_writes_enabled"] is False
    manifest["voice_writes_enabled"] = True
    assert review(tmp_path, manifest).reason_code == "activation_not_supported"


@pytest.mark.parametrize(
    "change",
    [
        {"samples": 0},
        {"samples": True},
        {"failures": 1},
        {"calibration_verified": False},
        {"thresholds_predeclared": False},
        {"p95_ms": 40},
        {"max_ms": 15},
        {"p95_ms": float("nan")},
        {"scenarios": {}},
    ],
)
def test_claimed_pass_without_safe_acoustic_evidence_is_blocked(tmp_path, change):
    manifest = evidence(tmp_path)
    manifest["hardware_results"].update(change)
    assert review(tmp_path, manifest).status == "blocked"


def test_echo_failure_and_missing_scope_cannot_be_bypassed(tmp_path):
    manifest = evidence(tmp_path)
    manifest["hardware_results"]["scenarios"]["playback_echo"]["failures"] = 1
    assert review(tmp_path, manifest).reason_code == "acoustic_evidence_missing_or_failed"
    manifest["stages"]["live_reads"]["exposure_verified"] = False
    assert review(tmp_path, manifest).reason_code == "live_reads_scope_unverified"


def test_tampered_or_external_discovery_evidence_is_blocked(tmp_path):
    manifest = evidence(tmp_path)
    (tmp_path / "discovery.json").write_text("{}")
    assert review(tmp_path, manifest, "home_assistant_discovery").status == "blocked"
    manifest["stages"]["home_assistant_discovery"]["evidence_file"] = "../outside.json"
    assert review(tmp_path, manifest).reason_code == "discovery_evidence_invalid"


def test_bundled_record_only_passes_discovery_and_cli_is_content_free(capsys):
    path = ROOT / "docs/mcp/rollout-status.json"
    assert check_rollout(path, "home_assistant_discovery").status == "passed"
    assert main(["--manifest", str(path), "--stage", "voice_writes"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "stage": "voice_writes",
        "status": "blocked",
        "reason_code": "live_reads_evidence_missing",
    }
