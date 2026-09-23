"""Deterministic Task 00 tests. No physical audio or speech recognition claims."""

from __future__ import annotations

import copy
import json

import pytest
import config

from scripts import voice_test_suite as suite

pytestmark = pytest.mark.voice_local


@pytest.fixture
def bundle():
    return {name: suite.read_json(suite.SPEC / name) for name in suite.SPEC_FILES}


def test_catalog_traceability_profiles_and_models_agree(bundle):
    counts = suite.validate_specs(bundle)
    assert counts["requirements"] == 45
    assert counts["fixtures"] >= 28
    assert counts["metrics"] >= 31
    assert counts["timestamp_events"] >= 18
    for language, profile in config._profile_paths(suite.ROOT).items():
        assert suite.ROOT / bundle["fixtures.json"]["voices"][language]["model"] == profile.tts_model


def test_all_seven_controls_exist_in_both_languages(bundle):
    controls = {"STOP_SPEAKING", "MUTE", "UNMUTE", "SUSPEND_SESSION", "RESUME_SESSION",
                "END_SESSION", "CANCEL_TASK"}
    for language in ("it", "en"):
        found = {row["expected_intent"] for row in bundle["fixtures.json"]["fixtures"]
                 if row["language"] == language and row["category"] == "control"}
        assert controls <= found


@pytest.mark.parametrize("mutation,reason", [
    ("duplicate_fixture", "duplicate_record_id"),
    ("missing_requirement", "requirement_coverage"),
    ("unknown_metric", "fixture_unknown_metric"),
    ("unknown_task", "fixture_unknown_task"),
    ("real_speech", "fixture_privacy"),
    ("wrong_rate", "voice_sample_rate"),
    ("arbitrary_audio", "wrong_synthesis_path"),
    ("no_category", "fixture_category_coverage"),
    ("wrong_units", "threshold_units"),
    ("missing_limit", "threshold_metric_coverage"),
    ("nan_limit", "invalid_threshold_value"),
    ("zero_samples", "threshold_samples"),
    ("fake_calibration", "missing_calibration_evidence"),
    ("fake_verified", "baseline_cannot_verify_requirements"),
    ("changed_source", "requirement_source_changed"),
    ("changed_requirement", "requirement_text_mismatch"),
    ("path_escape", "repository_path_escape"),
    ("default_device", "example_must_not_select_devices"),
    ("unbounded_timeout", "hardware_unbounded_limit"),
    ("missing_timeouts", "hardware_missing_limits"),
    ("invalid_recipe", "fixture_recipe"),
    ("missing_event", "required_event_missing"),
    ("missing_metric", "required_metric_missing"),
    ("missing_field", "malformed_specification"),
])
def test_invalid_specifications_fail_closed(bundle, mutation, reason):
    fixtures = bundle["fixtures.json"]["fixtures"]
    voice = bundle["fixtures.json"]["voices"]["it"]
    limits = bundle["thresholds.json"]["profiles"]["desktop"]["metrics"]
    limit = limits["end_to_end_first_audio_ms"]
    trace = bundle["traceability.json"]["requirements"]
    if mutation == "duplicate_fixture":
        fixtures.append(copy.deepcopy(fixtures[0]))
    elif mutation == "missing_requirement":
        trace.pop()
    elif mutation == "unknown_metric":
        fixtures[0]["metric_ids"] = ["unrecognized"]
    elif mutation == "unknown_task":
        fixtures[0]["test_tasks"] = ["99"]
    elif mutation == "real_speech":
        fixtures[0]["privacy"] = "user_recording"
    elif mutation == "wrong_rate":
        voice["sample_rate_hz"] = 16000
    elif mutation == "arbitrary_audio":
        bundle["fixtures.json"]["synthesis"]["entrypoint"] = "fake.wav"
    elif mutation == "no_category":
        bundle["fixtures.json"]["fixtures"] = [r for r in fixtures if r["category"] != "noise"]
    elif mutation == "wrong_units":
        limit["unit"] = "seconds"
    elif mutation == "missing_limit":
        del limits["wer"]
    elif mutation == "nan_limit":
        limit["value"] = float("nan")
    elif mutation == "zero_samples":
        limit["minimum_samples"] = 0
    elif mutation == "fake_calibration":
        limit["status"] = "calibrated"
    elif mutation == "fake_verified":
        trace[0]["local_evidence"] = "passed"
    elif mutation == "changed_source":
        bundle["traceability.json"]["requirement_source_sha256"] = "0" * 64
    elif mutation == "changed_requirement":
        trace[0]["requirement"] = "Different requirement"
    elif mutation == "path_escape":
        voice["model"] = "../outside.onnx"
    elif mutation == "default_device":
        bundle["hardware.example.json"]["devices"]["pc_input"] = "default"
    elif mutation == "unbounded_timeout":
        bundle["hardware.example.json"]["limits"]["session_timeout_seconds"] = -1
    elif mutation == "missing_timeouts":
        bundle["hardware.example.json"]["limits"] = {}
    elif mutation == "invalid_recipe":
        fixtures[0]["recipe"] = {"kind": "fake_wav"}
    elif mutation == "missing_event":
        bundle["metrics.json"]["events"] = bundle["metrics.json"]["events"][1:]
    elif mutation == "missing_metric":
        bundle["metrics.json"]["metrics"] = [r for r in bundle["metrics.json"]["metrics"]
                                              if r["id"] != "cpu_percent"]
    elif mutation == "missing_field":
        del fixtures[0]["expected_intent"]
    with pytest.raises(suite.SpecificationError, match=f"^{reason}$"):
        suite.validate_specs(bundle)


