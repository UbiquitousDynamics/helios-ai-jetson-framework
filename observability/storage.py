"""Content-safe SQLite persistence for Helios KPI events.

The writer accepts only a closed set of scalar fields.  Conversation content,
network addresses, transport headers, credentials, and provider correlation
identifiers are never serialized.  The class is deliberately synchronous: it
is intended to be called by the bounded recorder's background worker, while
dashboard readers use independent short-lived connections.
"""

from __future__ import annotations

import csv
import ipaddress
import io
import json
import logging
import math
import numbers
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LATEST_SCHEMA_VERSION = 3
DEFAULT_QUERY_LIMIT = 10_000
MAX_QUERY_LIMIT = 100_000
_DAY_MS = 86_400_000
_LABEL_PATTERN = re.compile(r"^[^\s\x00-\x1f\x7f]{1,200}$")


class KPIStorageError(RuntimeError):
    """Raised when KPI storage cannot be initialized or queried safely."""


class EventSanitizationError(ValueError):
    """Raised when an event does not fit the content-free KPI schema."""


_OMITTED_FIELDS = frozenset({"request_id", "attempt_id"})
_FORBIDDEN_FIELDS = frozenset(
    {
        "answer",
        "answers",
        "api_key",
        "authorization",
        "content",
        "context",
        "cookie",
        "credential",
        "credentials",
        "header",
        "headers",
        "interface",
        "interface_name",
        "ip",
        "ip_address",
        "message",
        "password",
        "prompt",
        "prompts",
        "rag_context",
        "response",
        "responses",
        "secret",
        "transcript",
        "transcripts",
        "uri",
        "url",
    }
)

_LABEL_FIELDS = frozenset(
    {
        "event",
        "mode",
        "locality",
        "provider",
        "model",
        "resolved_model",
        "model_tier",
        "route",
        "language",
        "route_reason",
        "outcome",
        "error_category",
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
    }
)
_BOOLEAN_FIELDS = frozenset(
    {
        "success",
        "interface_available",
        "probe_success",
        "network_forced_local",
        "speech_committed",
        "streaming",
        "throttled",
    }
)
_INTEGER_FIELDS = frozenset(
    {
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "estimated_input_tokens",
        "estimated_output_tokens",
        "remaining_requests",
        "remaining_tokens",
        "fallback_count",
        "retry_count",
        "complexity_score",
        "count",
        "dropped_count",
        "wake_word_count",
        "recognized_count",
        "cancellation_count",
        "interruption_count",
    }
)
_FLOAT_FIELDS = frozenset(
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
        "streaming_lead_ms",
        "dns_ms",
        "tcp_ms",
        "connect_ms",
        "tls_ms",
        "ttfb_ms",
        "goodput_kbps",
        "network_quality_score",
        "probe_success_ratio",
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
    }
)
_PERCENT_FIELDS = frozenset({"cpu_percent", "gpu_percent", "memory_percent", "swap_percent"})
_RATIO_FIELDS = frozenset({"network_quality_score", "probe_success_ratio"})
_TEMPERATURE_FIELDS = frozenset({"cpu_temperature_c", "gpu_temperature_c"})
_COST_FIELDS = frozenset({"cost_usd", "estimated_cost_usd", "reported_cost_usd"})
_ALIASES = {
    "event_type": "event",
    "network_tier": "network_quality_tier",
    "quality_tier": "network_quality_tier",
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
    "listen_ms": "listening_ms",
    "tts_ms": "tts_synthesis_ms",
    "playback_ms": "audio_playback_ms",
}
_OUTCOME_ALIASES = {
    "ok": "succeeded",
    "success": "succeeded",
    "completed": "succeeded",
    "failure": "failed",
    "error": "failed",
}
_ALLOWED_INPUT_FIELDS = (
    _LABEL_FIELDS
    | _BOOLEAN_FIELDS
    | _INTEGER_FIELDS
    | _FLOAT_FIELDS
    | _COST_FIELDS
    | _OMITTED_FIELDS
    | frozenset({"timestamp", "timestamp_ms"})
    | frozenset(_ALIASES)
)

_DIMENSION_FIELDS = (
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
)
_FILTER_FIELDS = frozenset({"event", *_DIMENSION_FIELDS})

_EXPORT_FIELDS = (
    "timestamp",
    "event",
    "mode",
    "locality",
    "provider",
    "model",
    "resolved_model",
    "model_tier",
    "route",
    "language",
    "outcome",
    "success",
    "error_category",
    "route_reason",
    "rejection_reason",
    "fallback_from",
    "fallback_to",
    "fallback_cause",
    "retry_count",
    "fallback_count",
    "complexity_score",
    "network_state",
    "network_quality_tier",
    "network_reason",
    "network_quality_score",
    "interface_available",
    "interface_kind",
    "probe_success",
    "probe_success_ratio",
    "network_forced_local",
    "speech_committed",
    "streaming",
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
    "streaming_lead_ms",
    "dns_ms",
    "tcp_ms",
    "connect_ms",
    "tls_ms",
    "ttfb_ms",
    "goodput_kbps",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "estimated_input_tokens",
    "estimated_output_tokens",
    "cost_usd",
    "estimated_cost_usd",
    "reported_cost_usd",
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
    "throttled",
    "storage_used_mb",
    "count",
    "dropped_count",
    "wake_word_count",
    "recognized_count",
    "cancellation_count",
    "interruption_count",
)


