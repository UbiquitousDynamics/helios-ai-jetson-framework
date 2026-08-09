from __future__ import annotations

import base64
import http.client
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import config
from observability.aggregate import KPIQueryService
from observability.dashboard import API_RESOURCES, DashboardServer
from observability.service import ObservabilityService
from observability.storage import SQLiteKPIStore
from scripts.kpi import _DashboardQueryAdapter


class FakeQueryService:
    def __init__(self, *, empty: bool = False) -> None:
        self.empty = empty
        self.calls: list[tuple[str, dict[str, object]]] = []

    def query(self, resource: str, parameters: Mapping[str, object]) -> object:
        self.calls.append((resource, dict(parameters)))
        if self.empty:
            return None
        if resource == "health":
            return {"status": "healthy", "local_available": True}
        if resource == "export" and parameters.get("format") == "csv":
            return [{"event": "llm_request", "latency_ms": 12.5}]
        return {
            "resource": resource,
            "parameters": dict(parameters),
            "data": [],
        }


@contextmanager
def running_server(
    service: object,
    **kwargs: Any,
) -> Iterator[DashboardServer]:
    server = DashboardServer(service, port=0, **kwargs)
    server.start()
    try:
        yield server
    finally:
        server.close()


def request(
    server: DashboardServer,
    path: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    host, port = server.address
    connection = http.client.HTTPConnection(host, port, timeout=2)
    try:
        connection.request(method, path, headers=dict(headers or {}))
        response = connection.getresponse()
        body = response.read()
        return (
            response.status,
            {name.lower(): value for name, value in response.headers.items()},
            body,
        )
    finally:
        connection.close()


def json_body(body: bytes) -> object:
    return json.loads(body.decode("utf-8"))


def test_all_versioned_api_routes_use_the_injected_query_service() -> None:
    service = FakeQueryService()
    with running_server(service) as server:
        for resource in sorted(API_RESOURCES):
            status, headers, body = request(server, f"/api/v1/kpi/{resource}")
            assert status == 200
            assert headers["content-type"].startswith("application/json")
            assert json_body(body)

    assert {resource for resource, _query in service.calls} == API_RESOURCES
    health_query = next(query for resource, query in service.calls if resource == "health")
    summary_query = next(query for resource, query in service.calls if resource == "summary")
    assert health_query == {}
    assert summary_query == {"window_seconds": 3_600}


def test_query_validation_rejects_unknown_duplicate_blank_and_unbounded_values() -> None:
    service = FakeQueryService()
    invalid_paths = (
        "/api/v1/kpi/summary?unknown=1",
        "/api/v1/kpi/summary?window=1h&window=6h",
        "/api/v1/kpi/summary?mode=",
        "/api/v1/kpi/summary?window=32d",
        "/api/v1/kpi/summary?start=2026-08-01T00%3A00%3A00Z",
        ("/api/v1/kpi/summary?start=2026-01-01T00%3A00%3A00Z&end=2026-08-01T00%3A00%3A00Z"),
        "/api/v1/kpi/summary?provider=contains+spaces",
        "/api/v1/kpi/timeseries?points=1001",
        "/api/v1/kpi/timeseries?metric=secret_content",
        "/api/v1/kpi/health?window=1h",
        "/api/v1/kpi/summary?window=1h%",
    )
    with running_server(service) as server:
        for path in invalid_paths:
            status, _headers, body = request(server, path)
            assert status == 400, path
            assert json_body(body) == {
                "error": {
                    "code": "invalid_query",
                    "message": "Request could not be served.",
                }
            }

    assert service.calls == []


def test_filters_are_typed_and_bounded_before_the_query_service() -> None:
    service = FakeQueryService()
    path = (
        "/api/v1/kpi/timeseries?window=6h&mode=talk&locality=remote"
        "&provider=openai-codex&model=gpt-5.6-terra&route=remote-talk"
        "&outcome=success&network_tier=good&metric=first_audio_ms&points=42"
    )
    with running_server(service) as server:
        status, _headers, _body = request(server, path)

    assert status == 200
    assert service.calls == [
        (
            "timeseries",
            {
                "window_seconds": 21_600,
                "mode": "talk",
                "locality": "remote",
                "provider": "openai-codex",
                "model": "gpt-5.6-terra",
                "route": "remote-talk",
                "outcome": "success",
                "network_tier": "good",
                "metric": "first_audio_ms",
                "points": 42,
            },
        )
    ]


def test_canonical_latency_metrics_are_allowed_on_both_read_routes() -> None:
    service = FakeQueryService()
    with running_server(service) as server:
        actual_status, _headers, _body = request(
            server,
            "/api/v1/kpi/latency?window=1h&metric=actual_first_audio_ms",
        )
        listening_status, _headers, _body = request(
            server,
            "/api/v1/kpi/timeseries?window=1h&metric=listening_ms&points=20",
        )
        synthesis_status, _headers, _body = request(
            server,
            "/api/v1/kpi/latency?window=1h&metric=tts_synthesis_ms",
        )

    assert actual_status == listening_status == synthesis_status == 200
    assert [call[1]["metric"] for call in service.calls] == [
        "actual_first_audio_ms",
        "listening_ms",
        "tts_synthesis_ms",
    ]


def test_bearer_auth_is_optional_and_protects_static_and_api_routes() -> None:
    service = FakeQueryService()
    with running_server(service, auth_token="correct-token") as server:
        missing, missing_headers, missing_body = request(server, "/")
        wrong, _wrong_headers, _wrong_body = request(
            server,
            "/api/v1/kpi/health",
            headers={"Authorization": "Bearer wrong-token"},
        )
        accepted, _accepted_headers, accepted_body = request(
            server,
            "/api/v1/kpi/health",
            headers={"Authorization": "Bearer correct-token"},
        )

    assert missing == 401
    assert 'Basic realm="helios-kpi"' in missing_headers["www-authenticate"]
    assert 'Bearer realm="helios-kpi"' in missing_headers["www-authenticate"]
    assert json_body(missing_body)["error"]["code"] == "unauthorized"
    assert wrong == 401
    assert accepted == 200
    assert json_body(accepted_body)["status"] == "healthy"


def test_browser_compatible_basic_auth_reuses_the_configured_secret() -> None:
    service = FakeQueryService()

    def basic(username: str, password: str) -> dict[str, str]:
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        return {"Authorization": f"Basic {credentials}"}

    with running_server(service, auth_token="correct-token") as server:
        static_status, _static_headers, _static_body = request(
            server,
            "/",
            headers=basic("helios", "correct-token"),
        )
        api_status, _api_headers, api_body = request(
            server,
            "/api/v1/kpi/health",
            headers=basic("helios", "correct-token"),
        )
        wrong_user, _wrong_headers, _wrong_body = request(
            server,
            "/",
            headers=basic("admin", "correct-token"),
        )
        malformed, _malformed_headers, _malformed_body = request(
            server,
            "/",
            headers={"Authorization": "Basic not-base64"},
        )

    assert static_status == api_status == 200
    assert json_body(api_body)["status"] == "healthy"
    assert wrong_user == malformed == 401


def test_only_get_and_head_are_allowed_and_head_has_no_body() -> None:
    service = FakeQueryService()
    with running_server(service) as server:
        post_status, post_headers, post_body = request(
            server,
            "/api/v1/kpi/summary",
            method="POST",
        )
        head_status, head_headers, head_body = request(
            server,
            "/api/v1/kpi/summary",
            method="HEAD",
        )
        extension_status, extension_headers, extension_body = request(
            server,
            "/api/v1/kpi/summary",
            method="PROPFIND",
        )

    assert post_status == 405
    assert post_headers["allow"] == "GET, HEAD"
    assert json_body(post_body)["error"]["code"] == "method_not_allowed"
    assert head_status == 200
    assert int(head_headers["content-length"]) > 0
    assert head_body == b""
    assert extension_status == 405
    assert extension_headers["allow"] == "GET, HEAD"
    assert extension_headers["x-content-type-options"] == "nosniff"
    assert json_body(extension_body)["error"]["code"] == "method_not_allowed"


def test_security_headers_and_exact_static_allowlist() -> None:
    service = FakeQueryService()
    with running_server(service) as server:
        status, headers, html = request(server, "/")
        css_status, _css_headers, css = request(server, "/dashboard.css")
        js_status, _js_headers, javascript = request(server, "/dashboard.js")
        missing_status, _missing_headers, _missing = request(server, "/favicon.ico")
        traversal_status, _traversal_headers, _traversal = request(server, "/static/../config.py")

    assert status == css_status == js_status == 200
    assert missing_status == traversal_status == 404
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in headers["content-security-policy"]
    page = html.decode("utf-8")
    for visible_section in ("Overview", "Routing", "Latency", "Network", "Jetson"):
        assert visible_section in page
    assert "Local versus remote routing decisions" in page
    assert b"No metrics have been recorded yet" in html
    assert b"innerHTML" not in javascript
    assert b"https://" not in html
    for canonical_key in (
        b"actual_first_audio_ms",
        b"network_quality_score",
        b"network_state",
        b"timestamp_ms",
        b"circuit.state",
    ):
        assert canonical_key in javascript
    assert b'value === null || value === undefined || value === ""' in javascript
    assert b"statisticsWithSamples" in javascript
    assert b"groupedStatisticsWithSamples" in javascript
    assert b"drawMultiLineChart(" in javascript
    assert b'pathValue(latency, ["breakdown"])' in javascript
    for latency_series in (
        b'{ metric: "end_to_end_ms", points: 180 }',
        b'{ metric: "first_token_ms", points: 180 }',
        b'{ metric: "actual_first_audio_ms", points: 180 }',
    ):
        assert latency_series in javascript
    assert b"observedAvailability" in javascript
    assert b"observedSuccesses" in javascript
    assert b"failures.push(names[index])" in javascript
    assert b'unavailable: ${failures.join(", ")}' in javascript
    assert b"Rejected Candidates" in javascript
    assert b"(success_rate|probe_success_ratio)" in javascript
    assert css


def test_health_endpoint_merges_cached_runtime_provider_state() -> None:
    service = ObservabilityService(config.KPISettings())
    service.set_runtime_health_provider(
        lambda: {
            "status": "healthy",
            "local_available": True,
            "remote_available": False,
            "providers": [
                {
                    "provider": "ollama",
                    "model": "local-model",
                    "locality": "local",
                    "enabled": True,
                    "available": True,
                    "circuit_state": "available",
                }
            ],
        }
    )
    try:
        with running_server(service) as server:
            status, _headers, body = request(server, "/api/v1/kpi/health")
    finally:
        service.close()

    payload = json_body(body)
    assert status == 200
    assert isinstance(payload, dict)
    assert payload["status"] == "healthy"
    assert payload["local_available"] is True
    assert payload["remote_available"] is False
    assert payload["providers"][0]["circuit_state"] == "available"


def test_health_endpoint_keeps_runtime_state_when_storage_status_fails() -> None:
    class BrokenHealthQuery:
        def query(self, resource: str, parameters: Mapping[str, object]) -> object:
            assert resource == "health"
            assert not parameters
            raise RuntimeError("storage unavailable")

    service = ObservabilityService(config.KPISettings())
    service.query_service = BrokenHealthQuery()  # type: ignore[assignment]
    service.set_runtime_health_provider(
        lambda: {
            "status": "healthy",
            "local_available": True,
            "remote_available": True,
            "providers": [],
        }
    )
    try:
        with running_server(service) as server:
            status, _headers, body = request(server, "/api/v1/kpi/health")
    finally:
        service.close()

    payload = json_body(body)
    assert status == 200
    assert payload["storage_available"] is False
    assert payload["status"] == "healthy"
    assert payload["local_available"] is True
    assert payload["remote_available"] is True


def test_empty_query_response_is_stable_and_dashboard_usable() -> None:
    service = FakeQueryService(empty=True)
    with running_server(service) as server:
        status, _headers, body = request(server, "/api/v1/kpi/summary")

    assert status == 200
    assert json_body(body) == {"data": [], "empty": True}


def test_csv_export_is_bounded_and_downloadable() -> None:
    service = FakeQueryService()
    with running_server(service, maximum_export_rows=25) as server:
        status, headers, body = request(server, "/api/v1/kpi/export?format=csv&limit=25")
        invalid_status, _invalid_headers, _invalid_body = request(
            server,
            "/api/v1/kpi/export?format=csv&limit=26",
        )

    assert status == 200
    assert headers["content-type"].startswith("text/csv")
    assert headers["content-disposition"] == 'attachment; filename="helios-kpi.csv"'
    assert body.decode("utf-8").splitlines() == [
        "event,latency_ms",
        "llm_request,12.5",
    ]
    assert invalid_status == 400


def test_cli_dashboard_json_export_is_an_array_not_a_json_string(tmp_path: Path) -> None:
    store = SQLiteKPIStore(tmp_path / "kpi.sqlite3", maintenance_interval_seconds=10_000)
    store.write({"event": "llm_request_succeeded", "count": 1})
    service = _DashboardQueryAdapter(store, KPIQueryService(store))
    with running_server(service, maximum_export_rows=25) as server:
        status, headers, body = request(
            server,
            "/api/v1/kpi/export?format=json&limit=25",
        )
    store.close()

    payload = json_body(body)
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert isinstance(payload, list)
    assert payload[0]["event"] == "llm_request_succeeded"


def test_csv_rows_are_neutralized_against_spreadsheet_formulas() -> None:
    class FormulaQueryService(FakeQueryService):
        def query(self, resource: str, parameters: Mapping[str, object]) -> object:
            if resource == "export":
                return [{"provider": '=WEBSERVICE("https://invalid")'}]
            return super().query(resource, parameters)

    with running_server(FormulaQueryService()) as server:
        status, _headers, body = request(
            server,
            "/api/v1/kpi/export?format=csv&limit=1",
        )

    assert status == 200
    assert "'=WEBSERVICE" in body.decode("utf-8").splitlines()[1]


def test_lan_binding_requires_explicit_permission_and_authentication() -> None:
    service = FakeQueryService()
    with pytest.raises(ValueError, match="allow_lan"):
        DashboardServer(service, host="0.0.0.0", port=0)
    with pytest.raises(ValueError, match="authentication"):
        DashboardServer(service, host="0.0.0.0", port=0, allow_lan=True)
    with pytest.raises(ValueError, match="at least 24"):
        DashboardServer(
            service,
            host="0.0.0.0",
            port=0,
            allow_lan=True,
            auth_token="too-short",
        )


def test_static_directory_must_contain_the_exact_assets(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        DashboardServer(FakeQueryService(), port=0, static_directory=tmp_path)
