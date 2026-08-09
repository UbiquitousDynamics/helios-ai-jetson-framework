"""Run synthetic, content-free benchmarks for the optional KPI subsystem.

The benchmark intentionally reports measurements instead of enforcing timing
budgets.  Results vary with the device, filesystem, Python build, and current
system load, so callers should compare the emitted JSON with their own baseline.
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
import tempfile
import threading
import time
import tracemalloc
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.metrics import MetricEvent, SafeMetricsRecorder  # noqa: E402
from observability.aggregate import KPIQueryService  # noqa: E402
from observability.resources import ResourceCollector  # noqa: E402
from observability.storage import SQLiteKPIStore  # noqa: E402

_MAX_EVENTS = 1_000_000
_MAX_ITERATIONS = 10_000
_MAX_QUEUE_SIZE = 100_000


def _positive_integer(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between one and {maximum}")
    return value


def _cli_positive_integer(value: str) -> int:
    try:
        converted = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if converted < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return converted


def _percentile(sorted_values: Sequence[int], quantile: float) -> float:
    """Return an R-7 linearly interpolated percentile for non-empty values."""

    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] + fraction * (sorted_values[upper] - sorted_values[lower])


def _duration_summary(samples_ns: Sequence[int]) -> dict[str, float | int]:
    if not samples_ns:
        raise ValueError("at least one timing sample is required")
    ordered = sorted(samples_ns)
    total = sum(ordered)
    to_ms = 1 / 1_000_000
    return {
        "samples": len(ordered),
        "minimum_ms": ordered[0] * to_ms,
        "mean_ms": (total / len(ordered)) * to_ms,
        "p50_ms": _percentile(ordered, 0.50) * to_ms,
        "p95_ms": _percentile(ordered, 0.95) * to_ms,
        "p99_ms": _percentile(ordered, 0.99) * to_ms,
        "maximum_ms": ordered[-1] * to_ms,
    }


class _DiscardSink:
    def __init__(self) -> None:
        self.persisted = 0

    def write_batch(self, payloads: Sequence[Mapping[str, Any]]) -> None:
        self.persisted += len(payloads)


class _GateSink:
    """Hold the recorder worker so its bounded mailbox can be filled exactly."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.persisted = 0

    def write_batch(self, payloads: Sequence[Mapping[str, Any]]) -> None:
        self.entered.set()
        self.release.wait(timeout=10.0)
        self.persisted += len(payloads)


def _event_record_benchmark(events: int) -> dict[str, Any]:
    sink = _DiscardSink()
    queue_size = events + 1
    recorder = SafeMetricsRecorder(
        sink=sink,
        retain=0,
        asynchronous=True,
        queue_size=queue_size,
        batch_size=min(128, queue_size),
        flush_interval_seconds=0.005,
        shutdown_timeout_seconds=10.0,
    )
    event = MetricEvent("benchmark_event_record", mode="benchmark", outcome="succeeded")
    durations: list[int] = []
    for _ in range(events):
        started_ns = time.perf_counter_ns()
        recorder.record(event)
        durations.append(time.perf_counter_ns() - started_ns)
    drained = recorder.close()
    stats = recorder.stats()
    return {
        "record_call_duration": _duration_summary(durations),
        "record_calls": events,
        "persisted_events": stats.persisted,
        "dropped_events": stats.dropped_full,
        "failed_writes": stats.failed_writes,
        "shutdown_drained": drained,
    }


def _synthetic_request() -> MetricEvent:
    return MetricEvent(
        "llm_request_succeeded",
        mode="benchmark",
        locality="local",
        route="benchmark",
        outcome="succeeded",
        success=True,
        latency_ms=12.0,
        first_token_ms=4.0,
        end_to_end_ms=12.0,
        input_tokens=8,
        output_tokens=4,
        total_tokens=12,
    )


