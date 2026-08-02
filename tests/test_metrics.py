import json
import threading
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from api.api_client import _content_safe_jsonl_sink
from api.metrics import MetricEvent, SafeMetricsRecorder
from api.providers.contracts import ErrorCategory, Usage


def fixed_clock():
    return datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def test_recorder_emits_only_closed_content_free_schema():
    emitted = []
    recorder = SafeMetricsRecorder(sink=emitted.append, clock=fixed_clock)
    event = MetricEvent.from_usage(
        "attempt.completed",
        Usage(input_tokens=10, output_tokens=2, total_tokens=12),
        provider="groq",
        model="model",
        mode="talk",
        language="it",
        route_reason="auto.complex",
        latency_ms=12.5,
        cost_usd=Decimal("0.0001"),
        error_category=ErrorCategory.RATE_LIMITED,
    )

    recorded = recorder.record(event)

    assert recorded.timestamp == fixed_clock()
    assert recorder.snapshot() == (recorded,)
    assert emitted[0]["cost_usd"] == "0.0001"
    assert emitted[0]["error_category"] == "rate_limited"
    assert "prompt" not in emitted[0]
    assert "response" not in emitted[0]


def test_disabled_recorder_does_not_retain_or_emit():
    emitted = []
    recorder = SafeMetricsRecorder(enabled=False, sink=emitted.append, clock=fixed_clock)
    recorder.record(MetricEvent("attempt.started"))
    assert recorder.snapshot() == ()
    assert emitted == []


def test_labels_timings_and_cost_are_validated():
    with pytest.raises(ValueError, match="machine-readable"):
        MetricEvent("contains private text")
    with pytest.raises(ValueError, match="non-negative"):
        MetricEvent("attempt", latency_ms=-1)
    with pytest.raises(TypeError, match="Decimal"):
        MetricEvent("attempt", cost_usd="0.1")
    with pytest.raises(ValueError, match="finite"):
        MetricEvent("attempt", latency_ms=True)
    with pytest.raises(ValueError, match="integer"):
        SafeMetricsRecorder(retain=1.5)


def test_jsonl_metric_sink_prunes_records_older_than_retention(tmp_path):
    path = tmp_path / "metrics.jsonl"
    path.write_text(
        json.dumps({"event": "old", "timestamp": "2026-01-01T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    sink = _content_safe_jsonl_sink(path, retention_days=14)

    sink({"event": "new", "timestamp": "2026-07-27T12:00:00Z"})

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert records == [{"event": "new", "timestamp": "2026-07-27T12:00:00Z"}]


def test_async_recorder_keeps_sink_io_off_the_calling_thread_and_flushes():
    entered = threading.Event()
    release = threading.Event()
    emitted = []

    def blocking_sink(payload):
        entered.set()
        release.wait(timeout=1)
        emitted.append(payload)

    recorder = SafeMetricsRecorder(
        sink=blocking_sink,
        clock=fixed_clock,
        asynchronous=True,
    )

    recorded = recorder.record(MetricEvent("attempt.completed"))
    assert entered.wait(timeout=1)
    assert emitted == []

    release.set()
    recorder.close()
    recorder.close()

    assert emitted == [recorded.as_dict()]
