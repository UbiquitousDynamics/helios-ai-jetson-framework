"""Task 02 local evidence runner checks; no audio device is opened here."""

from __future__ import annotations

import copy
import json

import pytest

from scripts import voice_suite_task02 as task


pytestmark = pytest.mark.voice_local


@pytest.fixture
def specifications():
    contract = task.read_json(task.SPEC / "task02-contract.json")
    profile = task.read_json(task.SPEC / "task02-profile.json")["profiles"]["desktop"]
    return contract, profile


def test_local_runner_passes_real_authority_boundary_with_per_metric_samples(specifications):
    summary, cases, events = task.run_local(*specifications)
    assert summary["status"] == "passed"
    assert summary["sample_count"] == len(cases) == 10
    assert summary["event_count"] == len(events)
    assert summary["languages"] == ["en", "it"]
    assert set(summary["metrics"]) == {
        "authority_mismatches",
        "final_count_mismatches",
        "provisional_sink_admissions",
    }
    assert set(summary["metrics"].values()) == {0}
    assert all(case["status"] == "passed" for case in cases)
    assert {row["observed_authority"] for row in events} == {"provisional", "final", "ignored"}
    assert {row["correlation_id"] for row in events} == {case["correlation_id"] for case in cases}

    samples = summary["metric_summaries"]
    assert samples["authority_mismatches"]["sample_count"] == len(events)
    assert samples["final_count_mismatches"]["sample_count"] == len(cases)
    assert samples["provisional_sink_admissions"]["sample_count"] == sum(
        row["observed_authority"] == "provisional" for row in events
    )
    assert all(item["status"] == "passed" and item["maximum"] == 0 for item in samples.values())


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("contract_version", "invalid_contract_version"),
        ("missing_identity", "missing_segment_identity"),
        ("duplicate_case", "invalid_scenario_id"),
        ("relaxed_limit", "logical_invariant_cannot_be_relaxed"),
        ("reduced_sample_requirement", "logical_invariant_cannot_be_relaxed"),
        ("missing_language", "language_coverage"),
    ],
)
def test_tampered_contract_or_profile_fails_closed(specifications, mutation, code):
    contract, profile = copy.deepcopy(specifications)
    if mutation == "contract_version":
        contract["contract_version"] = "9.9.9"
    elif mutation == "missing_identity":
        del contract["scenarios"][0]["events"][0]["capture_id"]
    elif mutation == "duplicate_case":
        contract["scenarios"][1]["id"] = contract["scenarios"][0]["id"]
    elif mutation == "relaxed_limit":
        profile["invariants"]["provisional_sink_admissions"]["maximum"] = 1
    elif mutation == "reduced_sample_requirement":
        profile["invariants"]["provisional_sink_admissions"]["minimum_samples"] = 9
    elif mutation == "missing_language":
        for case in contract["scenarios"]:
            case["language"] = "en"
    with pytest.raises(task.SpecificationError, match=f"^{code}$"):
        task.run_local(contract, profile)


def test_insufficient_provisional_boundary_samples_cannot_pass(specifications):
    contract, profile = copy.deepcopy(specifications)
    # Keep ten scenarios, while reducing actual provisional boundary attempts.
    case = next(case for case in contract["scenarios"] if case["id"] == "provisional_only_timeout")
    case["events"] = case["events"][:1]
    with pytest.raises(task.SpecificationError, match="^contract_coverage_missing$"):
        task.run_local(contract, profile)


def test_contract_cannot_replace_final_coverage_with_provisional_only_cases(specifications):
    contract, profile = copy.deepcopy(specifications)
    for case in contract["scenarios"]:
        case["expected_final_count"] = 0
        case["events"] = [
            {
                "text": "Emilia synthetic unfinished request",
                "is_final": False,
                "capture_id": 1,
                "segment_id": 1,
                "revision": 1,
                "expected": "provisional",
            }
        ]
    with pytest.raises(task.SpecificationError):
        task.run_local(contract, profile)


