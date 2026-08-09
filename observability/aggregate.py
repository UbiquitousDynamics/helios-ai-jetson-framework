"""Mathematically correct, empty-safe KPI aggregation queries."""

from __future__ import annotations

import json
import math
import numbers
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from observability.storage import MAX_QUERY_LIMIT, SQLiteKPIStore, timestamp_to_ms

_REQUEST_EVENTS = frozenset(
    {
        "llm_request_succeeded",
        "llm_request_failed",
    }
)
_ATTEMPT_EVENTS = frozenset({"llm_attempt_succeeded", "llm_attempt_failed"})
_VOICE_COMMAND_EVENTS = frozenset({"voice_command_completed", "voice_command_failed"})
_ROUTING_EVENTS = frozenset(
    {
        "llm_route_decided",
        "llm_route_candidate_rejected",
        *_REQUEST_EVENTS,
    }
)
_PROVIDER_EVENTS = frozenset({*_ATTEMPT_EVENTS, "provider_health_changed"})
_NETWORK_EVENTS = frozenset(
    {
        "network_probe_completed",
        "network_state_changed",
        "llm_route_decided",
        *_ATTEMPT_EVENTS,
    }
)
_LATENCY_METRICS = frozenset(
    {
        "latency_ms",
        "first_token_ms",
        "first_audio_ms",
        "actual_first_audio_ms",
        "listening_ms",
        "speech_finalization_ms",
        "stt_ms",
        "rag_ms",
        "routing_ms",
        "inference_ms",
        "tts_synthesis_ms",
        "audio_playback_ms",
        "audio_duration_ms",
        "speech_dispatch_ms",
        "end_to_end_ms",
        "dns_ms",
        "tcp_ms",
        "connect_ms",
        "tls_ms",
        "ttfb_ms",
    }
)
_LATENCY_BREAKDOWN_METRICS = (
    "listening_ms",
    "speech_finalization_ms",
    "stt_ms",
    "rag_ms",
    "routing_ms",
    "first_token_ms",
    "inference_ms",
    "tts_synthesis_ms",
    "speech_dispatch_ms",
    "actual_first_audio_ms",
    "audio_playback_ms",
    "audio_duration_ms",
)
_RESOURCE_METRICS = (
    "cpu_percent",
    "gpu_percent",
    "memory_percent",
    "swap_percent",
    "memory_used_mb",
    "memory_total_mb",
    "swap_used_mb",
    "swap_total_mb",
    "cpu_temperature_c",
    "gpu_temperature_c",
    "power_w",
    "cpu_frequency_mhz",
    "gpu_frequency_mhz",
    "storage_used_mb",
)
_NUMERIC_METRICS = _LATENCY_METRICS | frozenset(
    {
        "network_quality_score",
        "probe_success_ratio",
        "goodput_kbps",
        "cpu_percent",
        "gpu_percent",
        "memory_percent",
        "swap_percent",
        "memory_used_mb",
        "memory_total_mb",
        "swap_used_mb",
        "swap_total_mb",
        "cpu_temperature_c",
        "gpu_temperature_c",
        "power_w",
        "cpu_frequency_mhz",
        "gpu_frequency_mhz",
        "storage_used_mb",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "estimated_input_tokens",
        "estimated_output_tokens",
        "retry_count",
        "fallback_count",
        "complexity_score",
        "count",
        "dropped_count",
    }
)
_INDEXED_FILTERS = frozenset(
    {
        "event",
        "mode",
        "locality",
        "provider",
        "model",
        "model_tier",
        "route",
        "outcome",
        "error_category",
        "route_reason",
        "rejection_reason",
        "fallback_from",
        "fallback_to",
        "fallback_cause",
        "network_state",
        "network_quality_tier",
    }
)
_POST_FILTERS = frozenset(
    {
        "language",
        "success",
        "network_reason",
        "interface_available",
        "interface_kind",
        "probe_success",
        "network_forced_local",
        "speech_committed",
        "streaming",
        "throttled",
    }
)
_FILTER_ALIASES = {
    "network_tier": "network_quality_tier",
    "quality_tier": "network_quality_tier",
}
_METRIC_ALIASES = {
    "listen_ms": "listening_ms",
    "tts_ms": "tts_synthesis_ms",
    "playback_ms": "audio_playback_ms",
    "quality_score": "network_quality_score",
    "cpu_pct": "cpu_percent",
    "gpu_pct": "gpu_percent",
    "memory_pct": "memory_percent",
    "swap_pct": "swap_percent",
    "memory_mb": "memory_used_mb",
    "swap_mb": "swap_used_mb",
    "cpu_temp_c": "cpu_temperature_c",
    "gpu_temp_c": "gpu_temperature_c",
    "storage_mb": "storage_used_mb",
    "network_quality": "network_quality_score",
}
_DERIVED_SERIES = frozenset(
    {"requests", "success_rate", "fallback_rate", "error_rate", "temperature_c"}
)
_HISTOGRAM_BOUNDS_MS = (
    10.0,
    25.0,
    50.0,
    100.0,
    200.0,
    500.0,
    1_000.0,
    2_000.0,
    5_000.0,
    10_000.0,
    30_000.0,
    60_000.0,
    120_000.0,
    240_000.0,
)


