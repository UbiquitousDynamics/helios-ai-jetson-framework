from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

import pytest

from observability.aggregate import KPIQueryService, percentile_r7
from observability.dashboard import API_RESOURCES, DashboardServer, _TIMESERIES_METRICS
from observability.storage import SQLiteKPIStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_percentile_r7_is_exact_and_never_averages_percentiles() -> None:
    values = [1.0, 2.0, 3.0, 4.0]

    assert percentile_r7(values, 25) == pytest.approx(1.75)
    assert percentile_r7(values, 0.5) == pytest.approx(2.5)
    assert percentile_r7(values, 95) == pytest.approx(3.85)
    assert percentile_r7([], 95) is None
    with pytest.raises(ValueError):
        percentile_r7([1.0, float("nan")], 50)


def test_empty_queries_have_stable_zero_and_null_values(tmp_path: Path) -> None:
    store = SQLiteKPIStore(tmp_path / "kpi.sqlite3")
    service = KPIQueryService(store)

    summary = service.summary()
    latency = service.latency()

    assert summary["request_count"] == 0
    assert summary["success_rate"] == 0.0
    assert summary["end_to_end_ms"]["p95"] is None
    assert latency["statistics"]["count"] == 0
    assert service.timeseries()["points"] == []
    assert service.providers() == {"providers": []}
    assert service.network()["current"] is None
    assert service.resources()["current"] is None
    store.close()


def test_latency_query_returns_canonical_voice_stage_breakdown(tmp_path: Path) -> None:
    now = _now()
    store = SQLiteKPIStore(tmp_path / "kpi.sqlite3", maintenance_interval_seconds=10_000)
    store.write_batch(
        [
            {
                "event": "voice_listen_completed",
                "timestamp": now,
                "listening_ms": 400.0,
                "speech_finalization_ms": 35.0,
                "stt_ms": 80.0,
            },
            {"event": "rag_completed", "timestamp": now, "rag_ms": 15.0},
            {
                "event": "llm_request_succeeded",
                "timestamp": now,
                "outcome": "succeeded",
                "routing_ms": 5.0,
                "first_token_ms": 45.0,
                "inference_ms": 120.0,
                "speech_dispatch_ms": 150.0,
                "actual_first_audio_ms": 190.0,
                "end_to_end_ms": 330.0,
            },
            {
                "event": "tts_completed",
                "timestamp": now,
                "tts_synthesis_ms": 30.0,
                "audio_playback_ms": 110.0,
                "audio_duration_ms": 100.0,
            },
        ]
    )

    latency = KPIQueryService(store).latency(metric="actual_first_audio_ms")

    assert latency["metric"] == "actual_first_audio_ms"
    assert latency["statistics"]["p50"] == pytest.approx(190.0)
    expected_means = {
        "listening_ms": 400.0,
        "speech_finalization_ms": 35.0,
        "stt_ms": 80.0,
        "rag_ms": 15.0,
        "routing_ms": 5.0,
        "first_token_ms": 45.0,
        "inference_ms": 120.0,
        "tts_synthesis_ms": 30.0,
        "speech_dispatch_ms": 150.0,
        "actual_first_audio_ms": 190.0,
        "audio_playback_ms": 110.0,
        "audio_duration_ms": 100.0,
    }
    assert {
        name: statistics["mean"] for name, statistics in latency["breakdown"].items()
    } == expected_means
    store.close()