def test_cli_emits_content_free_artifacts_and_never_promotes_hil(tmp_path, capsys):
    contract = task.read_json(task.SPEC / "task02-contract.json")
    hardware = task.SPEC / "hardware.example.json"
    assert task.main(["--artifact-dir", str(tmp_path), "--hardware-manifest", str(hardware)]) == 2
    printed = capsys.readouterr().out
    report = json.loads(printed)
    assert report["status"] == "blocked"
    assert report["local_authority"]["status"] == "passed"
    assert report["hil"]["status"] == "blocked"
    assert report["hil"]["required_for_task"] is True
    assert report["hil"]["acoustic_exchanges"] == 0
    assert report["hil"]["devices_opened"] == 0
    assert report["acoustic_metrics"]["sample_count"] == 0
    assert report["next_task"] == "03" and report["next_task_started"] is False
    assert report["cleanup"]["audio_files_created"] == 0

    folder = tmp_path / report["run_id"]
    assert {path.name for path in folder.iterdir()} == {
        "results.json",
        "cases.jsonl",
        "events.jsonl",
    }
    serialized = printed + "".join(path.read_text(encoding="utf-8") for path in folder.iterdir())
    assert all(
        event["text"] not in serialized
        for case in contract["scenarios"]
        for event in case["events"]
        if event["text"].strip()
    )
    rows = [
        json.loads(line)
        for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == report["local_authority"]["event_count"]
    assert all("text" not in row and "transcript" not in row for row in rows)


def test_populated_manifest_alone_cannot_claim_acoustic_pass(tmp_path):
    hardware = task.read_json(task.SPEC / "hardware.example.json")
    # Test-only placeholders exercise the guard, never a real device selection.
    hardware["devices"] = {role: {"test_only": True} for role in hardware["devices"]}
    hardware["calibration"]["status"] = "calibrated"
    hardware["isolation"] = {key: True for key in hardware["isolation"]}
    manifest = tmp_path / "synthetic-hardware.json"
    manifest.write_text(json.dumps(hardware), encoding="utf-8")

    report = task.run(tmp_path / "artifacts", "desktop", manifest)
    assert report["local_authority"]["status"] == "passed"
    assert report["status"] == report["hil"]["status"] == "blocked"
    assert "acoustic_authority_path_not_executed" in report["hil"]["reasons"]
    assert report["hil"]["acoustic_exchanges"] == 0


def test_runner_rejects_artifact_root_inside_source_tree(tmp_path):
    destination = task.ROOT / "tests" / "voice_suite" / "forbidden-task02-output"
    assert not destination.exists()
    with pytest.raises(task.SpecificationError, match="^artifact_directory_must_be_temporary$"):
        task.run(destination, "desktop", task.SPEC / "hardware.example.json")
    assert not destination.exists()


def test_invalid_contract_cli_response_is_sanitized(tmp_path, monkeypatch, capsys):
    contract = task.read_json(task.SPEC / "task02-contract.json")
    profile = task.read_json(task.SPEC / "task02-profile.json")
    contract["scenarios"][0]["events"][0]["text"] = "SECRET_TRANSCRIPT_CANARY"
    del contract["scenarios"][0]["events"][0]["capture_id"]
    (tmp_path / "task02-contract.json").write_text(json.dumps(contract), encoding="utf-8")
    (tmp_path / "task02-profile.json").write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(task, "SPEC", tmp_path)

    output = tmp_path / "output"
    assert (
        task.main(
            [
                "--artifact-dir",
                str(output),
                "--hardware-manifest",
                str(task.ROOT / "tests" / "voice_suite" / "hardware.example.json"),
            ]
        )
        == 1
    )
    printed = capsys.readouterr().out
    assert json.loads(printed) == {
        "schema_version": 1,
        "task_id": "02",
        "status": "failed",
        "reason": "task02_validation_failed",
    }
    assert "SECRET_TRANSCRIPT_CANARY" not in printed
    assert not output.exists()