@pytest.mark.parametrize("payload,code", [
    ('{"schema_version":1,"schema_version":1}', "duplicate_json_key"),
    ('{"schema_version":1,"limit":NaN}', "nonfinite_json"),
    ('{"schema_version":true}', "unsupported_schema"),
    ('{"schema_version":2}', "unsupported_schema"),
    ('[]', "object_required"),
    ('not json', "unreadable_json"),
])
def test_json_rejects_ambiguous_or_invalid_input(tmp_path, payload, code):
    path = tmp_path / "fixture.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(suite.SpecificationError, match=f"^{code}$"):
        suite.read_json(path)


def limit(**overrides):
    return {"value": 5, "minimum_samples": 20, "status": "calibrated",
            "statistic": "p95", "direction": "max", **overrides}


def test_p95_failure_retains_outlier_and_failure_count():
    result = suite.summarize([1.0] * 18 + [100.0, 100.0], limit())
    assert result == {"sample_count": 20, "median": 1.0, "p95": 100.0, "maximum": 100.0,
                      "minimum": 1.0, "failure_count": 2, "status": "failed"}


@pytest.mark.parametrize("values,threshold", [
    ([], limit()),
    ([1], limit()),
    ([1] * 20, limit(status="provisional")),
    ([1] * 20, limit(status="uncalibrated", value=None)),
])
def test_insufficient_or_uncalibrated_measurement_never_passes(values, threshold):
    assert suite.summarize(values, threshold)["status"] == "unverified"


def test_threshold_boundary_is_inclusive_and_hard_failure_is_not_hidden():
    assert suite.summarize([5] * 20, limit())["status"] == "passed"
    assert suite.summarize([6], limit())["status"] == "failed"
    assert suite.summarize([0] * 999 + [1], limit(value=0, statistic="rate"))["status"] == "failed"
    assert suite.summarize([1, 1], limit(value=1, minimum_samples=2,
                                      direction="min", statistic="minimum"))["status"] == "passed"


@pytest.mark.parametrize("value", [True, None, float("nan"), float("inf"), "1"])
def test_invalid_measurement_rejected(value):
    with pytest.raises(suite.SpecificationError, match="invalid_metric_sample"):
        suite.summarize([value], limit())


def test_invalid_rate_cannot_cancel_a_real_failure():
    with pytest.raises(suite.SpecificationError, match="invalid_count_sample"):
        suite.summarize([-1, 1], limit(value=0, statistic="rate", minimum_samples=2))
    with pytest.raises(suite.SpecificationError, match="invalid_threshold_value"):
        suite.summarize([1], limit(value=None, status="invariant", minimum_samples=1))


def test_word_error_rate_uses_reference_denominators():
    threshold = limit(value=0.1, statistic="rate", unit="edits/reference_word", minimum_samples=2)
    result = suite.summarize([0, 2], threshold, denominators=[1, 99])
    assert result["value"] == 0.02
    assert result["numerator"] == 2 and result["denominator"] == 100
    assert result["status"] == "passed"
    with pytest.raises(suite.SpecificationError, match="rate_denominators_required"):
        suite.summarize([0, 2], threshold)


def test_numeric_overflow_cannot_emit_nonfinite_json():
    assert not suite.finite_number(10 ** 1000)
    with pytest.raises(suite.SpecificationError, match="nonfinite_metric_aggregate"):
        suite.summarize([-1e308, 1e308], limit())


def test_artifacts_cannot_be_written_into_source_tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(suite.SpecificationError, match="artifact_directory_must_be_temporary"):
        suite.artifact_root(repo / "docs", repo)
    assert suite.artifact_root(repo / ".voice-test-artifacts" / "run", repo).is_dir()
    assert suite.artifact_root(tmp_path / "external", repo).is_dir()


@pytest.mark.parametrize("require_hil,expected", [(False, "passed"), (True, "blocked")])
def test_baseline_writes_content_free_evidence_without_acoustic_claims(tmp_path, require_hil, expected):
    report = suite.run_baseline(suite.SPEC, tmp_path, "desktop", require_hil)
    assert report["status"] == expected
    assert report["local_baseline"] == "passed"
    assert report["hil"]["status"] == "blocked"
    assert report["hil"]["exchanges"] == 0
    assert report["cleanup"]["streams_opened"] == 0
    folder = tmp_path / report["correlation_id"]
    assert json.loads((folder / "results.json").read_text()) == report
    raw = (folder / "metrics.jsonl").read_text()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert rows and all(row["status"] == "unverified" and row["sample_count"] == 0 for row in rows)
    assert all(row["correlation_id"] == report["correlation_id"] for row in rows)
    assert not list(folder.glob("*.wav"))
    assert not any(word in json.dumps(report) + raw for word in
                   ("expected_authoritative_transcript", "canonical_requirement", "USERPROFILE"))
    assert all(not row["audio_generated"] for row in report["voices"].values())


def test_required_hil_exit_is_blocked_not_success(tmp_path, capsys):
    assert suite.main(["--artifact-dir", str(tmp_path), "--require-hil"]) == 2
    assert json.loads(capsys.readouterr().out)["hil"]["status"] == "blocked"


def test_invalid_spec_failure_does_not_echo_sensitive_input(tmp_path, monkeypatch, capsys):
    (tmp_path / "fixtures.json").write_text("secret input not json", encoding="utf-8")
    monkeypatch.setattr(suite, "SPEC", tmp_path)
    assert suite.main(["--artifact-dir", str(tmp_path / "output")]) == 1
    output = capsys.readouterr().out
    assert "secret" not in output and "Traceback" not in output
    assert json.loads(output)["status"] == "failed"