def test_summary_counts_only_llm_requests_and_computes_weighted_rates(tmp_path: Path) -> None:
    now = _now()
    store = SQLiteKPIStore(tmp_path / "kpi.sqlite3", maintenance_interval_seconds=10_000)
    events = [
        {
            "event": "llm_request_succeeded",
            "timestamp": now,
            "mode": "talk",
            "locality": "device",
            "outcome": "succeeded",
            "end_to_end_ms": 100.0,
            "first_token_ms": 20.0,
        },
        {
            "event": "llm_request_failed",
            "timestamp": now,
            "mode": "talk",
            "locality": "device",
            "outcome": "failed",
            "error_category": "read_timeout",
            "interruption_count": 1,
            "end_to_end_ms": 300.0,
        },
        {
            "event": "llm_request_succeeded",
            "timestamp": now,
            "mode": "think",
            "locality": "remote",
            "outcome": "succeeded",
            "end_to_end_ms": 500.0,
            "fallback_count": 1,
            "fallback_from": "remote-a",
            "fallback_to": "remote-b",
        },
        {
            "event": "llm_request_succeeded",
            "timestamp": now,
            "mode": "think",
            "locality": "remote",
            "outcome": "succeeded",
            "end_to_end_ms": 700.0,
        },
        {
            "event": "llm_attempt_succeeded",
            "timestamp": now,
            "mode": "think",
            "locality": "remote",
            "outcome": "succeeded",
            "cost_usd": "0.0125",
        },
        {
            "event": "llm_attempt_failed",
            "timestamp": now,
            "mode": "think",
            "locality": "remote",
            "outcome": "failed",
            "cost_usd": "0.0025",
        },
        {"event": "voice_command_completed", "timestamp": now, "outcome": "succeeded"},
        {"event": "voice_command_failed", "timestamp": now, "outcome": "failed"},
    ]
    store.write_batch(events)
    service = KPIQueryService(store)

    summary = service.summary(
        start_ms=round((now - timedelta(minutes=1)).timestamp() * 1_000),
        end_ms=round((now + timedelta(minutes=1)).timestamp() * 1_000),
    )

    assert summary["request_count"] == 4
    assert summary["voice_command_count"] == 2
    assert summary["success_rate"] == pytest.approx(0.75)
    assert summary["fallback_rate"] == pytest.approx(0.25)
    assert summary["local_success_rate"] == pytest.approx(0.5)
    assert summary["remote_success_rate"] == pytest.approx(1.0)
    assert summary["error_categories"] == {"read_timeout": 1}
    assert summary["timeout_count"] == 1
    assert summary["cancellation_count"] == 0
    assert summary["refusal_count"] == 0
    assert summary["interruption_count"] == 1
    assert summary["end_to_end_ms"]["p50"] == pytest.approx(400.0)
    assert summary["latency_by_locality"]["remote"]["p50"] == pytest.approx(600.0)
    assert summary["latency_by_mode"]["talk"]["p50"] == pytest.approx(200.0)
    assert summary["cost_usd"]["cost_usd"] == "0.0150"
    store.close()


def test_bounded_aggregates_exclude_unrelated_events_and_keep_latest_current_sample(
    tmp_path: Path,
) -> None:
    now = _now()
    store = SQLiteKPIStore(tmp_path / "kpi.sqlite3", maintenance_interval_seconds=10_000)
    store.write_batch(
        [
            {
                "event": "resource_sample",
                "timestamp": now - timedelta(seconds=3),
                "cpu_percent": 10.0,
            },
            {
                "event": "resource_sample",
                "timestamp": now - timedelta(seconds=2),
                "cpu_percent": 20.0,
            },
            {
                "event": "resource_sample",
                "timestamp": now - timedelta(seconds=1),
                "cpu_percent": 30.0,
            },
            {
                "event": "llm_request_succeeded",
                "timestamp": now,
                "outcome": "succeeded",
                "locality": "device",
                "end_to_end_ms": 50.0,
            },
        ]
    )
    service = KPIQueryService(store, max_events=2)

    summary = service.summary()
    resources = service.resources(points=2)

    assert summary["request_count"] == 1
    assert "truncated" not in summary
    assert resources["current"]["cpu_percent"] == 30.0
    assert resources["truncated"] is True
    assert [point["cpu_percent"] for point in resources["series"]] == [20.0, 30.0]
    store.close()


