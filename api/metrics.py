"""Content-safe, provider-neutral operational metrics.

The public ``MetricEvent`` and ``SafeMetricsRecorder`` names are retained for
compatibility.  The schema is deliberately closed: conversation content,
headers, endpoints, credentials, and arbitrary tags have nowhere to enter.
"""

from __future__ import annotations

import logging
import math
import numbers
import queue
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from api.providers.contracts import ErrorCategory, Usage

logger = logging.getLogger(__name__)
_LEGACY_EVENT_FIELDS = frozenset(
    {
        "event",
        "timestamp",
        "provider",
        "model",
        "mode",
        "language",
        "route_reason",
        "request_id",
        "attempt_id",
        "latency_ms",
        "first_token_ms",
        "first_audio_ms",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "remaining_requests",
        "remaining_tokens",
        "cost_usd",
        "error_category",
        "fallback_count",
    }
)


def _safe_label(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError(f"{name} must be a non-empty label of at most 200 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} contains control characters")
    if any(character.isspace() for character in value):
        raise ValueError(f"{name} must be a machine-readable label")
    return value


def _number(
    value: float | None,
    name: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, numbers.Real)
        or not math.isfinite(float(value))
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        if minimum == 0.0 and maximum is None:
            raise ValueError(f"{name} must be finite and non-negative")
        bounds = (
            f" between {minimum:g} and {maximum:g}"
            if maximum is not None
            else f" and at least {minimum:g}"
        )
        raise ValueError(f"{name} must be finite{bounds}")
    return float(value)


@dataclass(frozen=True, slots=True)
class MetricEvent:
    """A deliberately closed operational schema with no content fields."""

    # Keep the original public constructor order intact. New fields are
    # appended below so positional callers from earlier releases remain valid.
    event: str
    timestamp: datetime | None = None
    provider: str | None = None
    model: str | None = None
    mode: str | None = None
    language: str | None = None
    route_reason: str | None = None
    request_id: str | None = None
    attempt_id: str | None = None
    latency_ms: float | None = None
    first_token_ms: float | None = None
    first_audio_ms: float | None = None
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    remaining_requests: int | None = None
    remaining_tokens: int | None = None
    cost_usd: Decimal | None = None
    error_category: ErrorCategory | None = None
    fallback_count: int = 0

    # Additional low-cardinality dimensions and configured identifiers.
    resolved_model: str | None = None
    route: str | None = None
    locality: str | None = None
    model_tier: str | None = None
    outcome: str | None = None
    rejection_reason: str | None = None
    fallback_from: str | None = None
    fallback_to: str | None = None
    fallback_cause: str | None = None
    network_state: str | None = None
    network_quality_tier: str | None = None
    network_reason: str | None = None
    interface_kind: str | None = None
    circuit_state: str | None = None
    previous_circuit_state: str | None = None
    timeout_category: str | None = None
    resource_scope: str | None = None

    # Additional latency and stage durations, in milliseconds.
    actual_first_audio_ms: float | None = None
    speech_dispatch_ms: float | None = None
    listening_ms: float | None = None
    speech_finalization_ms: float | None = None
    stt_ms: float | None = None
    rag_ms: float | None = None
    routing_ms: float | None = None
    inference_ms: float | None = None
    tts_synthesis_ms: float | None = None
    audio_playback_ms: float | None = None
    audio_duration_ms: float | None = None
    end_to_end_ms: float | None = None
    streaming_lead_ms: float | None = None

    # Network measurements.
    dns_ms: float | None = None
    tcp_ms: float | None = None
    tls_ms: float | None = None
    ttfb_ms: float | None = None
    goodput_kbps: float | None = None
    network_quality_score: float | None = None
    probe_success_ratio: float | None = None

    # Device measurements.
    cpu_percent: float | None = None
    gpu_percent: float | None = None
    memory_percent: float | None = None
    swap_percent: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    swap_used_mb: float | None = None
    swap_total_mb: float | None = None
    cpu_temperature_c: float | None = None
    gpu_temperature_c: float | None = None
    power_w: float | None = None
    storage_used_mb: float | None = None
    cpu_frequency_mhz: float | None = None
    gpu_frequency_mhz: float | None = None

    # Additional usage, budget, and counters.
    estimated_input_tokens: int | None = None
    estimated_output_tokens: int | None = None
    retry_count: int = 0
    complexity_score: int | None = None
    count: int = 1
    dropped_count: int = 0
    wake_word_count: int = 0
    recognized_count: int = 0
    cancellation_count: int = 0
    interruption_count: int = 0
    estimated_cost_usd: Decimal | None = None

    # Controlled state flags.
    success: bool | None = None
    interface_available: bool | None = None
    probe_success: bool | None = None
    network_forced_local: bool | None = None
    speech_committed: bool | None = None
    streaming: bool | None = None
    throttled: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event, str):
            raise TypeError("event must be a string")
        for name in (
            "event",
            "provider",
            "model",
            "resolved_model",
            "mode",
            "language",
            "route",
            "locality",
            "model_tier",
            "route_reason",
            "outcome",
            "rejection_reason",
            "fallback_from",
            "fallback_to",
            "fallback_cause",
            "network_state",
            "network_quality_tier",
            "network_reason",
            "interface_kind",
            "circuit_state",
            "previous_circuit_state",
            "timeout_category",
            "resource_scope",
            "request_id",
            "attempt_id",
        ):
            _safe_label(getattr(self, name), name)
        if self.timestamp is not None and self.timestamp.tzinfo is None:
            raise ValueError("metric timestamp must be timezone-aware")
        for name in (
            "latency_ms",
            "first_token_ms",
            "first_audio_ms",
            "actual_first_audio_ms",
            "speech_dispatch_ms",
            "listening_ms",
            "speech_finalization_ms",
            "stt_ms",
            "rag_ms",
            "routing_ms",
            "inference_ms",
            "tts_synthesis_ms",
            "audio_playback_ms",
            "audio_duration_ms",
            "end_to_end_ms",
            "streaming_lead_ms",
            "dns_ms",
            "tcp_ms",
            "tls_ms",
            "ttfb_ms",
            "goodput_kbps",
            "memory_used_mb",
            "memory_total_mb",
            "swap_used_mb",
            "swap_total_mb",
            "power_w",
            "storage_used_mb",
            "cpu_frequency_mhz",
            "gpu_frequency_mhz",
        ):
            _number(getattr(self, name), name)
        for name in ("cpu_temperature_c", "gpu_temperature_c"):
            _number(getattr(self, name), name, minimum=-273.15, maximum=1_000.0)
        for name in (
            "network_quality_score",
            "probe_success_ratio",
        ):
            _number(getattr(self, name), name, maximum=1.0)
        for name in ("cpu_percent", "gpu_percent", "memory_percent", "swap_percent"):
            _number(getattr(self, name), name, maximum=100.0)
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "estimated_input_tokens",
            "estimated_output_tokens",
            "remaining_requests",
            "remaining_tokens",
            "retry_count",
            "fallback_count",
            "complexity_score",
            "count",
            "dropped_count",
            "wake_word_count",
            "recognized_count",
            "cancellation_count",
            "interruption_count",
        ):
            value = getattr(self, name)
            required = name in {
                "retry_count",
                "fallback_count",
                "count",
                "dropped_count",
                "wake_word_count",
                "recognized_count",
                "cancellation_count",
                "interruption_count",
            }
            if isinstance(value, bool) or (required and value is None):
                raise ValueError(f"{name} must be a non-negative integer")
            if value is not None and (not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("cost_usd", "estimated_cost_usd"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, Decimal):
                    raise TypeError(f"{name} must be a Decimal")
                if not value.is_finite() or value < 0:
                    raise ValueError(f"{name} must be finite and non-negative")
        if self.error_category is not None and not isinstance(self.error_category, ErrorCategory):
            raise TypeError("error_category must be an ErrorCategory")
        for name in (
            "success",
            "interface_available",
            "probe_success",
            "network_forced_local",
            "speech_committed",
            "streaming",
            "throttled",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{name} must be a boolean")

    @classmethod
    def from_usage(cls, event: str, usage: Usage, **kwargs: Any) -> MetricEvent:
        return cls(
            event=event,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            total_tokens=usage.total_tokens,
            **kwargs,
        )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        timestamp = payload["timestamp"]
        if timestamp is not None:
            payload["timestamp"] = (
                timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            )
        for name in ("cost_usd", "estimated_cost_usd"):
            if payload[name] is not None:
                payload[name] = str(payload[name])
        if payload["error_category"] is not None:
            payload["error_category"] = payload["error_category"].value
        return {
            name: value
            for name, value in payload.items()
            if value is not None or name in _LEGACY_EVENT_FIELDS
        }


@dataclass(frozen=True, slots=True)
class RecorderStats:
    accepted: int
    persisted: int
    dropped_full: int
    failed_writes: int
    queue_depth: int
    queue_capacity: int
    closed: bool

    def as_dict(self) -> dict[str, int | bool]:
        return asdict(self)


class FanoutMetricSink:
    """Best-effort fan-out that preserves independent legacy and KPI sinks."""

    def __init__(self, sinks: Iterable[Any]) -> None:
        self._sinks = tuple(sink for sink in sinks if sink is not None)

    @staticmethod
    def _write(sink: Any, payloads: list[Mapping[str, Any]]) -> None:
        write_batch = getattr(sink, "write_batch", None)
        if callable(write_batch):
            write_batch(payloads)
            return
        for payload in payloads:
            sink(payload)

    def write_batch(self, payloads: list[Mapping[str, Any]]) -> None:
        successes = 0
        for sink in self._sinks:
            try:
                self._write(sink, payloads)
                successes += 1
            except Exception:
                logger.warning("Unable to persist metrics to one configured sink")
        if self._sinks and successes == 0:
            raise RuntimeError("all metric sinks failed")

    def __call__(self, payload: Mapping[str, Any]) -> None:
        self.write_batch([payload])


class SafeMetricsRecorder:
    """Bounded, no-throw event recorder with optional asynchronous batching."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        sink: Callable[[Mapping[str, Any]], None] | Any | None = None,
        clock: Callable[[], datetime] | None = None,
        retain: int = 1_000,
        asynchronous: bool = False,
        queue_size: int = 256,
        batch_size: int = 32,
        flush_interval_seconds: float = 0.25,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        if isinstance(retain, bool) or not isinstance(retain, int) or retain < 0:
            raise ValueError("retain must be a non-negative integer")
        if not isinstance(asynchronous, bool):
            raise TypeError("asynchronous must be a boolean")
        if isinstance(queue_size, bool) or not isinstance(queue_size, int) or queue_size < 1:
            raise ValueError("queue_size must be a positive integer")
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= queue_size
        ):
            raise ValueError("batch_size must be between one and queue_size")
        for name, value in (
            ("flush_interval_seconds", flush_interval_seconds),
            ("shutdown_timeout_seconds", shutdown_timeout_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, numbers.Real)
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError(f"{name} must be finite and positive")
        self.enabled = enabled
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._retain = retain
        self._batch_size = batch_size
        self._flush_interval = float(flush_interval_seconds)
        self._shutdown_timeout = float(shutdown_timeout_seconds)
        self._events: list[MetricEvent] = []
        self._lock = threading.RLock()
        self._closed = False
        self._accepted = 0
        self._persisted = 0
        self._dropped_full = 0
        self._reported_dropped = 0
        self._failed_writes = 0
        self._stop_requested = threading.Event()
        self._mailbox: queue.Queue[object] | None = None
        self._worker: threading.Thread | None = None
        if enabled and sink is not None and asynchronous:
            self._mailbox = queue.Queue(maxsize=queue_size)
            self._worker = threading.Thread(
                target=self._consume,
                name="helios-metrics",
                daemon=True,
            )
            self._worker.start()

    @staticmethod
    def _persist_batch(sink: Any, payloads: list[Mapping[str, Any]]) -> None:
        write_batch = getattr(sink, "write_batch", None)
        if callable(write_batch):
            write_batch(payloads)
            return
        for payload in payloads:
            sink(payload)

    def _drop_payload(self) -> Mapping[str, Any] | None:
        with self._lock:
            dropped = self._dropped_full - self._reported_dropped
            if dropped <= 0:
                return None
            self._reported_dropped = self._dropped_full
        try:
            return MetricEvent(
                "metrics_events_dropped",
                timestamp=self._clock(),
                dropped_count=dropped,
                count=dropped,
                outcome="queue_full",
            ).as_dict()
        except Exception:
            return None

    def _consume(self) -> None:
        assert self._mailbox is not None
        assert self._sink is not None
        while True:
            try:
                item = self._mailbox.get(timeout=self._flush_interval)
            except queue.Empty:
                if self._stop_requested.is_set():
                    return
                continue
            queued: list[MetricEvent] = []
            consumed = 1
            queued.append(item)  # type: ignore[arg-type]

            deadline = time.monotonic() + self._flush_interval
            while len(queued) < self._batch_size:
                if self._stop_requested.is_set() and self._mailbox.empty():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._mailbox.get(timeout=remaining)
                except queue.Empty:
                    break
                consumed += 1
                queued.append(item)  # type: ignore[arg-type]

            payloads = [event.as_dict() for event in queued]
            drop_payload = self._drop_payload()
            if drop_payload is not None:
                payloads.append(drop_payload)
            if payloads:
                try:
                    self._persist_batch(self._sink, payloads)
                except Exception:
                    with self._lock:
                        self._failed_writes += len(payloads)
                    logger.warning("Unable to persist an operational metric batch")
                else:
                    with self._lock:
                        self._persisted += len(payloads)
            for _ in range(consumed):
                self._mailbox.task_done()
            if self._stop_requested.is_set() and self._mailbox.empty():
                return

    def record(self, event: MetricEvent) -> MetricEvent:
        if not isinstance(event, MetricEvent):
            raise TypeError("only MetricEvent instances can be recorded")
        if event.timestamp is None:
            timestamp = self._clock()
            if timestamp.tzinfo is None:
                raise ValueError("metric clock must return a timezone-aware datetime")
            event = replace(event, timestamp=timestamp)
        if not self.enabled:
            return event

        sink: Any | None = None
        with self._lock:
            if self._closed:
                return event
            self._accepted += 1
            if self._retain:
                self._events.append(event)
                if len(self._events) > self._retain:
                    del self._events[: len(self._events) - self._retain]
            if self._mailbox is not None:
                try:
                    self._mailbox.put_nowait(event)
                except queue.Full:
                    self._dropped_full += 1
                    dropped = self._dropped_full
                    if dropped == 1 or dropped & (dropped - 1) == 0:
                        logger.warning(
                            "Dropping operational metrics because the queue is full (total=%s)",
                            dropped,
                        )
                return event
            sink = self._sink

        if sink is not None:
            try:
                self._persist_batch(sink, [event.as_dict()])
            except Exception:
                with self._lock:
                    self._failed_writes += 1
                logger.warning("Unable to persist an operational metric")
            else:
                with self._lock:
                    self._persisted += 1
        return event

    def snapshot(self) -> tuple[MetricEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def stats(self) -> RecorderStats:
        with self._lock:
            mailbox = self._mailbox
            return RecorderStats(
                accepted=self._accepted,
                persisted=self._persisted,
                dropped_full=self._dropped_full,
                failed_writes=self._failed_writes,
                queue_depth=mailbox.qsize() if mailbox is not None else 0,
                queue_capacity=mailbox.maxsize if mailbox is not None else 0,
                closed=self._closed,
            )

    @property
    def dropped_events(self) -> int:
        return self.stats().dropped_full

    def close(self) -> bool:
        """Request a final drain and report whether the worker stopped in time."""

        with self._lock:
            self._closed = True
            mailbox = self._mailbox
            worker = self._worker
        if mailbox is None or worker is None:
            return True
        self._stop_requested.set()
        if worker is threading.current_thread():
            return False
        worker.join(timeout=self._shutdown_timeout)
        if worker.is_alive():
            logger.warning("Metric recorder worker did not stop before the shutdown deadline")
            return False
        return True


def record_safely(
    recorder: SafeMetricsRecorder | Any | None,
    event: str,
    /,
    **values: Any,
) -> MetricEvent | None:
    """Construct and record an event without changing application behavior."""

    if recorder is None:
        return None
    try:
        metric = MetricEvent(event=event, **values)
        return recorder.record(metric)
    except Exception:
        return None


__all__ = [
    "FanoutMetricSink",
    "MetricEvent",
    "RecorderStats",
    "SafeMetricsRecorder",
    "record_safely",
]
