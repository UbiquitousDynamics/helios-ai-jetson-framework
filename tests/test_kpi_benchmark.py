from __future__ import annotations

import json

import pytest

from scripts.benchmark_kpi import main, run_benchmarks


def test_small_kpi_benchmark_reports_schema_and_invariants() -> None:
    result = run_benchmarks(events=32, queries=2, resource_samples=2, queue_size=4)

    assert result["schema_version"] == 1
    assert result["parameters"] == {
        "events": 32,
        "queries": 2,
        "resource_samples": 2,
        "queue_size": 4,
    }
    assert set(result) >= {
        "event_record_overhead",
        "async_sqlite_throughput",
        "raw_database_query",
        "dashboard_summary_query",
        "bounded_queue",
        "resource_sampler_overhead",
    }

    recorder = result["event_record_overhead"]
    assert recorder["record_calls"] == 32
    assert recorder["persisted_events"] == 32
    assert recorder["dropped_events"] == 0
    assert recorder["shutdown_drained"] is True
    assert recorder["record_call_duration"]["samples"] == 32

    sqlite = result["async_sqlite_throughput"]
    assert sqlite["submitted_events"] == 32
    assert sqlite["persisted_events"] == 32
    assert sqlite["dropped_events"] == 0
    assert sqlite["throughput_events_per_second"] > 0
    assert sqlite["database_size_bytes"] > 0

    raw_query = result["raw_database_query"]
    assert raw_query["iterations"] == 2
    assert raw_query["returned_rows"] == 32
    assert raw_query["query_duration"]["samples"] == 2

    summary = result["dashboard_summary_query"]
    assert summary["iterations"] == 2
    assert summary["request_count"] == 32
    assert summary["query_duration"]["samples"] == 2

    queue = result["bounded_queue"]
    assert queue["capacity_events"] == 4
    assert queue["queue_depth_at_capacity"] == 4
    assert queue["dropped_events"] == queue["fill_attempts"] - 4
    assert queue["traced_peak_increase_bytes"] >= queue["traced_current_increase_bytes"]
    assert queue["shutdown_drained"] is True
    assert queue["queue_depth_after_shutdown"] == 0

    sampler = result["resource_sampler_overhead"]
    assert sampler["samples"] == 2
    assert 0 <= sampler["available_samples"] <= 2
    assert sampler["wall_duration"]["samples"] == 2
    assert sampler["process_cpu_duration"]["samples"] == 2

    # The public return value is directly suitable for machine-readable output.
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    ("name", "value"),
    (("events", 0), ("queries", True), ("resource_samples", -1), ("queue_size", 0)),
)
def test_kpi_benchmark_rejects_unsafe_sizes(name: str, value: object) -> None:
    with pytest.raises(ValueError):
        run_benchmarks(**{name: value})


def test_benchmark_cli_prints_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "--events",
                "8",
                "--queries",
                "1",
                "--resource-samples",
                "1",
                "--queue-size",
                "2",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["parameters"]["events"] == 8
    assert output["bounded_queue"]["capacity_events"] == 2