def test_rollup_counts_preserve_request_and_fallback_math(tmp_path: Path) -> None:
    now = _now()
    old = now - timedelta(days=10)
    store = SQLiteKPIStore(
        tmp_path / "kpi.sqlite3",
        raw_retention_days=7,
        rollup_retention_days=90,
        maintenance_interval_seconds=10_000,
    )
    store.write_batch(
        [
            {
                "event": "llm_request_succeeded",
                "timestamp": old,
                "outcome": "succeeded",
                "locality": "remote",
                "fallback_count": 1,
            },
            {
                "event": "llm_request_failed",
                "timestamp": old,
                "outcome": "failed",
                "locality": "remote",
            },
        ]
    )
    store.maintain(now=now)

    summary = KPIQueryService(store).summary(
        start_ms=round((old - timedelta(days=1)).timestamp() * 1_000),
        end_ms=round((now + timedelta(days=1)).timestamp() * 1_000),
    )

    assert summary["request_count"] == 2
    assert summary["success_rate"] == pytest.approx(0.5)
    assert summary["fallback_rate"] == pytest.approx(0.5)
    assert summary["remote_success_rate"] == pytest.approx(0.5)
    store.close()


def test_filters_timeseries_provider_network_and_resource_queries(tmp_path: Path) -> None:
    now = _now()
    store = SQLiteKPIStore(tmp_path / "kpi.sqlite3", maintenance_interval_seconds=10_000)
    store.write_batch(
        [
            {
                "event": "llm_request_succeeded",
                "timestamp": now,
                "mode": "talk",
                "provider": "remote",
                "model": "tier-a",
                "locality": "remote",
                "outcome": "succeeded",
                "end_to_end_ms": 250.0,
                "fallback_count": 1,
            },
            {
                "event": "llm_attempt_succeeded",
                "timestamp": now,
                "mode": "talk",
                "provider": "remote",
                "model": "tier-a",
                "locality": "remote",
                "outcome": "succeeded",
                "latency_ms": 200.0,
                "network_quality_tier": "good",
            },
            {
                "event": "llm_route_decided",
                "timestamp": now,
                "mode": "talk",
                "provider": "remote",
                "model": "tier-a",
                "locality": "remote",
                "outcome": "selected",
                "network_state": "online",
                "network_quality_tier": "good",
                "network_quality_score": 0.85,
            },
            {
                "event": "provider_health_changed",
                "timestamp": now,
                "provider": "remote",
                "model": "tier-a",
                "circuit_state": "cooldown",
                "previous_circuit_state": "available",
            },
            {
                "event": "network_probe_completed",
                "timestamp": now,
                "network_state": "online",
                "network_quality_tier": "good",
                "network_quality_score": 0.85,
                "ttfb_ms": 120.0,
                "goodput_kbps": 900.0,
                "probe_success": True,
            },
            {
                "event": "resource_sample",
                "timestamp": now,
                "mode": "talk",
                "locality": "remote",
                "provider": "remote",
                "model": "tier-a",
                "cpu_percent": 30.0,
                "gpu_percent": 40.0,
                "cpu_temperature_c": 52.0,
                "gpu_temperature_c": 49.0,
            },
        ]
    )
    service = KPIQueryService(store)

    filtered = service.summary(filters={"locality": "remote", "outcome": "success"})
    series = service.timeseries(
        metrics=("requests", "success_rate", "fallback_rate", "temperature_c"),
        interval_seconds=60,
    )
    providers = service.providers()["providers"]
    network = service.network()
    resources = service.resources()

    assert filtered["request_count"] == 1
    assert series["points"][0]["metrics"]["requests"]["mean"] == 1.0
    assert providers[0]["locality"] == "remote"
    assert providers[0]["circuit"]["state"] == "cooldown"
    assert network["current"]["network_quality_score"] == 0.85
    assert network["remote_success_by_quality_tier"]["good"]["success_rate"] == 1.0
    assert network["remote_success_by_quality_tier"]["good"]["failure_count"] == 0
    assert network["remote_success_by_quality_tier"]["good"]["latency_ms"]["p50"] == 200.0
    assert network["routing_decisions_by_quality_tier"] == {"good": 1}
    assert resources["current"]["cpu_percent"] == 30.0
    assert resources["metrics"]["gpu_percent"]["p95"] == 40.0
    assert len(network["series"]) <= 300
    assert len(resources["series"]) <= 300
    for metric in _TIMESERIES_METRICS:
        response = service.query(
            "timeseries", {"window_seconds": 3_600, "metric": metric, "points": 20}
        )
        assert len(response["points"]) <= 20
    store.close()