def percentile_r7(values: Iterable[float], percentile: float) -> float | None:
    """Return the exact Hyndman/Fan type-7 percentile.

    Percentiles in ``[0, 1]`` are accepted as fractions; values above one and
    up to 100 are interpreted as percentages. Empty input returns ``None``.
    """

    if isinstance(percentile, bool) or not isinstance(percentile, numbers.Real):
        raise ValueError("percentile must be a finite number")
    requested = float(percentile)
    if not math.isfinite(requested) or not 0 <= requested <= 100:
        raise ValueError("percentile must be between zero and 100")
    probability = requested if requested <= 1 else requested / 100
    samples: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            raise ValueError("percentile samples must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("percentile samples must be finite numbers")
        samples.append(number)
    if not samples:
        return None
    samples.sort()
    position = (len(samples) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return samples[lower]
    fraction = position - lower
    return samples[lower] + fraction * (samples[upper] - samples[lower])


def _number(event: Mapping[str, Any], name: str) -> float | None:
    value = event.get(name)
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _metric_values(events: Iterable[Mapping[str, Any]], name: str) -> list[float]:
    values: list[float] = []
    for event in events:
        value = _number(event, name)
        if value is not None:
            values.append(value)
    return values


def _statistics(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
        }
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / len(values),
        "p50": percentile_r7(values, 50),
        "p90": percentile_r7(values, 90),
        "p95": percentile_r7(values, 95),
        "p99": percentile_r7(values, 99),
    }


def _event_name(record: Mapping[str, Any]) -> str:
    return str(record.get("event") or "").replace(".", "_")


def _succeeded(record: Mapping[str, Any]) -> bool:
    success = record.get("success")
    if isinstance(success, bool):
        return success
    outcome = str(record.get("outcome") or "").lower()
    if outcome:
        return outcome in {"ok", "success", "succeeded", "completed"}
    name = _event_name(record)
    return name.endswith(("_succeeded", "_completed")) and not name.endswith("_failed")


def _weight(record: Mapping[str, Any]) -> int:
    value = record.get("count", 1)
    return int(value) if isinstance(value, numbers.Integral) and not isinstance(value, bool) else 1


def _is_request(record: Mapping[str, Any]) -> bool:
    return _event_name(record) in _REQUEST_EVENTS


def _is_attempt(record: Mapping[str, Any]) -> bool:
    return _event_name(record) in _ATTEMPT_EVENTS


def _distribution(
    records: Iterable[Mapping[str, Any]],
    field: str,
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for record in records:
        value = record.get(field)
        if value not in (None, ""):
            counts[str(value)] += _weight(record)
    return dict(sorted(counts.items()))


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _downsample(records: Sequence[Mapping[str, Any]], points: int) -> list[Mapping[str, Any]]:
    if isinstance(points, bool) or not isinstance(points, int) or not 1 <= points <= 1_000:
        raise ValueError("points must be between one and 1000")
    ordered = sorted(records, key=lambda record: int(record.get("timestamp_ms", 0)))
    if len(ordered) <= points:
        return ordered
    if points == 1:
        return [ordered[-1]]
    return [ordered[round(index * (len(ordered) - 1) / (points - 1))] for index in range(points)]


def _event_types_for_metrics(metric_names: Iterable[str]) -> frozenset[str]:
    selected: set[str] = set()
    for name in metric_names:
        if name in _RESOURCE_METRICS or name == "temperature_c":
            selected.add("resource_sample")
        elif name in {
            "network_quality_score",
            "probe_success_ratio",
            "goodput_kbps",
            "dns_ms",
            "tcp_ms",
            "connect_ms",
            "tls_ms",
            "ttfb_ms",
        }:
            selected.update({"network_probe_completed", "network_state_changed"})
        elif name in {"latency_ms"}:
            selected.update(_ATTEMPT_EVENTS)
        elif name in {"listening_ms", "speech_finalization_ms", "stt_ms"}:
            selected.add("voice_listen_completed")
        elif name == "rag_ms":
            selected.update({"rag_completed", "rag_failed"})
        elif name in {"tts_synthesis_ms", "audio_playback_ms", "audio_duration_ms"}:
            selected.update({"tts_completed", "tts_failed", *_REQUEST_EVENTS})
        else:
            selected.update(_REQUEST_EVENTS)
    return frozenset(selected)


def _filter_match(actual: Any, expected: object) -> bool:
    values: Sequence[object]
    if isinstance(expected, Sequence) and not isinstance(expected, (str, bytes, bytearray)):
        values = expected
    else:
        values = (expected,)
    for value in values:
        if isinstance(actual, bool):
            if isinstance(value, bool) and actual is value:
                return True
            if isinstance(value, str) and value.lower() in {"true", "false"}:
                return actual is (value.lower() == "true")
        elif str(actual) == str(value):
            return True
    return False


class KPIQueryService:
    """Dashboard-oriented aggregation over raw events and count rollups."""

    def __init__(
        self,
        store: SQLiteKPIStore,
        *,
        max_events: int = MAX_QUERY_LIMIT,
        clock: Any = time.time,
    ) -> None:
        if (
            isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or not 1 <= max_events <= MAX_QUERY_LIMIT
        ):
            raise ValueError(f"max_events must be between one and {MAX_QUERY_LIMIT}")
        self.store = store
        self.max_events = max_events
        self._clock = clock

    @staticmethod
    def _split_filters(
        filters: Mapping[str, object] | None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        indexed: dict[str, object] = {}
        post: dict[str, object] = {}
        for raw_name, value in (filters or {}).items():
            name = str(raw_name).strip().lower().replace("-", "_")
            name = _FILTER_ALIASES.get(name, name)
            if name == "outcome":
                aliases = {
                    "success": "succeeded",
                    "ok": "succeeded",
                    "completed": "succeeded",
                    "failure": "failed",
                    "error": "failed",
                }
                if isinstance(value, str):
                    value = aliases.get(value.lower(), value.lower())
                elif isinstance(value, Sequence):
                    value = tuple(
                        aliases.get(str(item).lower(), str(item).lower()) for item in value
                    )
            if name == "locality" and value == "local":
                value = ("local", "device", "trusted_lan")
            if name in _INDEXED_FILTERS:
                indexed[name] = value
            elif name in _POST_FILTERS:
                post[name] = value
            else:
                raise ValueError(f"unsupported KPI filter: {raw_name}")
        return indexed, post

    def _records(
        self,
        *,
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
        event_types: Iterable[str] | None = None,
        include_rollups: bool = True,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        indexed, post = self._split_filters(filters)
        raw = self.store.query_events(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=indexed,
            event_types=event_types,
            limit=self.max_events,
            ascending=False,
        )
        if post:
            raw = [
                record
                for record in raw
                if all(_filter_match(record.get(name), value) for name, value in post.items())
            ]
        rollups: list[dict[str, Any]] = []
        if include_rollups and not post:
            rollups = self.store.query_rollups(
                start_ms=start_ms,
                end_ms=end_ms,
                filters=indexed,
                event_types=event_types,
                limit=self.max_events,
                ascending=False,
            )
        return raw, rollups

    def _truncated(
        self,
        raw: Sequence[Mapping[str, Any]],
        rollups: Sequence[Mapping[str, Any]],
    ) -> bool:
        # The store deliberately bounds reads. Hitting the bound is treated as
        # possibly truncated (including the exact-bound case) so callers never
        # mistake a partial aggregate for an exact one.
        return len(raw) >= self.max_events or len(rollups) >= self.max_events

    def summary(
        self,
        *,
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        raw, rollups = self._records(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            event_types=_REQUEST_EVENTS,
        )
        attempt_raw, attempt_rollups = self._records(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            event_types=_ATTEMPT_EVENTS,
        )
        voice_raw, voice_rollups = self._records(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            event_types=_VOICE_COMMAND_EVENTS,
        )
        all_records: list[Mapping[str, Any]] = [*raw, *rollups]
        requests = [record for record in all_records if _is_request(record)]
        if not requests:
            requests = [*attempt_raw, *attempt_rollups]
        request_count = sum(_weight(record) for record in requests)
        successes = sum(_weight(record) for record in requests if _succeeded(record))
        failures = request_count - successes
        raw_requests = [record for record in raw if _is_request(record)]
        if not raw_requests:
            raw_requests = list(attempt_raw)
        fallback_requests = sum(
            _weight(record)
            for record in requests
            if (_number(record, "fallback_count") or 0) > 0
            or record.get("fallback_from")
            or record.get("fallback_to")
            or record.get("fallback_cause")
        )
        local_requests = sum(
            _weight(record)
            for record in requests
            if record.get("locality") in {"device", "local", "trusted_lan"}
        )
        remote_requests = sum(
            _weight(record) for record in requests if record.get("locality") == "remote"
        )
        local_successes = sum(
            _weight(record)
            for record in requests
            if record.get("locality") in {"device", "local", "trusted_lan"} and _succeeded(record)
        )
        remote_successes = sum(
            _weight(record)
            for record in requests
            if record.get("locality") == "remote" and _succeeded(record)
        )
        error_categories = _distribution(requests, "error_category")
        timeout_count = sum(
            _weight(record)
            for record in requests
            if "timeout" in str(record.get("error_category") or "")
        )
        cancellation_count = sum(
            _weight(record) for record in requests if record.get("error_category") == "cancelled"
        )
        refusal_count = sum(
            _weight(record)
            for record in requests
            if record.get("error_category") == "safety_refusal"
        )
        interruption_count = sum(
            int(_number(record, "interruption_count") or 0) for record in raw_requests
        )
        voice_command_count = sum(
            _weight(record)
            for record in [*voice_raw, *voice_rollups]
            if _event_name(record).startswith("voice_command_")
            and _event_name(record).endswith(("_completed", "_failed"))
        )

        bounds = [
            int(record["timestamp_ms"])
            for record in raw
            if isinstance(record.get("timestamp_ms"), numbers.Integral)
        ]
        if start_ms is not None and end_ms is not None:
            duration_ms = max(0, self._coerce_epoch_ms(end_ms) - self._coerce_epoch_ms(start_ms))
        elif len(bounds) >= 2:
            duration_ms = max(bounds) - min(bounds)
        else:
            duration_ms = 0

        token_totals = {
            name: sum(int(value) for value in _metric_values(raw_requests, name))
            for name in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
                "estimated_input_tokens",
                "estimated_output_tokens",
            )
        }
        # Charges are attempt-scoped, including conservative settlements for
        # transmitted attempts that later fail. Terminal request events do not
        # carry cost, so summing every raw attempt avoids both omissions and
        # double counting.
        cost_records = list(attempt_raw)
        cost_totals: dict[str, str] = {}
        for name in ("cost_usd", "estimated_cost_usd", "reported_cost_usd"):
            total = Decimal(0)
            for record in cost_records:
                try:
                    total += Decimal(str(record.get(name, "0")))
                except (InvalidOperation, ValueError):
                    continue
            cost_totals[name] = format(total, "f")

        result = {
            "request_count": request_count,
            "success_count": successes,
            "failure_count": failures,
            "success_rate": _ratio(successes, request_count),
            "error_rate": _ratio(failures, request_count),
            "fallback_request_count": fallback_requests,
            "fallback_rate": _ratio(fallback_requests, request_count),
            "requests_per_minute": (
                request_count / (duration_ms / 60_000) if duration_ms > 0 else 0.0
            ),
            "local_request_count": local_requests,
            "remote_request_count": remote_requests,
            "local_share": _ratio(local_requests, local_requests + remote_requests),
            "remote_share": _ratio(remote_requests, local_requests + remote_requests),
            "local_success_rate": _ratio(local_successes, local_requests),
            "remote_success_rate": _ratio(remote_successes, remote_requests),
            "error_categories": error_categories,
            "timeout_count": timeout_count,
            "cancellation_count": cancellation_count,
            "refusal_count": refusal_count,
            "interruption_count": interruption_count,
            "voice_command_count": voice_command_count,
            "latency_ms": _statistics(_metric_values(raw_requests, "latency_ms")),
            "end_to_end_ms": _statistics(_metric_values(raw_requests, "end_to_end_ms")),
            "first_token_ms": _statistics(_metric_values(raw_requests, "first_token_ms")),
            "first_audio_ms": _statistics(_metric_values(raw_requests, "first_audio_ms")),
            "actual_first_audio_ms": _statistics(
                _metric_values(raw_requests, "actual_first_audio_ms")
            ),
            "latency_by_locality": {
                locality: _statistics(
                    _metric_values(
                        [record for record in raw_requests if record.get("locality") == locality],
                        "end_to_end_ms",
                    )
                )
                for locality in sorted(
                    {str(record["locality"]) for record in raw_requests if record.get("locality")}
                )
            },
            "latency_by_mode": {
                mode: _statistics(
                    _metric_values(
                        [record for record in raw_requests if record.get("mode") == mode],
                        "end_to_end_ms",
                    )
                )
                for mode in sorted(
                    {str(record["mode"]) for record in raw_requests if record.get("mode")}
                )
            },
            "tokens": token_totals,
            "cost_usd": cost_totals,
        }
        if any(
            (
                self._truncated(raw, rollups),
                self._truncated(attempt_raw, attempt_rollups),
                self._truncated(voice_raw, voice_rollups),
            )
        ):
            result["truncated"] = True
        return result

    def timeseries(
        self,
        *,
        metrics: Iterable[str] | str = ("end_to_end_ms",),
        interval_seconds: int = 60,
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, int)
            or interval_seconds < 1
        ):
            raise ValueError("interval_seconds must be a positive integer")
        if isinstance(metrics, str):
            metric_names = tuple(
                _METRIC_ALIASES.get(name.strip(), name.strip())
                for name in metrics.split(",")
                if name.strip()
            )
        else:
            metric_names = tuple(_METRIC_ALIASES.get(name, name) for name in metrics)
        if (
            not metric_names
            or len(metric_names) > 8
            or any(name not in _NUMERIC_METRICS | _DERIVED_SERIES for name in metric_names)
        ):
            raise ValueError("timeseries metrics must contain one to eight supported names")
        raw, rollups = self._records(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            event_types=_event_types_for_metrics(metric_names),
        )
        interval_ms = interval_seconds * 1_000
        buckets: dict[int, dict[str, Any]] = {}

        def bucket(timestamp_ms: int) -> dict[str, Any]:
            start = (timestamp_ms // interval_ms) * interval_ms
            return buckets.setdefault(
                start,
                {
                    "timestamp_ms": start,
                    "request_count": 0,
                    "success_count": 0,
                    "fallback_count": 0,
                    "values": defaultdict(list),
                },
            )

        for record in raw:
            timestamp = record.get("timestamp_ms")
            if not isinstance(timestamp, numbers.Integral):
                continue
            point = bucket(int(timestamp))
            if _is_request(record):
                point["request_count"] += 1
                if _succeeded(record):
                    point["success_count"] += 1
                if (_number(record, "fallback_count") or 0) > 0:
                    point["fallback_count"] += 1
            for name in metric_names:
                if name == "temperature_c":
                    temperatures = tuple(
                        value
                        for value in (
                            _number(record, "cpu_temperature_c"),
                            _number(record, "gpu_temperature_c"),
                        )
                        if value is not None
                    )
                    if temperatures:
                        point["values"][name].append(max(temperatures))
                    continue
                if name in _DERIVED_SERIES:
                    continue
                value = _number(record, name)
                if value is not None:
                    point["values"][name].append(value)
        for record in rollups:
            if not _is_request(record):
                continue
            point = bucket(int(record["bucket_ms"]))
            count = _weight(record)
            point["request_count"] += count
            if _succeeded(record):
                point["success_count"] += count
            if (
                record.get("fallback_from")
                or record.get("fallback_to")
                or record.get("fallback_cause")
            ):
                point["fallback_count"] += count

        points: list[dict[str, Any]] = []
        for timestamp in sorted(buckets):
            source = buckets[timestamp]
            derived_values = {
                "requests": [float(source["request_count"])],
                "success_rate": [_ratio(source["success_count"], source["request_count"])],
                "fallback_rate": [_ratio(source["fallback_count"], source["request_count"])],
                "error_rate": [
                    _ratio(
                        source["request_count"] - source["success_count"],
                        source["request_count"],
                    )
                ],
            }
            point = {
                "timestamp_ms": timestamp,
                "request_count": source["request_count"],
                "success_count": source["success_count"],
                "success_rate": _ratio(source["success_count"], source["request_count"]),
                "metrics": {
                    name: _statistics(derived_values.get(name, source["values"].get(name, [])))
                    for name in metric_names
                },
            }
            if len(metric_names) == 1:
                point["metric"] = metric_names[0]
                point["value"] = point["metrics"][metric_names[0]]["mean"]
            points.append(point)
        result: dict[str, Any] = {"interval_seconds": interval_seconds, "points": points}
        if self._truncated(raw, rollups):
            result["truncated"] = True
        return result

    def latency(
        self,
        *,
        metric: str = "end_to_end_ms",
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        metric = _METRIC_ALIASES.get(metric, metric)
        if metric not in _LATENCY_METRICS:
            raise ValueError(f"unsupported latency metric: {metric}")
        raw, _rollups = self._records(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            event_types=_event_types_for_metrics((metric, *_LATENCY_BREAKDOWN_METRICS)),
            include_rollups=False,
        )
        values = _metric_values(raw, metric)
        counts = [0] * (len(_HISTOGRAM_BOUNDS_MS) + 1)
        for value in values:
            for index, bound in enumerate(_HISTOGRAM_BOUNDS_MS):
                if value <= bound:
                    counts[index] += 1
                    break
            else:
                counts[-1] += 1
        histogram = [
            {"upper_bound_ms": bound, "count": counts[index]}
            for index, bound in enumerate(_HISTOGRAM_BOUNDS_MS)
        ]
        histogram.append({"upper_bound_ms": None, "count": counts[-1]})
        result: dict[str, Any] = {
            "metric": metric,
            "statistics": _statistics(values),
            "histogram": histogram,
            "breakdown": {
                name: _statistics(_metric_values(raw, name)) for name in _LATENCY_BREAKDOWN_METRICS
            },
        }
        if len(raw) >= self.max_events:
            result["truncated"] = True
        return result

    def routing(
        self,
        *,
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        raw, rollups = self._records(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            event_types=_ROUTING_EVENTS,
        )
        records: list[Mapping[str, Any]] = [*raw, *rollups]
        decisions = [record for record in records if _event_name(record) == "llm_route_decided"]
        selected = decisions or [record for record in records if _is_request(record)]
        rejected = [
            record for record in records if _event_name(record) == "llm_route_candidate_rejected"
        ]
        fallback_records = [
            record for record in records if record.get("fallback_from") or record.get("fallback_to")
        ]
        paths: Counter[str] = Counter()
        for record in fallback_records:
            source = record.get("fallback_from") or "unknown"
            destination = record.get("fallback_to") or "unknown"
            paths[f"{source}->{destination}"] += _weight(record)
        forced_local = sum(
            _weight(record) for record in selected if record.get("network_forced_local") is True
        )
        result = {
            "locality": _distribution(selected, "locality"),
            "routes": _distribution(selected, "route"),
            "model_tiers": _distribution(selected, "model_tier"),
            "routing_reasons": _distribution(selected, "route_reason"),
            "rejection_reasons": _distribution(rejected, "rejection_reason"),
            "fallback_paths": dict(sorted(paths.items())),
            "fallback_causes": _distribution(fallback_records, "fallback_cause"),
            "complexity_scores": _distribution(selected, "complexity_score"),
            "network_forced_local_count": forced_local,
        }
        if self._truncated(raw, rollups):
            result["truncated"] = True
        return result

    def providers(
        self,
        *,
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        raw, rollups = self._records(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            event_types=_PROVIDER_EVENTS,
        )
        raw_attempts = [record for record in raw if _is_attempt(record)]
        attempts: list[Mapping[str, Any]] = [
            *raw_attempts,
            *(record for record in rollups if _is_attempt(record)),
        ]
        groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for record in attempts:
            groups[
                (str(record.get("provider") or "unknown"), str(record.get("model") or "unknown"))
            ].append(record)
        health_events = [
            record for record in raw if _event_name(record) == "provider_health_changed"
        ]
        for record in health_events:
            groups.setdefault(
                (
                    str(record.get("provider") or "unknown"),
                    str(record.get("model") or "unknown"),
                ),
                [],
            )
        result: list[dict[str, Any]] = []
        for (provider, model), records in sorted(groups.items()):
            count = sum(_weight(record) for record in records)
            successes = sum(_weight(record) for record in records if _succeeded(record))
            localities = {
                str(record.get("locality"))
                for record in records
                if record.get("locality") in {"local", "remote"}
            }
            raw_group = [
                record
                for record in raw_attempts
                if (str(record.get("provider") or "unknown"), str(record.get("model") or "unknown"))
                == (provider, model)
            ]
            latest_health = max(
                (
                    record
                    for record in health_events
                    if str(record.get("provider") or "unknown") == provider
                    and str(record.get("model") or "unknown") == model
                ),
                key=lambda record: int(record.get("timestamp_ms", 0)),
                default=None,
            )
            result.append(
                {
                    "provider": provider,
                    "model": model,
                    "locality": next(iter(localities)) if len(localities) == 1 else None,
                    "attempt_count": count,
                    "success_count": successes,
                    "failure_count": count - successes,
                    "success_rate": _ratio(successes, count),
                    "errors": _distribution(records, "error_category"),
                    "latency_ms": _statistics(_metric_values(raw_group, "latency_ms")),
                    "circuit": (
                        {
                            "state": latest_health.get("circuit_state"),
                            "previous_state": latest_health.get("previous_circuit_state"),
                            "timestamp_ms": latest_health.get("timestamp_ms"),
                        }
                        if latest_health is not None
                        else None
                    ),
                }
            )
        response: dict[str, Any] = {"providers": result}
        if self._truncated(raw, rollups):
            response["truncated"] = True
        return response

    def network(
        self,
        *,
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
        points: int = 300,
    ) -> dict[str, Any]:
        raw, rollups = self._records(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            event_types=_NETWORK_EVENTS,
        )
        network_raw = [
            record
            for record in raw
            if record.get("network_state") is not None
            or record.get("network_quality_score") is not None
        ]
        observations = [
            record
            for record in network_raw
            if _event_name(record) in {"network_probe_completed", "network_state_changed"}
        ]
        # Older databases may contain only route-level network annotations.
        # Prefer real connectivity observations, but keep that data usable.
        samples = observations or network_raw
        latest = max(samples, key=lambda record: int(record.get("timestamp_ms", 0)), default=None)
        transitions = sum(
            _weight(record)
            for record in [*raw, *rollups]
            if _event_name(record) == "network_state_changed"
        )
        remote_attempts = [
            record
            for record in [*raw, *rollups]
            if _is_attempt(record) and record.get("locality") == "remote"
        ]
        tier_results: dict[str, dict[str, int | float]] = {}
        for tier in sorted(
            {
                str(record["network_quality_tier"])
                for record in remote_attempts
                if record.get("network_quality_tier")
            }
        ):
            selected = [
                record for record in remote_attempts if record.get("network_quality_tier") == tier
            ]
            count = sum(_weight(record) for record in selected)
            succeeded = sum(_weight(record) for record in selected if _succeeded(record))
            tier_results[tier] = {
                "attempt_count": count,
                "success_count": succeeded,
                "failure_count": count - succeeded,
                "success_rate": _ratio(succeeded, count),
                "latency_ms": _statistics(_metric_values(selected, "latency_ms")),
                "errors": _distribution(selected, "error_category"),
            }
        current = None
        if latest is not None:
            current = {
                name: latest.get(name)
                for name in (
                    "timestamp_ms",
                    "network_state",
                    "network_quality_tier",
                    "network_quality_score",
                    "network_reason",
                    "interface_available",
                    "interface_kind",
                    "probe_success",
                    "probe_success_ratio",
                    "dns_ms",
                    "tcp_ms",
                    "connect_ms",
                    "tls_ms",
                    "ttfb_ms",
                    "goodput_kbps",
                )
            }
        series = []
        for record in _downsample(samples, points):
            series.append(
                {
                    "timestamp_ms": record.get("timestamp_ms"),
                    "network_state": record.get("network_state"),
                    "network_quality_tier": record.get("network_quality_tier"),
                    "network_quality_score": record.get("network_quality_score"),
                    "ttfb_ms": record.get("ttfb_ms"),
                    "goodput_kbps": record.get("goodput_kbps"),
                    "probe_success_ratio": record.get("probe_success_ratio"),
                }
            )
        result = {
            "current": current,
            "series": series,
            "transition_count": transitions,
            "states": _distribution(samples, "network_state"),
            "quality_tiers": _distribution(samples, "network_quality_tier"),
            "quality_score": _statistics(_metric_values(samples, "network_quality_score")),
            "ttfb_ms": _statistics(_metric_values(samples, "ttfb_ms")),
            "goodput_kbps": _statistics(_metric_values(samples, "goodput_kbps")),
            "probe_success_ratio": _statistics(_metric_values(samples, "probe_success_ratio")),
            "routing_decisions_by_quality_tier": _distribution(
                [
                    record
                    for record in [*raw, *rollups]
                    if _event_name(record) == "llm_route_decided"
                ],
                "network_quality_tier",
            ),
            "remote_success_by_quality_tier": tier_results,
        }
        if self._truncated(raw, rollups):
            result["truncated"] = True
        return result

    def resources(
        self,
        *,
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
        points: int = 300,
    ) -> dict[str, Any]:
        raw, _rollups = self._records(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            event_types=("resource_sample",),
            include_rollups=False,
        )
        latest = max(raw, key=lambda record: int(record.get("timestamp_ms", 0)), default=None)
        current = None
        if latest is not None:
            current = {
                "timestamp_ms": latest.get("timestamp_ms"),
                "locality": latest.get("locality"),
                "mode": latest.get("mode"),
                "provider": latest.get("provider"),
                "model": latest.get("model"),
                "throttled": latest.get("throttled"),
                **{name: latest.get(name) for name in _RESOURCE_METRICS},
            }
        by_locality: dict[str, dict[str, Any]] = {}
        for locality in sorted(
            {str(record["locality"]) for record in raw if record.get("locality")}
        ):
            selected = [record for record in raw if record.get("locality") == locality]
            by_locality[locality] = {
                name: _statistics(_metric_values(selected, name)) for name in _RESOURCE_METRICS
            }
        series = [
            {
                "timestamp_ms": record.get("timestamp_ms"),
                "locality": record.get("locality"),
                "mode": record.get("mode"),
                "throttled": record.get("throttled"),
                **{name: record.get(name) for name in _RESOURCE_METRICS},
            }
            for record in _downsample(raw, points)
        ]
        result = {
            "current": current,
            "series": series,
            "sample_count": len(raw),
            "metrics": {name: _statistics(_metric_values(raw, name)) for name in _RESOURCE_METRICS},
            "by_locality": by_locality,
            "throttled_sample_count": sum(record.get("throttled") is True for record in raw),
        }
        if len(raw) >= self.max_events:
            result["truncated"] = True
        return result

    @staticmethod
    def _coerce_epoch_ms(value: Any) -> int:
        if isinstance(value, str) and value.isdigit():
            return int(value)
        if isinstance(value, numbers.Real) and not isinstance(value, bool):
            return int(value)
        return timestamp_to_ms(value)

    def _parameters(self, parameters: Mapping[str, object]) -> dict[str, Any]:
        values = dict(parameters)
        nested_filters = values.pop("filters", None)
        if nested_filters is not None and not isinstance(nested_filters, Mapping):
            raise ValueError("filters must be a mapping")
        filters = dict(nested_filters or {})
        for name in tuple(values):
            normalized = str(name).strip().lower().replace("-", "_")
            normalized = _FILTER_ALIASES.get(normalized, normalized)
            if normalized in _INDEXED_FILTERS | _POST_FILTERS:
                filters[normalized] = values.pop(name)
        if "from" in values:
            values["start_ms"] = values.pop("from")
        if "to" in values:
            values["end_ms"] = values.pop("to")
        if "start" in values:
            values["start_ms"] = values.pop("start")
        if "end" in values:
            values["end_ms"] = values.pop("end")
        window = values.pop("window", None)
        window_seconds = values.pop("window_seconds", None)
        if window is not None and window_seconds is not None:
            raise ValueError("window and window_seconds cannot both be supplied")
        if window_seconds is not None:
            if (
                isinstance(window_seconds, bool)
                or not isinstance(window_seconds, numbers.Integral)
                or not 1 <= int(window_seconds) <= 90 * 86_400
            ):
                raise ValueError("window_seconds must be between one second and 90 days")
            if "start_ms" in values or "end_ms" in values:
                raise ValueError("window_seconds cannot be combined with explicit time bounds")
            now_ms = round(float(self._clock()) * 1_000)
            values["end_ms"] = now_ms
            values["start_ms"] = now_ms - int(window_seconds) * 1_000
        if window is not None:
            if "start_ms" in values or "end_ms" in values:
                raise ValueError("window cannot be combined with explicit time bounds")
            match = re_full_window(str(window))
            now_ms = round(float(self._clock()) * 1_000)
            values["end_ms"] = now_ms
            values["start_ms"] = now_ms - match
        for name in ("start_ms", "end_ms"):
            if name in values:
                values[name] = self._coerce_epoch_ms(values[name])
        if filters:
            values["filters"] = filters
        return values

    def query(self, resource: str, parameters: Mapping[str, object] | None = None) -> Any:
        """Dispatch a versioned dashboard resource to a strict query method."""

        name = resource.strip().lower().replace("-", "_").strip("/")
        if name.startswith("api/v1/kpi/"):
            name = name.removeprefix("api/v1/kpi/")
        parameters = dict(parameters or {})
        if name in {"health", "status"}:
            if parameters:
                raise ValueError("health does not accept query parameters")
            return self.store.status()
        if name == "export":
            values = self._parameters(parameters)
            export_format = str(values.pop("format", "json")).lower()
            if export_format == "json":
                return json.loads(self.store.export_json(**values))
            if export_format == "csv":
                return self.store.export_csv(**values)
            raise ValueError("export format must be json or csv")
        methods = {
            "summary": self.summary,
            "timeseries": self.timeseries,
            "latency": self.latency,
            "routing": self.routing,
            "providers": self.providers,
            "provider_health": self.providers,
            "network": self.network,
            "resources": self.resources,
        }
        try:
            method = methods[name]
        except KeyError:
            raise ValueError(f"unknown KPI resource: {resource}") from None
        values = self._parameters(parameters)
        points = values.pop("points", None)
        limit = values.pop("limit", None)
        if name == "timeseries":
            if "metric" in values and "metrics" not in values:
                values["metrics"] = values.pop("metric")
            if "interval" in values and "interval_seconds" not in values:
                values["interval_seconds"] = int(values.pop("interval"))
            if points is not None and "interval_seconds" not in values:
                start = values.get("start_ms")
                end = values.get("end_ms")
                if isinstance(start, int) and isinstance(end, int):
                    values["interval_seconds"] = max(
                        1, math.ceil((end - start) / 1_000 / int(points))
                    )
        elif name in {"network", "resources"} and points is not None:
            values["points"] = int(points)
        result = method(**values)
        if name in {"providers", "provider_health"} and limit is not None:
            providers = result.get("providers", [])
            if len(providers) > int(limit):
                result = {
                    **result,
                    "providers": providers[: int(limit)],
                    "truncated": True,
                }
        return result


def re_full_window(value: str) -> int:
    """Parse a small, bounded dashboard window into milliseconds."""

    if len(value) < 2 or not value[:-1].isdigit() or value[-1] not in "mhd":
        raise ValueError("window must use a positive m, h, or d suffix")
    amount = int(value[:-1])
    if amount < 1:
        raise ValueError("window must be positive")
    factors = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    duration = amount * factors[value[-1]]
    if duration > 90 * 86_400_000:
        raise ValueError("window cannot exceed 90 days")
    return duration


__all__ = ["KPIQueryService", "percentile_r7", "re_full_window"]