def _sqlite_benchmarks(
    directory: Path,
    *,
    events: int,
    queries: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    database_path = directory / "kpi-benchmark.sqlite3"
    store = SQLiteKPIStore(
        database_path,
        raw_retention_days=365,
        rollup_retention_days=365,
        max_size_bytes=1024 * 1024 * 1024,
        maintenance_interval_seconds=3_600.0,
    )
    queue_size = events + 1
    recorder = SafeMetricsRecorder(
        sink=store,
        retain=0,
        asynchronous=True,
        queue_size=queue_size,
        batch_size=min(128, queue_size),
        flush_interval_seconds=0.005,
        shutdown_timeout_seconds=30.0,
    )
    event = _synthetic_request()
    try:
        started_ns = time.perf_counter_ns()
        for _ in range(events):
            recorder.record(event)
        drained = recorder.close()
        elapsed_ns = max(1, time.perf_counter_ns() - started_ns)
        stats = recorder.stats()
        storage_status = store.status()
        async_sqlite = {
            "submitted_events": events,
            "persisted_events": stats.persisted,
            "dropped_events": stats.dropped_full,
            "failed_writes": stats.failed_writes,
            "elapsed_seconds": elapsed_ns / 1_000_000_000,
            "throughput_events_per_second": stats.persisted * 1_000_000_000 / elapsed_ns,
            "shutdown_drained": drained,
            "database_size_bytes": storage_status["size_bytes"],
            "journal_mode": storage_status["journal_mode"],
        }

        query_limit = min(events, 100_000)
        query_durations: list[int] = []
        returned_rows = 0
        for _ in range(queries):
            started_ns = time.perf_counter_ns()
            rows = store.query_events(limit=query_limit, ascending=False)
            query_durations.append(time.perf_counter_ns() - started_ns)
            returned_rows = len(rows)
        raw_query = {
            "query_duration": _duration_summary(query_durations),
            "iterations": queries,
            "limit": query_limit,
            "returned_rows": returned_rows,
        }

        aggregate_limit = min(events + 1, 100_000)
        aggregates = KPIQueryService(store, max_events=aggregate_limit)
        summary_durations: list[int] = []
        summary: Mapping[str, Any] = {}
        for _ in range(queries):
            started_ns = time.perf_counter_ns()
            summary = aggregates.summary()
            summary_durations.append(time.perf_counter_ns() - started_ns)
        summary_query = {
            "query_duration": _duration_summary(summary_durations),
            "iterations": queries,
            "request_count": int(summary.get("request_count", 0)),
            "truncated": bool(summary.get("truncated", False)),
        }
        return async_sqlite, raw_query, summary_query
    finally:
        recorder.close()
        store.close()


def _bounded_queue_benchmark(queue_size: int) -> dict[str, Any]:
    sink = _GateSink()
    recorder = SafeMetricsRecorder(
        sink=sink,
        retain=0,
        asynchronous=True,
        queue_size=queue_size,
        batch_size=1,
        flush_interval_seconds=0.005,
        shutdown_timeout_seconds=10.0,
    )
    event = MetricEvent("benchmark_queue", mode="benchmark")
    recorder.record(event)
    if not sink.entered.wait(timeout=5.0):
        sink.release.set()
        recorder.close()
        raise RuntimeError("KPI benchmark recorder worker did not start")

    tracing_was_active = tracemalloc.is_tracing()
    if not tracing_was_active:
        tracemalloc.start()
    before_current, before_peak = tracemalloc.get_traced_memory()
    try:
        for _ in range(queue_size):
            recorder.record(event)
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        if not tracing_was_active:
            tracemalloc.stop()

    drop_attempts = queue_size * 3
    durations: list[int] = []
    try:
        for _ in range(drop_attempts):
            started_ns = time.perf_counter_ns()
            recorder.record(event)
            durations.append(time.perf_counter_ns() - started_ns)
        at_capacity = recorder.stats()
    finally:
        sink.release.set()
    drained = recorder.close()
    final_stats = recorder.stats()
    fill_attempts = queue_size + drop_attempts
    return {
        "capacity_events": queue_size,
        "fill_attempts": fill_attempts,
        "queue_depth_at_capacity": at_capacity.queue_depth,
        "dropped_events": at_capacity.dropped_full,
        "enqueued_events": fill_attempts - at_capacity.dropped_full,
        "record_call_duration_while_full": _duration_summary(durations),
        "traced_current_increase_bytes": max(0, current_bytes - before_current),
        "traced_peak_increase_bytes": max(0, peak_bytes - max(before_current, before_peak)),
        "shutdown_drained": drained,
        "queue_depth_after_shutdown": final_stats.queue_depth,
    }


def _resource_sampler_benchmark(directory: Path, samples: int) -> dict[str, Any]:
    # An unknown platform value disables procfs and tegrastats probes, retaining
    # a portable disk-usage sample without invoking an external executable.
    collector = ResourceCollector(disk_path=directory, system="benchmark")
    wall_durations: list[int] = []
    cpu_durations: list[int] = []
    available = 0
    for _ in range(samples):
        wall_started_ns = time.perf_counter_ns()
        cpu_started_ns = time.process_time_ns()
        snapshot = collector.collect()
        cpu_durations.append(time.process_time_ns() - cpu_started_ns)
        wall_durations.append(time.perf_counter_ns() - wall_started_ns)
        available += int(snapshot.available)
    return {
        "wall_duration": _duration_summary(wall_durations),
        "process_cpu_duration": _duration_summary(cpu_durations),
        "samples": samples,
        "available_samples": available,
        "external_commands_enabled": False,
    }


def run_benchmarks(
    *,
    events: int = 5_000,
    queries: int = 20,
    resource_samples: int = 5,
    queue_size: int = 256,
) -> dict[str, Any]:
    """Run every KPI benchmark and return a JSON-compatible result mapping."""

    events = _positive_integer(events, "events", _MAX_EVENTS)
    queries = _positive_integer(queries, "queries", _MAX_ITERATIONS)
    resource_samples = _positive_integer(
        resource_samples,
        "resource_samples",
        _MAX_ITERATIONS,
    )
    queue_size = _positive_integer(queue_size, "queue_size", _MAX_QUEUE_SIZE)

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with tempfile.TemporaryDirectory(prefix="helios-kpi-benchmark-") as temporary:
        directory = Path(temporary)
        async_sqlite, raw_query, summary_query = _sqlite_benchmarks(
            directory,
            events=events,
            queries=queries,
        )
        bounded_queue = _bounded_queue_benchmark(queue_size)
        sampler = _resource_sampler_benchmark(directory, resource_samples)

    return {
        "schema_version": 1,
        "generated_at_utc": generated_at,
        "runtime": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "operating_system": platform.system(),
            "machine_architecture": platform.machine(),
            "sqlite_version": sqlite3.sqlite_version,
        },
        "parameters": {
            "events": events,
            "queries": queries,
            "resource_samples": resource_samples,
            "queue_size": queue_size,
        },
        "event_record_overhead": _event_record_benchmark(events),
        "async_sqlite_throughput": async_sqlite,
        "raw_database_query": raw_query,
        "dashboard_summary_query": summary_query,
        "bounded_queue": bounded_queue,
        "resource_sampler_overhead": sampler,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark Helios' content-free KPI recorder, store, queries, and sampler.",
    )
    parser.add_argument("--events", type=_cli_positive_integer, default=5_000)
    parser.add_argument("--queries", type=_cli_positive_integer, default=20)
    parser.add_argument("--resource-samples", type=_cli_positive_integer, default=5)
    parser.add_argument("--queue-size", type=_cli_positive_integer, default=256)
    parser.add_argument("--pretty", action="store_true", help="indent the JSON output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = run_benchmarks(
            events=arguments.events,
            queries=arguments.queries,
            resource_samples=arguments.resource_samples,
            queue_size=arguments.queue_size,
        )
    except ValueError as error:
        _parser().error(str(error))
    print(
        json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=True,
            indent=2 if arguments.pretty else None,
            separators=None if arguments.pretty else (",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