def test_routing_counts_once_and_network_series_uses_probe_observations(tmp_path: Path) -> None:
    now = _now()
    store = SQLiteKPIStore(tmp_path / "kpi.sqlite3", maintenance_interval_seconds=10_000)
    common = {
        "timestamp": now,
        "mode": "talk",
        "network_state": "offline",
        "network_quality_tier": "offline",
        "network_forced_local": True,
        "complexity_score": 2,
    }
    store.write_batch(
        [
            {
                **common,
                "event": "llm_route_decided",
                "route": "local-talk",
                "locality": "local",
                "outcome": "selected",
            },
            {
                **common,
                "event": "llm_route_candidate_rejected",
                "route": "remote-talk",
                "locality": "remote",
                "outcome": "rejected",
                "rejection_reason": "network_offline",
            },
            {
                **common,
                "event": "llm_request_succeeded",
                "route": "local-talk",
                "locality": "local",
                "outcome": "succeeded",
            },
            {
                "event": "network_probe_completed",
                "timestamp": now - timedelta(seconds=1),
                "network_state": "online",
                "network_quality_tier": "excellent",
                "network_quality_score": 0.95,
                "probe_success": True,
            },
        ]
    )

    service = KPIQueryService(store)
    routing = service.routing()
    network = service.network()
    store.close()

    assert routing["network_forced_local_count"] == 1
    assert routing["complexity_scores"] == {"2": 1}
    assert network["current"]["network_state"] == "online"
    assert network["quality_tiers"] == {"excellent": 1}
    assert len(network["series"]) == 1


def test_real_query_service_serves_every_dashboard_route(tmp_path: Path) -> None:
    now = _now()
    store = SQLiteKPIStore(tmp_path / "kpi.sqlite3", maintenance_interval_seconds=10_000)
    store.write_batch(
        [
            {
                "event": "llm_request_succeeded",
                "timestamp": now,
                "mode": "talk",
                "locality": "device",
                "outcome": "succeeded",
                "end_to_end_ms": 100.0,
            },
            {
                "event": "llm_attempt_succeeded",
                "timestamp": now,
                "provider": "ollama",
                "model": "local",
                "mode": "talk",
                "locality": "device",
                "outcome": "succeeded",
                "latency_ms": 90.0,
            },
            {"event": "resource_sample", "timestamp": now, "cpu_percent": 10.0},
        ]
    )
    service = KPIQueryService(store)
    server = DashboardServer(service, port=0)
    server.start()
    host, port = server.address
    try:
        responses = {}
        for resource in sorted(API_RESOURCES):
            with urlopen(f"http://{host}:{port}/api/v1/kpi/{resource}", timeout=3) as response:
                body = response.read()
                assert response.status == 200, resource
                assert body, resource
                responses[resource] = json.loads(body)
        assert "value" in responses["timeseries"]["points"][0]
        assert len(responses["network"]["series"]) <= 300
        assert len(responses["resources"]["series"]) <= 300
        assert isinstance(responses["export"], list)
    finally:
        server.close()
        store.close()


def test_provider_query_honors_the_validated_result_limit(tmp_path: Path) -> None:
    now = _now()
    store = SQLiteKPIStore(tmp_path / "kpi.sqlite3", maintenance_interval_seconds=10_000)
    store.write_batch(
        [
            {
                "event": "llm_attempt_succeeded",
                "timestamp": now,
                "provider": provider,
                "model": "model",
                "outcome": "succeeded",
            }
            for provider in ("alpha", "beta")
        ]
    )

    result = KPIQueryService(store).query(
        "providers",
        {"window_seconds": 3_600, "limit": 1},
    )

    assert len(result["providers"]) == 1
    assert result["truncated"] is True
    store.close()
