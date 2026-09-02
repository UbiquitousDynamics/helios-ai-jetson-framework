from __future__ import annotations

import json
from pathlib import Path

from scripts.device_voice_diagnostics import (
    _LOCAL_FALLBACK_INTERACTIVE_TARGET_SECONDS,
    _LOCAL_FALLBACK_HISTORY_TURNS,
    _LOCAL_FALLBACK_MAX_FIRST_TOKEN_SECONDS,
    _LOCAL_FALLBACK_MAX_TOTAL_SECONDS,
    _completed_route,
    analyze_log,
    analyze_metrics,
    parse_args,
)


def test_log_analysis_counts_failure_signals_without_transcripts(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text(
        "\n".join(
            [
                "provider=openai-codex action=thread_resume reason=worker_error",
                "Network gate state=offline reason=tls_timeout",
                "Network gate state=online reason=quality_validated",
                "provider=ollama event=stream_worker_stop_unacknowledged stop_reason=timeout",
                "Completed talk request using route codex-talk-terra",
            ]
        ),
        encoding="utf-8",
    )

    report = analyze_log(path)

    assert report["counts"]["codex_worker_error"] == 1
    assert report["counts"]["local_stream_stop_unacknowledged"] == 1
    assert report["interpretation"]["codex_resume_instability"] is True
    assert report["interpretation"]["network_flapping_signal"] is True
    assert all(
        "transcript" not in line.lower() for lines in report["samples"].values() for line in lines
    )


def test_metrics_analysis_reads_jsonl_events_and_categories(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    path.write_text(
        json.dumps({"event": "llm_request_failed", "error_category": "read_timeout"})
        + "\n"
        + json.dumps({"name": "llm_request_succeeded"})
        + "\nnot-json\n",
        encoding="utf-8",
    )

    report = analyze_metrics(path)

    assert report["events"] == {
        "llm_request_failed": 1,
        "llm_request_succeeded": 1,
    }
    assert report["error_categories"] == {"read_timeout": 1}


def test_completed_route_uses_the_final_completed_route_only() -> None:
    assert (
        _completed_route(
            [
                "conversation_session=abc event=llm_route_selected route=codex-talk-luna",
                "Completed talk request using route local-talk (provider=ollama)",
            ]
        )
        == "local-talk"
    )
    assert _completed_route(["Planning talk request with eligible routes: codex-talk-luna"]) is None


def test_parse_args_rejects_unbounded_or_empty_matrices() -> None:
    args = parse_args(
        [
            "--repetitions",
            "2",
            "--network-samples",
            "1",
            "--skip-live",
            "--skip-remote-live",
        ]
    )

    assert args.repetitions == 2
    assert args.network_samples == 1
    assert args.skip_live is True
    assert args.skip_remote_live is True


def test_local_fallback_probe_keeps_a_production_sized_but_bounded_budget() -> None:
    assert _LOCAL_FALLBACK_INTERACTIVE_TARGET_SECONDS == 15.0
    assert _LOCAL_FALLBACK_HISTORY_TURNS == 10
    assert _LOCAL_FALLBACK_MAX_FIRST_TOKEN_SECONDS == 30.0
    assert _LOCAL_FALLBACK_MAX_TOTAL_SECONDS >= _LOCAL_FALLBACK_MAX_FIRST_TOKEN_SECONDS