def _normalized_key(value: object) -> str:
    return str(value).strip().lower().replace("-", "_")


def _event_mapping(event: object) -> Mapping[str, Any]:
    if isinstance(event, Mapping):
        return event
    as_dict_method = getattr(event, "as_dict", None)
    if callable(as_dict_method):
        value = as_dict_method()
        if isinstance(value, Mapping):
            return value
    if is_dataclass(event) and not isinstance(event, type):
        value = asdict(event)
        if isinstance(value, Mapping):
            return value
    raise EventSanitizationError("KPI event must be a mapping or an immutable event object")


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _label(value: Any, name: str) -> str:
    value = _enum_value(value)
    if not isinstance(value, str) or not _LABEL_PATTERN.fullmatch(value):
        raise EventSanitizationError(f"{name} must be a machine-readable label")
    if "://" in value:
        raise EventSanitizationError(f"{name} must not contain a URL")
    try:
        ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        pass
    else:
        raise EventSanitizationError(f"{name} must not contain an IP address")
    return value


def _nonnegative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, numbers.Integral) or value < 0:
        raise EventSanitizationError(f"{name} must be a non-negative integer")
    return int(value)


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise EventSanitizationError(f"{name} must be a finite number")
    number = float(value)
    minimum = -273.15 if name in _TEMPERATURE_FIELDS else 0.0
    if not math.isfinite(number) or number < minimum:
        raise EventSanitizationError(f"{name} must be finite and at least {minimum:g}")
    if name in _PERCENT_FIELDS and number > 100:
        raise EventSanitizationError(f"{name} must be between zero and 100")
    if name in _RATIO_FIELDS and number > 1:
        raise EventSanitizationError(f"{name} must be between zero and one")
    return number


def _decimal_string(value: Any, name: str) -> str:
    if isinstance(value, bool):
        raise EventSanitizationError(f"{name} must be a non-negative decimal")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise EventSanitizationError(f"{name} must be a non-negative decimal") from None
    if not decimal_value.is_finite() or decimal_value < 0:
        raise EventSanitizationError(f"{name} must be finite and non-negative")
    return format(decimal_value, "f")


def timestamp_to_ms(value: Any, *, default_now: bool = False) -> int:
    """Convert a timezone-aware timestamp or epoch value to UTC milliseconds."""

    if value is None:
        if not default_now:
            raise EventSanitizationError("timestamp is required")
        return time.time_ns() // 1_000_000
    if isinstance(value, bool):
        raise EventSanitizationError("timestamp must not be a boolean")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise EventSanitizationError("timestamp must be timezone-aware")
        seconds = value.astimezone(timezone.utc).timestamp()
    elif isinstance(value, numbers.Real):
        seconds = float(value)
        if abs(seconds) >= 100_000_000_000:
            seconds /= 1_000
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise EventSanitizationError("timestamp is not valid ISO-8601") from None
        if parsed.tzinfo is None:
            raise EventSanitizationError("timestamp must be timezone-aware")
        seconds = parsed.astimezone(timezone.utc).timestamp()
    else:
        raise EventSanitizationError("timestamp has an unsupported type")
    if not math.isfinite(seconds) or seconds < 0:
        raise EventSanitizationError("timestamp must be finite and non-negative")
    milliseconds = round(seconds * 1_000)
    if milliseconds > 253_402_300_799_999:
        raise EventSanitizationError("timestamp is outside the supported range")
    return milliseconds


