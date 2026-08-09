from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from api.metrics import MetricEvent
from observability.storage import (
    EventSanitizationError,
    LATEST_SCHEMA_VERSION,
    SQLiteKPIStore,
    apply_migrations,
    neutralize_csv_cell,
    sanitize_event,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _database_bytes(path: Path) -> bytes:
    payload = b""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            payload += candidate.read_bytes()
    return payload


def test_sanitizer_omits_ids_and_rejects_content_fields() -> None:
    sanitized = sanitize_event(
        {
            "event": "llm_attempt_succeeded",
            "timestamp": _now(),
            "request_id": "provider-secret-id",
            "attempt_id": "internal-attempt-id",
            "provider": "ollama",
        }
    )

    assert "request_id" not in sanitized
    assert "attempt_id" not in sanitized
    with pytest.raises(EventSanitizationError, match="sensitive"):
        sanitize_event({"event": "bad", "prompt": "never persist me"})
    with pytest.raises(EventSanitizationError, match="unknown"):
        sanitize_event({"event": "bad", "user_text": "never persist me"})


def test_actual_metric_resource_and_voice_fields_are_persisted(tmp_path: Path) -> None:
    path = tmp_path / "kpi.sqlite3"
    store = SQLiteKPIStore(path, maintenance_interval_seconds=10_000)
    resource = MetricEvent(
        "resource_sample",
        timestamp=_now(),
        resource_scope="device",
        cpu_percent=21.5,
        gpu_percent=44.0,
        memory_percent=52.0,
        swap_percent=2.0,
        memory_used_mb=2048.0,
        memory_total_mb=8192.0,
        swap_used_mb=32.0,
        swap_total_mb=1024.0,
        cpu_temperature_c=-10.0,
        gpu_temperature_c=48.5,
        power_w=8.25,
        storage_used_mb=3.5,
        cpu_frequency_mhz=1200.0,
        gpu_frequency_mhz=624.0,
    )
    voice = MetricEvent(
        "voice_activity",
        timestamp=_now(),
        wake_word_count=1,
        recognized_count=1,
        cancellation_count=2,
        interruption_count=3,
    )

    assert store.write_batch((resource, voice)) == 2
    rows = store.query_events()
    store.close()

    assert rows[0]["cpu_percent"] == 21.5
    assert rows[0]["cpu_temperature_c"] == -10.0
    assert rows[0]["memory_total_mb"] == 8192.0
    assert rows[1]["wake_word_count"] == 1
    assert rows[1]["interruption_count"] == 3


def test_sensitive_markers_never_reach_database_or_exports(tmp_path: Path) -> None:
    path = tmp_path / "kpi.sqlite3"
    marker = "ULTRA_PRIVATE_TRANSCRIPT_92817"
    request_marker = "PROVIDER_REQUEST_SECRET_177"
    store = SQLiteKPIStore(path, maintenance_interval_seconds=10_000)

    assert store.write(
        {
            "event": "llm_attempt_succeeded",
            "timestamp": _now(),
            "request_id": request_marker,
            "provider": "ollama",
            "outcome": "success",
        }
    )
    assert not store.write(
        {"event": "voice_command_completed", "timestamp": _now(), "transcript": marker}
    )
    exported_json = store.export_json()
    exported_csv = store.export_csv()
    store.close()

    database_bytes = _database_bytes(path)
    for forbidden in (marker, request_marker, "transcript"):
        assert forbidden.encode() not in database_bytes
        assert forbidden not in exported_json
        assert forbidden not in exported_csv
    assert json.loads(exported_json)[0]["outcome"] == "succeeded"


def test_schema_migrates_from_version_one(tmp_path: Path) -> None:
    path = tmp_path / "kpi.sqlite3"
    connection = sqlite3.connect(path, isolation_level=None)
    apply_migrations(connection, target_version=1)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    connection.close()

    store = SQLiteKPIStore(path)
    status = store.status()
    columns = {row[1] for row in store._writer.execute("PRAGMA table_info(kpi_events)").fetchall()}
    store.close()

    assert status["schema_version"] == LATEST_SCHEMA_VERSION
    assert {"locality", "model_tier", "network_quality_tier", "weight"}.issubset(columns)


def test_version_two_counter_weight_is_backfilled_and_compacted(tmp_path: Path) -> None:
    path = tmp_path / "kpi.sqlite3"
    old = _now() - timedelta(days=10)
    timestamp_ms = round(old.timestamp() * 1_000)
    payload = {
        "event": "metrics_events_dropped",
        "timestamp": old.isoformat().replace("+00:00", "Z"),
        "timestamp_ms": timestamp_ms,
        "count": 7,
        "dropped_count": 7,
    }
    connection = sqlite3.connect(path, isolation_level=None)
    apply_migrations(connection, target_version=2)
    connection.execute(
        "INSERT INTO kpi_events(timestamp_ms, event, payload) VALUES (?, ?, ?)",
        (timestamp_ms, payload["event"], json.dumps(payload)),
    )
    connection.close()

    store = SQLiteKPIStore(
        path,
        raw_retention_days=7,
        rollup_retention_days=90,
        maintenance_interval_seconds=10_000,
    )
    assert store._writer.execute("SELECT weight FROM kpi_events").fetchone()[0] == 7
    store.maintain(now=_now())
    rollups = store.query_rollups(event_types=("metrics_events_dropped",))
    store.close()

    assert len(rollups) == 1
    assert rollups[0]["count"] == 7


def test_dropped_metric_count_survives_rollup_compaction(tmp_path: Path) -> None:
    now = _now()
    old = now - timedelta(days=10)
    store = SQLiteKPIStore(
        tmp_path / "kpi.sqlite3",
        raw_retention_days=7,
        rollup_retention_days=90,
        maintenance_interval_seconds=10_000,
    )
    store.write(
        MetricEvent(
            "metrics_events_dropped",
            timestamp=old,
            count=11,
            dropped_count=11,
        )
    )
    store.maintain(now=now)
    rollups = store.query_rollups(event_types=("metrics_events_dropped",))
    store.close()

    assert sum(row["count"] for row in rollups) == 11


def test_writes_and_rollup_merges_use_sqlite_322_compatible_sql(tmp_path: Path) -> None:
    now = _now()
    old = now - timedelta(days=10)
    statements: list[str] = []
    store = SQLiteKPIStore(
        tmp_path / "kpi.sqlite3",
        raw_retention_days=7,
        rollup_retention_days=90,
        maintenance_interval_seconds=10_000,
    )
    store._writer.set_trace_callback(statements.append)

    for count in (3, 5):
        assert store.write(
            MetricEvent(
                "metrics_events_dropped",
                timestamp=old,
                count=count,
                dropped_count=count,
            )
        )
        store.maintain(now=now)

    rollups = store.query_rollups(event_types=("metrics_events_dropped",))
    store.close()

    assert sum(row["count"] for row in rollups) == 8
    assert all("ON CONFLICT" not in statement.upper() for statement in statements)


def test_batch_writer_supports_concurrent_short_lived_readers(tmp_path: Path) -> None:
    store = SQLiteKPIStore(tmp_path / "kpi.sqlite3", maintenance_interval_seconds=10_000)
    began = threading.Event()
    failures: list[BaseException] = []

    def writer() -> None:
        try:
            began.set()
            for batch in range(10):
                store.write_batch(
                    {
                        "event": "llm_attempt_succeeded",
                        "timestamp": _now(),
                        "provider": "ollama",
                        "model": "model",
                        "count": 1,
                    }
                    for _ in range(10)
                )
        except BaseException as error:  # pragma: no cover - assertion below reports it
            failures.append(error)

    thread = threading.Thread(target=writer)
    thread.start()
    assert began.wait(timeout=1)
    while thread.is_alive():
        assert isinstance(store.query_events(limit=100), list)
    thread.join(timeout=2)

    assert failures == []
    assert store.status()["raw_event_count"] == 100
    store.close()


def test_retention_rolls_up_counts_and_clear_is_transactional(tmp_path: Path) -> None:
    now = _now()
    old = now - timedelta(days=10)
    store = SQLiteKPIStore(
        tmp_path / "kpi.sqlite3",
        raw_retention_days=7,
        rollup_retention_days=90,
        maintenance_interval_seconds=10_000,
    )
    store.write_batch(
        (
            {
                "event": "llm_request_succeeded",
                "timestamp": old,
                "provider": "remote",
                "model": "model",
                "locality": "remote",
                "outcome": "succeeded",
                "fallback_count": 1,
            },
            {
                "event": "llm_request_failed",
                "timestamp": old,
                "provider": "remote",
                "model": "model",
                "locality": "remote",
                "outcome": "failed",
            },
        )
    )

    result = store.maintain(now=now)
    rollups = store.query_rollups()
    cleared = store.clear()
    status = store.status()
    store.close()

    assert result["raw_events_rolled_up"] == 2
    assert sum(row["count"] for row in rollups) == 2
    assert any(row["fallback_cause"] == "unspecified" for row in rollups)
    assert cleared == {"raw_events_removed": 0, "rollup_rows_removed": len(rollups)}
    assert status["raw_event_count"] == status["rollup_row_count"] == 0


def test_max_size_pressure_prefers_compacted_counts(tmp_path: Path) -> None:
    store = SQLiteKPIStore(
        tmp_path / "kpi.sqlite3",
        max_size_bytes=128 * 1024,
        maintenance_interval_seconds=10_000,
    )
    now = _now()
    store.write_batch(
        {
            "event": "resource_sample",
            "timestamp": now,
            "cpu_percent": float(index % 100),
            "memory_used_mb": float(index),
        }
        for index in range(3_000)
    )
    store.maintain(now=now)
    status = store.status()
    store.close()

    assert status["raw_event_count"] < 3_000
    assert status["rollup_row_count"] >= 1
    # A SQLite schema has a non-zero minimum footprint; this bound leaves one
    # page of platform variation while still proving pressure is enforced.
    assert status["size_bytes"] <= 160 * 1024


def test_exports_are_bounded_and_csv_cells_are_formula_safe(tmp_path: Path) -> None:
    store = SQLiteKPIStore(
        tmp_path / "kpi.sqlite3",
        export_max_rows=2,
        maintenance_interval_seconds=10_000,
    )
    store.write_batch(
        {"event": "sample", "timestamp": _now(), "count": index} for index in range(3)
    )

    assert len(json.loads(store.export_json())) == 2
    assert len(store.export_csv().splitlines()) == 3
    with pytest.raises(ValueError, match="limit"):
        store.export_json(limit=3)
    assert neutralize_csv_cell("=2+2") == "'=2+2"
    store.close()
