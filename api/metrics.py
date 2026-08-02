"""Content-safe, provider-neutral inference metrics."""

from __future__ import annotations

import logging
import math
import numbers
import queue
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

from api.providers.contracts import ErrorCategory, Usage

logger = logging.getLogger(__name__)
_STOP = object()


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


def _milliseconds(value: float | None, name: str) -> float | None:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, numbers.Real)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be finite and non-negative")
    return value


@dataclass(frozen=True, slots=True)
class MetricEvent:
    """A deliberately closed schema with no prompt or response fields."""

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

    def __post_init__(self) -> None:
        if not isinstance(self.event, str):
            raise TypeError("event must be a string")
        for name in (
            "event",
            "provider",
            "model",
            "mode",
            "language",
            "route_reason",
            "request_id",
            "attempt_id",
        ):
            _safe_label(getattr(self, name), name)
        if self.timestamp is not None and self.timestamp.tzinfo is None:
            raise ValueError("metric timestamp must be timezone-aware")
        for name in ("latency_ms", "first_token_ms", "first_audio_ms"):
            _milliseconds(getattr(self, name), name)
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "remaining_requests",
            "remaining_tokens",
            "fallback_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value is None and name == "fallback_count":
                raise ValueError(f"{name} must be a non-negative integer")
            if value is not None and (not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.cost_usd is not None:
            if not isinstance(self.cost_usd, Decimal):
                raise TypeError("cost_usd must be a Decimal")
            if not self.cost_usd.is_finite() or self.cost_usd < 0:
                raise ValueError("cost_usd must be finite and non-negative")
        if self.error_category is not None and not isinstance(self.error_category, ErrorCategory):
            raise TypeError("error_category must be an ErrorCategory")

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
        if payload["cost_usd"] is not None:
            payload["cost_usd"] = str(payload["cost_usd"])
        if payload["error_category"] is not None:
            payload["error_category"] = payload["error_category"].value
        return payload


class SafeMetricsRecorder:
    """Thread-safe recorder with an optional content-free event sink."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        sink: Callable[[Mapping[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        retain: int = 1000,
        asynchronous: bool = False,
        queue_size: int = 256,
    ) -> None:
        if isinstance(retain, bool) or not isinstance(retain, int) or retain < 0:
            raise ValueError("retain must be a non-negative integer")
        if not isinstance(asynchronous, bool):
            raise TypeError("asynchronous must be a boolean")
        if isinstance(queue_size, bool) or not isinstance(queue_size, int) or queue_size < 1:
            raise ValueError("queue_size must be a positive integer")
        self.enabled = enabled
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._retain = retain
        self._events: list[MetricEvent] = []
        self._lock = threading.RLock()
        self._closed = False
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

    def _consume(self) -> None:
        assert self._mailbox is not None
        assert self._sink is not None
        while True:
            payload = self._mailbox.get()
            try:
                if payload is _STOP:
                    return
                self._sink(payload)  # type: ignore[arg-type]
            except Exception:
                logger.warning("Unable to persist an inference metric")
            finally:
                self._mailbox.task_done()

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
        payload = event.as_dict()
        with self._lock:
            if self._closed:
                return event
            if self._retain:
                self._events.append(event)
                if len(self._events) > self._retain:
                    del self._events[: len(self._events) - self._retain]
            if self._mailbox is not None:
                try:
                    self._mailbox.put_nowait(payload)
                except queue.Full:
                    logger.warning("Dropping an inference metric because the queue is full")
            elif self._sink is not None:
                self._sink(payload)
        return event

    def snapshot(self) -> tuple[MetricEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            mailbox = self._mailbox
            worker = self._worker
        if mailbox is not None and worker is not None:
            mailbox.put(_STOP)
            worker.join()


__all__ = ["MetricEvent", "SafeMetricsRecorder"]