def _explicit_timestamp_ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise EventSanitizationError("timestamp_ms must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > 253_402_300_799_999:
        raise EventSanitizationError("timestamp_ms is outside the supported range")
    return round(number)


def _query_timestamp_ms(value: Any) -> int:
    return (
        _explicit_timestamp_ms(value) if isinstance(value, numbers.Real) else timestamp_to_ms(value)
    )


def _timestamp_text(milliseconds: int) -> str:
    value = datetime.fromtimestamp(milliseconds / 1_000, tz=timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sanitize_event(event: object) -> dict[str, Any]:
    """Return a strict content-free event suitable for persistence.

    Unknown fields fail closed. Provider request and attempt identifiers are
    intentionally ignored, allowing existing ``MetricEvent`` instances to be
    persisted without retaining high-cardinality external identifiers.
    """

    source = _event_mapping(event)
    normalized: dict[str, Any] = {}
    for raw_name, value in source.items():
        name = _normalized_key(raw_name)
        if name in _FORBIDDEN_FIELDS:
            raise EventSanitizationError(f"sensitive field {name!r} is forbidden")
        if name in _OMITTED_FIELDS:
            continue
        if name not in _ALLOWED_INPUT_FIELDS:
            raise EventSanitizationError(f"unknown KPI field {name!r}")
        name = _ALIASES.get(name, name)
        if name in normalized and normalized[name] != value:
            raise EventSanitizationError(f"conflicting values for KPI field {name!r}")
        normalized[name] = value

    if "event" not in normalized:
        raise EventSanitizationError("event is required")

    timestamp_value = normalized.pop("timestamp", None)
    timestamp_ms_value = normalized.pop("timestamp_ms", None)
    if timestamp_value is not None:
        timestamp_ms = timestamp_to_ms(timestamp_value)
    elif timestamp_ms_value is not None:
        timestamp_ms = _explicit_timestamp_ms(timestamp_ms_value)
    else:
        timestamp_ms = timestamp_to_ms(None, default_now=True)
    if timestamp_value is not None and timestamp_ms_value is not None:
        supplied_ms = _explicit_timestamp_ms(timestamp_ms_value)
        if abs(timestamp_ms - supplied_ms) > 1:
            raise EventSanitizationError("timestamp and timestamp_ms disagree")

    result: dict[str, Any] = {
        "timestamp": _timestamp_text(timestamp_ms),
        "timestamp_ms": timestamp_ms,
    }
    for name, value in normalized.items():
        if value is None:
            continue
        if name in _LABEL_FIELDS:
            result[name] = _label(value, name)
            if name == "outcome":
                result[name] = _OUTCOME_ALIASES.get(result[name].lower(), result[name].lower())
        elif name in _BOOLEAN_FIELDS:
            if not isinstance(value, bool):
                raise EventSanitizationError(f"{name} must be a boolean")
            result[name] = value
        elif name in _INTEGER_FIELDS:
            result[name] = _nonnegative_integer(value, name)
        elif name in _FLOAT_FIELDS:
            result[name] = _finite_number(value, name)
        elif name in _COST_FIELDS:
            result[name] = _decimal_string(value, name)
        else:  # pragma: no cover - protected by the closed sets above
            raise EventSanitizationError(f"unsupported KPI field {name!r}")
    return result


_MIGRATION_1 = (
    """
    CREATE TABLE IF NOT EXISTS kpi_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS kpi_events (
        id INTEGER PRIMARY KEY,
        timestamp_ms INTEGER NOT NULL,
        event TEXT NOT NULL,
        mode TEXT,
        provider TEXT,
        model TEXT,
        payload TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_kpi_events_time ON kpi_events(timestamp_ms)",
    "CREATE INDEX IF NOT EXISTS idx_kpi_events_type_time ON kpi_events(event, timestamp_ms)",
)

_MIGRATION_2 = (
    "ALTER TABLE kpi_events ADD COLUMN locality TEXT",
    "ALTER TABLE kpi_events ADD COLUMN model_tier TEXT",
    "ALTER TABLE kpi_events ADD COLUMN route TEXT",
    "ALTER TABLE kpi_events ADD COLUMN outcome TEXT",
    "ALTER TABLE kpi_events ADD COLUMN error_category TEXT",
    "ALTER TABLE kpi_events ADD COLUMN route_reason TEXT",
    "ALTER TABLE kpi_events ADD COLUMN rejection_reason TEXT",
    "ALTER TABLE kpi_events ADD COLUMN fallback_from TEXT",
    "ALTER TABLE kpi_events ADD COLUMN fallback_to TEXT",
    "ALTER TABLE kpi_events ADD COLUMN fallback_cause TEXT",
    "ALTER TABLE kpi_events ADD COLUMN network_state TEXT",
    "ALTER TABLE kpi_events ADD COLUMN network_quality_tier TEXT",
    """
    CREATE TABLE kpi_rollup_counts (
        bucket_ms INTEGER NOT NULL,
        interval_seconds INTEGER NOT NULL,
        event TEXT NOT NULL,
        mode TEXT NOT NULL DEFAULT '',
        locality TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        model_tier TEXT NOT NULL DEFAULT '',
        route TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL DEFAULT '',
        error_category TEXT NOT NULL DEFAULT '',
        route_reason TEXT NOT NULL DEFAULT '',
        rejection_reason TEXT NOT NULL DEFAULT '',
        fallback_from TEXT NOT NULL DEFAULT '',
        fallback_to TEXT NOT NULL DEFAULT '',
        fallback_cause TEXT NOT NULL DEFAULT '',
        network_state TEXT NOT NULL DEFAULT '',
        network_quality_tier TEXT NOT NULL DEFAULT '',
        count INTEGER NOT NULL CHECK(count >= 0),
        PRIMARY KEY (
            bucket_ms, interval_seconds, event, mode, locality, provider,
            model, model_tier, route, outcome, error_category, route_reason,
            rejection_reason, fallback_from, fallback_to, fallback_cause,
            network_state, network_quality_tier
        )
    ) WITHOUT ROWID
    """,
    """
    CREATE INDEX idx_kpi_events_mode_locality_time
    ON kpi_events(mode, locality, timestamp_ms)
    """,
    """
    CREATE INDEX idx_kpi_events_provider_model_time
    ON kpi_events(provider, model, timestamp_ms)
    """,
    """
    CREATE INDEX idx_kpi_events_network_time
    ON kpi_events(network_quality_tier, network_state, timestamp_ms)
    """,
    "CREATE INDEX idx_kpi_rollups_time ON kpi_rollup_counts(bucket_ms)",
)

_MIGRATION_3 = (
    """
    ALTER TABLE kpi_events
    ADD COLUMN weight INTEGER NOT NULL DEFAULT 1 CHECK(weight >= 0)
    """,
)

_MIGRATIONS = {1: _MIGRATION_1, 2: _MIGRATION_2, 3: _MIGRATION_3}


def _backfill_event_weights(connection: sqlite3.Connection) -> None:
    """Recover counter weights already present in version-two JSON payloads."""

    updates: list[tuple[int, int]] = []
    for row_id, payload in connection.execute("SELECT id, payload FROM kpi_events"):
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, Mapping):
            continue
        count = event.get("count", 1)
        if (
            isinstance(count, numbers.Integral)
            and not isinstance(count, bool)
            and 0 <= count <= 9_223_372_036_854_775_807
        ):
            updates.append((int(count), int(row_id)))
            if len(updates) >= 512:
                connection.executemany("UPDATE kpi_events SET weight = ? WHERE id = ?", updates)
                updates.clear()
    if updates:
        connection.executemany("UPDATE kpi_events SET weight = ? WHERE id = ?", updates)


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    target_version: int = LATEST_SCHEMA_VERSION,
    now_ms: int | None = None,
) -> None:
    """Apply ordered, transactional migrations to ``target_version``."""

    if not 0 <= target_version <= LATEST_SCHEMA_VERSION:
        raise KPIStorageError(f"unsupported KPI schema target: {target_version}")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at_ms INTEGER NOT NULL
        )
        """
    )
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > LATEST_SCHEMA_VERSION:
        raise KPIStorageError(
            f"KPI database schema {current} is newer than supported {LATEST_SCHEMA_VERSION}"
        )
    applied_at = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    for version in range(current + 1, target_version + 1):
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _MIGRATIONS[version]:
                connection.execute(statement)
            if version == 3:
                _backfill_event_weights(connection)
            connection.execute(
                "INSERT OR REPLACE INTO schema_migrations(version, applied_at_ms) VALUES (?, ?)",
                (version, applied_at),
            )
            connection.execute(f"PRAGMA user_version = {version}")
            connection.commit()
        except sqlite3.DatabaseError as error:
            connection.rollback()
            raise KPIStorageError(f"unable to apply KPI schema migration {version}") from error


def neutralize_csv_cell(value: Any) -> Any:
    """Prevent spreadsheet formula execution for exported textual cells."""

    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


class SQLiteKPIStore:
    """One-writer SQLite KPI store with bounded, read-only query helpers."""

    def __init__(
        self,
        path: str | Path,
        *,
        raw_retention_days: int = 7,
        rollup_retention_days: int = 90,
        max_size_bytes: int = 256 * 1024 * 1024,
        rollup_interval_seconds: int = 60,
        export_max_rows: int = 100_000,
        busy_timeout_ms: int = 2_000,
        maintenance_interval_seconds: float = 300.0,
    ) -> None:
        for name, value in (
            ("raw_retention_days", raw_retention_days),
            ("rollup_retention_days", rollup_retention_days),
            ("rollup_interval_seconds", rollup_interval_seconds),
            ("max_size_bytes", max_size_bytes),
            ("export_max_rows", export_max_rows),
            ("busy_timeout_ms", busy_timeout_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if rollup_interval_seconds == 0 or max_size_bytes == 0 or export_max_rows == 0:
            raise ValueError("rollup interval, maximum size, and export limit must be positive")
        if maintenance_interval_seconds < 0:
            raise ValueError("maintenance_interval_seconds cannot be negative")

        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_retention_days = raw_retention_days
        self.rollup_retention_days = rollup_retention_days
        self.max_size_bytes = max_size_bytes
        self.rollup_interval_seconds = rollup_interval_seconds
        self.export_max_rows = min(export_max_rows, MAX_QUERY_LIMIT)
        self.busy_timeout_ms = busy_timeout_ms
        self.maintenance_interval_seconds = maintenance_interval_seconds
        self._lock = threading.RLock()
        self._closed = False
        self._dropped_invalid = 0
        self._last_maintenance_ms = time.time_ns() // 1_000_000

        new_database = not self.path.exists() or self.path.stat().st_size == 0
        try:
            self._writer = sqlite3.connect(
                self.path,
                timeout=busy_timeout_ms / 1_000,
                isolation_level=None,
                check_same_thread=False,
            )
            self._writer.row_factory = sqlite3.Row
            self._configure_connection(self._writer, readonly=False)
            if new_database:
                self._writer.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self.journal_mode = self._enable_wal(self._writer)
            apply_migrations(self._writer)
        except (OSError, sqlite3.DatabaseError, KPIStorageError) as error:
            writer = getattr(self, "_writer", None)
            if writer is not None:
                writer.close()
            raise KPIStorageError(f"unable to initialize KPI database at {self.path}") from error

    def _configure_connection(
        self,
        connection: sqlite3.Connection,
        *,
        readonly: bool,
    ) -> None:
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        else:
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA journal_size_limit=8388608")
            connection.execute("PRAGMA wal_autocheckpoint=512")

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> str:
        try:
            mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if mode == "wal":
                return mode
        except sqlite3.DatabaseError:
            pass
        try:
            return str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        except sqlite3.DatabaseError as error:
            raise KPIStorageError("unable to select a safe SQLite journal mode") from error

    def _ensure_open(self) -> None:
        if self._closed:
            raise KPIStorageError("KPI store is closed")

    def _read_connection(self) -> sqlite3.Connection:
        self._ensure_open()
        try:
            connection = sqlite3.connect(
                self.path.as_uri() + "?mode=ro",
                uri=True,
                timeout=self.busy_timeout_ms / 1_000,
            )
            connection.row_factory = sqlite3.Row
            self._configure_connection(connection, readonly=True)
            return connection
        except sqlite3.DatabaseError as error:
            raise KPIStorageError("unable to open a read-only KPI connection") from error

    @staticmethod
    def _event_row(event: Mapping[str, Any]) -> tuple[Any, ...]:
        payload = json.dumps(
            dict(event),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        dimensions = {
            name: event.get(name)
            for name in _DIMENSION_FIELDS
            if name not in {"mode", "provider", "model"}
        }
        if (
            int(event.get("fallback_count", 0)) > 0
            and not dimensions.get("fallback_from")
            and not dimensions.get("fallback_to")
            and not dimensions.get("fallback_cause")
        ):
            dimensions["fallback_cause"] = "unspecified"
        return (
            event["timestamp_ms"],
            event["event"],
            event.get("mode"),
            event.get("provider"),
            event.get("model"),
            payload,
            *(
                dimensions[name]
                for name in _DIMENSION_FIELDS
                if name not in {"mode", "provider", "model"}
            ),
            int(event.get("count", 1)),
        )

    def __call__(self, payload: Mapping[str, Any]) -> None:
        """Retain compatibility with the existing one-event sink protocol."""

        self.write_batch((payload,))

    def write(self, event: object) -> bool:
        """Persist one event and return whether it passed sanitization."""

        return self.write_batch((event,)) == 1

    def write_batch(self, events: Iterable[object]) -> int:
        """Sanitize and commit a batch atomically, dropping invalid events."""

        sanitized: list[dict[str, Any]] = []
        dropped = 0
        for event in events:
            try:
                sanitized.append(sanitize_event(event))
            except EventSanitizationError:
                dropped += 1
        with self._lock:
            self._ensure_open()
            if dropped:
                self._dropped_invalid += dropped
            if sanitized:
                sql = """
                    INSERT INTO kpi_events (
                        timestamp_ms, event, mode, provider, model, payload,
                        locality, model_tier, route, outcome, error_category,
                        route_reason, rejection_reason, fallback_from, fallback_to,
                        fallback_cause, network_state, network_quality_tier, weight
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                try:
                    self._writer.execute("BEGIN IMMEDIATE")
                    self._writer.executemany(sql, (self._event_row(event) for event in sanitized))
                    self._write_meta_locked("dropped_invalid", str(self._dropped_invalid))
                    self._writer.commit()
                except sqlite3.DatabaseError as error:
                    self._writer.rollback()
                    raise KPIStorageError("unable to persist KPI event batch") from error
            elif dropped:
                try:
                    self._writer.execute("BEGIN IMMEDIATE")
                    self._write_meta_locked("dropped_invalid", str(self._dropped_invalid))
                    self._writer.commit()
                except sqlite3.DatabaseError:
                    self._writer.rollback()

            now_ms = time.time_ns() // 1_000_000
            due = (
                self.maintenance_interval_seconds == 0
                or now_ms - self._last_maintenance_ms >= self.maintenance_interval_seconds * 1_000
            )
            if due:
                self._maintain_locked(now_ms)
        return len(sanitized)

    def _write_meta_locked(self, key: str, value: str) -> None:
        cursor = self._writer.execute(
            "UPDATE kpi_meta SET value = ? WHERE key = ?",
            (value, key),
        )
        if cursor.rowcount == 0:
            self._writer.execute(
                "INSERT INTO kpi_meta(key, value) VALUES (?, ?)",
                (key, value),
            )

    @staticmethod
    def _bounded_limit(limit: int, maximum: int = MAX_QUERY_LIMIT) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
            raise ValueError(f"limit must be between one and {maximum}")
        return limit

    @staticmethod
    def _filter_values(value: object, name: str) -> tuple[str, ...]:
        if isinstance(value, str) or isinstance(value, Enum):
            values: Sequence[object] = (value,)
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            values = value
        else:
            values = (value,)
        if not values or len(values) > 20:
            raise ValueError(f"filter {name!r} must contain between one and 20 values")
        labels = tuple(_label(_enum_value(item), name) for item in values)
        if name == "outcome":
            return tuple(_OUTCOME_ALIASES.get(item.lower(), item.lower()) for item in labels)
        return labels

    def _where_clause(
        self,
        *,
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
        event_types: Iterable[str] | None = None,
        rollup: bool = False,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        time_column = "bucket_ms" if rollup else "timestamp_ms"
        if start_ms is not None:
            clauses.append(f"{time_column} >= ?")
            parameters.append(_query_timestamp_ms(start_ms))
        if end_ms is not None:
            clauses.append(f"{time_column} < ?")
            parameters.append(_query_timestamp_ms(end_ms))
        selected_filters = dict(filters or {})
        if event_types is not None:
            if "event" in selected_filters:
                raise ValueError("event filter and event_types cannot both be supplied")
            selected_filters["event"] = tuple(event_types)
        for name, value in selected_filters.items():
            normalized_name = _normalized_key(name)
            normalized_name = _ALIASES.get(normalized_name, normalized_name)
            if normalized_name not in _FILTER_FIELDS:
                raise ValueError(f"unsupported KPI filter: {name}")
            values = self._filter_values(value, normalized_name)
            placeholders = ",".join("?" for _ in values)
            column = normalized_name
            if rollup:
                clauses.append(f"{column} IN ({placeholders})")
            else:
                clauses.append(f"{column} IN ({placeholders})")
            parameters.extend(values)
        rendered = " AND ".join(clauses)
        return (f" WHERE {rendered}" if rendered else ""), parameters

    def query_events(
        self,
        *,
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
        event_types: Iterable[str] | None = None,
        limit: int = DEFAULT_QUERY_LIMIT,
        ascending: bool = True,
    ) -> list[dict[str, Any]]:
        """Read sanitized raw events through a short-lived read-only connection."""

        limit = self._bounded_limit(limit)
        where, parameters = self._where_clause(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            event_types=event_types,
        )
        direction = "ASC" if ascending else "DESC"
        sql = (
            "SELECT payload FROM kpi_events"
            + where
            + f" ORDER BY timestamp_ms {direction}, id {direction} LIMIT ?"
        )
        parameters.append(limit)
        connection = self._read_connection()
        try:
            rows = connection.execute(sql, parameters).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                value = json.loads(row["payload"])
                if not isinstance(value, dict):
                    raise KPIStorageError("KPI database contains an invalid event payload")
                result.append(value)
            return result
        except (sqlite3.DatabaseError, json.JSONDecodeError) as error:
            raise KPIStorageError("unable to query KPI events") from error
        finally:
            connection.close()

    def query_rollups(
        self,
        *,
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
        event_types: Iterable[str] | None = None,
        limit: int = DEFAULT_QUERY_LIMIT,
        ascending: bool = True,
    ) -> list[dict[str, Any]]:
        """Read count rollups without exposing database row identifiers."""

        limit = self._bounded_limit(limit)
        where, parameters = self._where_clause(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            event_types=event_types,
            rollup=True,
        )
        direction = "ASC" if ascending else "DESC"
        selected = ", ".join(
            ("bucket_ms", "interval_seconds", "event", *_DIMENSION_FIELDS, "count")
        )
        sql = (
            f"SELECT {selected} FROM kpi_rollup_counts"
            + where
            + f" ORDER BY bucket_ms {direction} LIMIT ?"
        )
        parameters.append(limit)
        connection = self._read_connection()
        try:
            rows = connection.execute(sql, parameters).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                value = dict(row)
                for name in _DIMENSION_FIELDS:
                    if value[name] == "":
                        value[name] = None
                result.append(value)
            return result
        except sqlite3.DatabaseError as error:
            raise KPIStorageError("unable to query KPI rollups") from error
        finally:
            connection.close()

    def _rollup_locked(self, cutoff_ms: int, *, inclusive: bool = False) -> int:
        operator = "<=" if inclusive else "<"
        interval_ms = self.rollup_interval_seconds * 1_000
        coalesced = ", ".join(f"COALESCE({name}, '')" for name in _DIMENSION_FIELDS)
        group_positions = ", ".join(str(index) for index in range(1, 4 + len(_DIMENSION_FIELDS)))
        rows = self._writer.execute(
            f"""
            SELECT (timestamp_ms / ?) * ?, ?, event, {coalesced}, SUM(weight)
            FROM kpi_events
            WHERE timestamp_ms {operator} ?
            GROUP BY {group_positions}
            """,
            (interval_ms, interval_ms, self.rollup_interval_seconds, cutoff_ms),
        ).fetchall()
        if rows:
            key_columns = (
                "bucket_ms",
                "interval_seconds",
                "event",
                *_DIMENSION_FIELDS,
            )
            columns = ", ".join((*key_columns, "count"))
            placeholders = ", ".join("?" for _ in range(4 + len(_DIMENSION_FIELDS)))
            row_values = [tuple(row) for row in rows]

            # SQLite on older Jetson releases predates UPSERT ... DO UPDATE
            # (introduced in 3.24). Seed missing rows at zero, then increment
            # every row inside the surrounding transaction.
            self._writer.executemany(
                f"INSERT OR IGNORE INTO kpi_rollup_counts ({columns}) VALUES ({placeholders})",
                (row[:-1] + (0,) for row in row_values),
            )
            where = " AND ".join(f"{column} = ?" for column in key_columns)
            self._writer.executemany(
                f"UPDATE kpi_rollup_counts SET count = count + ? WHERE {where}",
                ((row[-1], *row[:-1]) for row in row_values),
            )
        cursor = self._writer.execute(
            f"DELETE FROM kpi_events WHERE timestamp_ms {operator} ?", (cutoff_ms,)
        )
        return max(0, cursor.rowcount)

    def maintain(self, *, now: Any = None) -> dict[str, int]:
        """Roll up expired raw data, prune expired rollups, and enforce size."""

        now_ms = timestamp_to_ms(now, default_now=True)
        with self._lock:
            self._ensure_open()
            return self._maintain_locked(now_ms)

    def _maintain_locked(self, now_ms: int) -> dict[str, int]:
        raw_cutoff = now_ms - self.raw_retention_days * _DAY_MS
        rollup_cutoff = now_ms - self.rollup_retention_days * _DAY_MS
        removed_raw = 0
        removed_rollups = 0
        try:
            self._writer.execute("BEGIN IMMEDIATE")
            removed_raw = self._rollup_locked(raw_cutoff)
            cursor = self._writer.execute(
                "DELETE FROM kpi_rollup_counts WHERE bucket_ms < ?", (rollup_cutoff,)
            )
            removed_rollups = max(0, cursor.rowcount)
            self._write_meta_locked("last_maintenance_ms", str(now_ms))
            self._writer.commit()
        except sqlite3.DatabaseError as error:
            self._writer.rollback()
            raise KPIStorageError("KPI retention maintenance failed") from error
        self._last_maintenance_ms = now_ms
        pressure_removed = self._enforce_size_locked()
        return {
            "raw_events_rolled_up": removed_raw,
            "expired_rollups_removed": removed_rollups,
            "size_pressure_rows_removed": pressure_removed,
        }

    def _database_size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
        return total

    def _checkpoint(self, mode: str = "PASSIVE") -> None:
        try:
            self._writer.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        except sqlite3.DatabaseError:
            return

    def _enforce_size_locked(self) -> int:
        self._checkpoint("TRUNCATE")
        removed = 0
        attempts = 0
        while self._database_size_bytes() > self.max_size_bytes and attempts < 32:
            attempts += 1
            row = self._writer.execute(
                """
                SELECT MAX(timestamp_ms) FROM (
                    SELECT timestamp_ms FROM kpi_events
                    ORDER BY timestamp_ms, id LIMIT 1000
                )
                """
            ).fetchone()
            cutoff = row[0] if row is not None else None
            try:
                self._writer.execute("BEGIN IMMEDIATE")
                if cutoff is not None:
                    removed += self._rollup_locked(int(cutoff), inclusive=True)
                else:
                    oldest = self._writer.execute(
                        "SELECT MIN(bucket_ms) FROM kpi_rollup_counts"
                    ).fetchone()[0]
                    cursor = self._writer.execute(
                        "DELETE FROM kpi_rollup_counts WHERE bucket_ms = ?", (oldest,)
                    )
                    count = max(0, cursor.rowcount)
                    removed += count
                    if count == 0:
                        self._writer.rollback()
                        break
                self._writer.commit()
            except sqlite3.DatabaseError:
                self._writer.rollback()
                break
            self._vacuum_to_limit_locked()
            self._checkpoint("TRUNCATE")
        return removed

    def _vacuum_to_limit_locked(self, *, maximum_steps: int = 2_048) -> None:
        """Release free pages incrementally without a blocking full VACUUM."""

        for _step in range(maximum_steps):
            if self._database_size_bytes() <= self.max_size_bytes:
                return
            try:
                free_pages = int(self._writer.execute("PRAGMA freelist_count").fetchone()[0])
                if free_pages == 0:
                    return
                # Some SQLite builds release only the final page per call even
                # when a larger argument is supplied, hence the bounded loop.
                self._writer.execute("PRAGMA incremental_vacuum(256)")
            except sqlite3.DatabaseError:
                return

    def status(self) -> dict[str, Any]:
        """Return sanitized storage health and capacity information."""

        connection = self._read_connection()
        try:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count, MIN(timestamp_ms) AS oldest,
                       MAX(timestamp_ms) AS newest FROM kpi_events
                """
            ).fetchone()
            rollups = int(
                connection.execute("SELECT COUNT(*) FROM kpi_rollup_counts").fetchone()[0]
            )
            metadata = {
                item["key"]: item["value"]
                for item in connection.execute("SELECT key, value FROM kpi_meta")
            }
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.DatabaseError as error:
            raise KPIStorageError("unable to inspect KPI storage") from error
        finally:
            connection.close()
        size_bytes = self._database_size_bytes()
        return {
            "schema_version": schema_version,
            "journal_mode": self.journal_mode,
            "size_bytes": size_bytes,
            "max_size_bytes": self.max_size_bytes,
            "over_size_limit": size_bytes > self.max_size_bytes,
            "raw_event_count": int(row["count"]),
            "rollup_row_count": rollups,
            "oldest_timestamp_ms": row["oldest"],
            "newest_timestamp_ms": row["newest"],
            "dropped_invalid": int(metadata.get("dropped_invalid", self._dropped_invalid)),
            "last_maintenance_ms": (
                int(metadata["last_maintenance_ms"]) if "last_maintenance_ms" in metadata else None
            ),
        }

    def clear(self) -> dict[str, int]:
        """Transactionally remove all KPI events and rollups."""

        with self._lock:
            self._ensure_open()
            raw_count = int(self._writer.execute("SELECT COUNT(*) FROM kpi_events").fetchone()[0])
            rollup_count = int(
                self._writer.execute("SELECT COUNT(*) FROM kpi_rollup_counts").fetchone()[0]
            )
            try:
                self._writer.execute("PRAGMA secure_delete=ON")
                self._writer.execute("BEGIN IMMEDIATE")
                self._writer.execute("DELETE FROM kpi_events")
                self._writer.execute("DELETE FROM kpi_rollup_counts")
                self._writer.execute("DELETE FROM kpi_meta")
                self._writer.commit()
                self._dropped_invalid = 0
                self._last_maintenance_ms = 0
            except sqlite3.DatabaseError as error:
                self._writer.rollback()
                raise KPIStorageError("unable to clear KPI data") from error
            finally:
                try:
                    self._writer.execute("PRAGMA secure_delete=FAST")
                except sqlite3.DatabaseError:
                    pass
            self._checkpoint("TRUNCATE")
            try:
                self._writer.execute("PRAGMA incremental_vacuum")
            except sqlite3.DatabaseError:
                pass
            return {"raw_events_removed": raw_count, "rollup_rows_removed": rollup_count}

    def _export_events(
        self,
        *,
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        selected_limit = self.export_max_rows if limit is None else limit
        selected_limit = self._bounded_limit(selected_limit, self.export_max_rows)
        return self.query_events(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            limit=selected_limit,
        )

    def export_json(
        self,
        *,
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> str:
        """Return a bounded JSON array containing only allowlisted fields."""

        events = self._export_events(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            limit=limit,
        )
        return json.dumps(events, ensure_ascii=True, separators=(",", ":"), sort_keys=True)

    def export_csv(
        self,
        *,
        start_ms: Any = None,
        end_ms: Any = None,
        filters: Mapping[str, object] | None = None,
        limit: int | None = None,
    ) -> str:
        """Return bounded CSV with a stable schema and formula-safe text."""

        events = self._export_events(
            start_ms=start_ms,
            end_ms=end_ms,
            filters=filters,
            limit=limit,
        )
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=_EXPORT_FIELDS, lineterminator="\n")
        writer.writeheader()
        for event in events:
            writer.writerow(
                {name: neutralize_csv_cell(event.get(name, "")) for name in _EXPORT_FIELDS}
            )
        return output.getvalue()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._checkpoint("TRUNCATE")
            self._writer.close()
            self._closed = True

    def __enter__(self) -> SQLiteKPIStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = [
    "DEFAULT_QUERY_LIMIT",
    "EventSanitizationError",
    "KPIStorageError",
    "LATEST_SCHEMA_VERSION",
    "MAX_QUERY_LIMIT",
    "SQLiteKPIStore",
    "apply_migrations",
    "neutralize_csv_cell",
    "sanitize_event",
    "timestamp_to_ms",
]
