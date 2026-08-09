import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from api.api_client import _content_safe_jsonl_sink
from api.metrics import MetricEvent, SafeMetricsRecorder, record_safely


def test_queue_overflow_is_nonblocking_counted_and_observable() -> None:
    entered = threading.Event()
    release = threading.Event()
    emitted: list[dict[str, object]] = []

    def blocking_sink(payload: dict[str, object]) -> None:
        entered.set()
        release.wait(timeout=2)
        emitted.append(payload)

    recorder = SafeMetricsRecorder(
        sink=blocking_sink,
        asynchronous=True,
        queue_size=1,
        batch_size=1,
        flush_interval_seconds=0.01,
    )
    recorder.record(MetricEvent("first"))
    assert entered.wait(timeout=1)

    recorder.record(MetricEvent("second"))
    started = time.perf_counter()
    recorder.record(MetricEvent("third"))
    enqueue_seconds = time.perf_counter() - started

    assert enqueue_seconds < 0.1
    assert recorder.dropped_events == 1
    release.set()
    recorder.close()
    assert any(payload["event"] == "metrics_events_dropped" for payload in emitted)


def test_synchronous_sink_failure_never_escapes_record() -> None:
    def broken_sink(_payload: object) -> None:
        raise OSError("storage unavailable")

    recorder = SafeMetricsRecorder(sink=broken_sink)

    recorded = recorder.record(MetricEvent("assistant_started"))

    assert recorded.event == "assistant_started"
    assert recorder.stats().failed_writes == 1


def test_record_safely_contains_schema_and_recorder_failures() -> None:
    recorder = SafeMetricsRecorder()

    assert record_safely(recorder, "voice_command", prompt="private") is None
    assert recorder.snapshot() == ()


def test_async_recorder_batches_and_final_flushes() -> None:
    batches: list[list[dict[str, object]]] = []

    class BatchSink:
        def write_batch(self, payloads: list[dict[str, object]]) -> None:
            batches.append(payloads)

    recorder = SafeMetricsRecorder(
        sink=BatchSink(),
        asynchronous=True,
        queue_size=8,
        batch_size=4,
        flush_interval_seconds=0.2,
    )
    for index in range(4):
        recorder.record(MetricEvent("sample", count=index + 1))
    recorder.close()

    assert [payload["count"] for payload in batches[0]] == [1, 2, 3, 4]
    assert recorder.stats().persisted == 4


def test_full_queue_shutdown_drains_after_a_blocked_sink_recovers() -> None:
    entered = threading.Event()
    release = threading.Event()
    emitted: list[dict[str, object]] = []

    def blocking_sink(payload: dict[str, object]) -> None:
        entered.set()
        release.wait(timeout=2)
        emitted.append(payload)

    recorder = SafeMetricsRecorder(
        sink=blocking_sink,
        asynchronous=True,
        queue_size=2,
        batch_size=1,
        flush_interval_seconds=0.01,
        shutdown_timeout_seconds=2,
    )
    recorder.record(MetricEvent("first"))
    assert entered.wait(timeout=1)
    recorder.record(MetricEvent("second"))
    recorder.record(MetricEvent("third"))
    recorder.record(MetricEvent("dropped"))

    stopped: list[bool] = []
    closer = threading.Thread(target=lambda: stopped.append(recorder.close()))
    closer.start()
    assert closer.is_alive()
    release.set()
    closer.join(timeout=2)

    assert stopped == [True]
    assert recorder.stats().queue_depth == 0
    assert [payload["event"] for payload in emitted] == [
        "first",
        "second",
        "metrics_events_dropped",
        "third",
    ]


def test_extended_event_remains_content_free() -> None:
    payload = MetricEvent(
        "llm_request_succeeded",
        provider="ollama",
        model="model:1",
        route="local-talk",
        locality="local",
        network_quality_tier="good",
        end_to_end_ms=123.0,
        cpu_percent=25.0,
        success=True,
    ).as_dict()

    serialized = str(payload).lower()
    for forbidden in ("prompt", "response", "transcript", "credential", "header"):
        assert forbidden not in serialized


def test_legacy_jsonl_sink_omits_content_and_high_cardinality_ids(tmp_path: Path) -> None:
    path = tmp_path / "metrics.jsonl"
    sink = _content_safe_jsonl_sink(path, retention_days=1)

    sink(
        {
            "event": "sample",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "provider": "local",
            "request_id": "provider-id",
            "attempt_id": "attempt-id",
            "prompt": "never persist this marker",
        }
    )

    stored = path.read_text(encoding="utf-8")
    assert '"provider":"local"' in stored
    assert "provider-id" not in stored
    assert "attempt-id" not in stored
    assert "never persist this marker" not in stored
