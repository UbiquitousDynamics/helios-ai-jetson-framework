"""Local-first, read-only HTTP dashboard for sanitized Helios KPI data."""

from __future__ import annotations

import base64
import binascii
import csv
import hmac
import io
import ipaddress
import json
import logging
import math
import re
import socket
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1/kpi/"
API_RESOURCES = frozenset(
    {
        "health",
        "summary",
        "timeseries",
        "latency",
        "routing",
        "providers",
        "network",
        "resources",
        "export",
    }
)

_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}
_COMMON_FILTERS = frozenset(
    {
        "window",
        "start",
        "end",
        "mode",
        "locality",
        "provider",
        "model",
        "route",
        "outcome",
        "network_tier",
    }
)
_QUERY_KEYS = {
    "health": frozenset(),
    "summary": _COMMON_FILTERS,
    "timeseries": _COMMON_FILTERS | {"metric", "points"},
    "latency": _COMMON_FILTERS | {"metric"},
    "routing": _COMMON_FILTERS,
    "providers": _COMMON_FILTERS | {"limit"},
    "network": _COMMON_FILTERS | {"points"},
    "resources": _COMMON_FILTERS | {"points"},
    "export": _COMMON_FILTERS | {"format", "limit"},
}
_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$")
_WINDOW_PATTERN = re.compile(r"^([1-9][0-9]*)([mhd])$")
_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_LATENCY_METRICS = frozenset(
    {
        "latency_ms",
        "end_to_end_ms",
        "first_token_ms",
        "first_audio_ms",
        "actual_first_audio_ms",
        "listening_ms",
        "speech_finalization_ms",
        "listen_ms",
        "stt_ms",
        "rag_ms",
        "routing_ms",
        "inference_ms",
        "tts_synthesis_ms",
        "tts_ms",
        "speech_dispatch_ms",
        "audio_playback_ms",
        "audio_duration_ms",
        "playback_ms",
        "dns_ms",
        "tcp_ms",
        "connect_ms",
        "tls_ms",
        "ttfb_ms",
    }
)
_TIMESERIES_METRICS = _LATENCY_METRICS | {
    "requests",
    "success_rate",
    "fallback_rate",
    "error_rate",
    "network_quality",
    "ttfb_ms",
    "goodput_kbps",
    "cpu_percent",
    "gpu_percent",
    "memory_percent",
    "temperature_c",
    "power_w",
}
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "answer",
        "api_key",
        "attempt_id",
        "auth_token",
        "authorization",
        "content",
        "context",
        "cookie",
        "credential",
        "credentials",
        "endpoint",
        "headers",
        "ip",
        "ip_address",
        "messages",
        "network_address",
        "password",
        "prompt",
        "refresh_token",
        "request_id",
        "response",
        "secret",
        "token_secret",
        "transcript",
        "uri",
        "url",
    }
)
_CSP = (
    "default-src 'self'; base-uri 'none'; connect-src 'self'; form-action 'none'; "
    "frame-ancestors 'none'; img-src 'self' data:; object-src 'none'; "
    "script-src 'self'; style-src 'self'"
)
_MAX_TARGET_LENGTH = 4_096
_MAX_QUERY_FIELDS = 24
_MAX_RESPONSE_BYTES = 8 * 1_024 * 1_024


class DashboardQueryService(Protocol):
    """Read-only query boundary consumed by :class:`DashboardServer`.

    Implementations must return sanitized JSON-compatible values. For CSV
    export they may return text, bytes, a sequence of flat mappings, or a
    mapping containing a ``rows`` sequence.
    """

    def query(self, resource: str, parameters: Mapping[str, object]) -> object: ...


class QueryValidationError(ValueError):
    """A client supplied an invalid, duplicate, or unbounded query."""


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _valid_percent_encoding(value: str) -> bool:
    index = 0
    while True:
        index = value.find("%", index)
        if index < 0:
            return True
        if index + 2 >= len(value):
            return False
        if value[index + 1] not in _HEX_DIGITS or value[index + 2] not in _HEX_DIGITS:
            return False
        index += 3


def _parse_timestamp(value: str) -> str:
    if len(value) > 40 or not _RFC3339_PATTERN.fullmatch(value):
        raise QueryValidationError
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise QueryValidationError from None
    if parsed.tzinfo is None:
        raise QueryValidationError
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_integer(value: str, maximum: int) -> int:
    if not value.isascii() or not value.isdigit() or value.startswith("0"):
        raise QueryValidationError
    parsed = int(value)
    if not 1 <= parsed <= maximum:
        raise QueryValidationError
    return parsed


def _window_seconds(value: str, maximum_query_days: int) -> int:
    match = _WINDOW_PATTERN.fullmatch(value)
    if match is None:
        raise QueryValidationError
    amount = int(match.group(1))
    multiplier = {"m": 60, "h": 3_600, "d": 86_400}[match.group(2)]
    seconds = amount * multiplier
    if seconds > maximum_query_days * 86_400:
        raise QueryValidationError
    return seconds


def _parse_query(
    raw_query: str,
    resource: str,
    *,
    maximum_query_days: int,
    maximum_query_points: int,
    maximum_export_rows: int,
) -> dict[str, object]:
    if not _valid_percent_encoding(raw_query):
        raise QueryValidationError
    if not raw_query:
        # Python 3.10.0 raises for an empty value with strict parsing while
        # later patch releases return an empty list. Keep dashboard behavior
        # stable on the Python version shipped by older JetPack images.
        pairs: list[tuple[str, str]] = []
    else:
        try:
            pairs = parse_qsl(
                raw_query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=_MAX_QUERY_FIELDS,
            )
        except ValueError:
            raise QueryValidationError from None

    values: dict[str, str] = {}
    for name, value in pairs:
        if name in values or name not in _QUERY_KEYS[resource] or not value:
            raise QueryValidationError
        values[name] = value

    parsed: dict[str, object] = {}
    has_start = "start" in values
    has_end = "end" in values
    if has_start != has_end or ("window" in values and has_start):
        raise QueryValidationError
    if resource != "health":
        if has_start:
            start = _parse_timestamp(values.pop("start"))
            end = _parse_timestamp(values.pop("end"))
            start_value = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_value = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if end_value <= start_value:
                raise QueryValidationError
            if (end_value - start_value).total_seconds() > maximum_query_days * 86_400:
                raise QueryValidationError
            parsed.update(start=start, end=end)
        else:
            parsed["window_seconds"] = _window_seconds(
                values.pop("window", "1h"),
                maximum_query_days,
            )

    choices = {
        "mode": frozenset({"talk", "think"}),
        "locality": frozenset({"local", "remote"}),
        "outcome": frozenset({"success", "failure"}),
        "network_tier": frozenset({"unknown", "offline", "poor", "fair", "good", "excellent"}),
        "format": frozenset({"json", "csv"}),
    }
    for name, allowed in choices.items():
        if name in values:
            value = values.pop(name)
            if value not in allowed:
                raise QueryValidationError
            parsed[name] = value

    for name in ("provider", "model", "route"):
        if name in values:
            value = values.pop(name)
            if not _LABEL_PATTERN.fullmatch(value):
                raise QueryValidationError
            parsed[name] = value

    if "metric" in values:
        metric = values.pop("metric")
        allowed_metrics = _LATENCY_METRICS if resource == "latency" else _TIMESERIES_METRICS
        if metric not in allowed_metrics:
            raise QueryValidationError
        parsed["metric"] = metric

    if "points" in values:
        parsed["points"] = _positive_integer(
            values.pop("points"),
            maximum_query_points,
        )
    elif resource in {"timeseries", "network", "resources"}:
        parsed["points"] = min(300, maximum_query_points)

    if "limit" in values:
        maximum = maximum_export_rows if resource == "export" else min(1_000, maximum_export_rows)
        parsed["limit"] = _positive_integer(values.pop("limit"), maximum)
    elif resource == "export":
        parsed["limit"] = min(1_000, maximum_export_rows)
    elif resource == "providers":
        parsed["limit"] = min(100, maximum_export_rows)

    if resource == "export" and "format" not in parsed:
        parsed["format"] = "json"
    if values:
        raise QueryValidationError
    return parsed


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TypeError("naive datetime")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported KPI response value: {type(value).__name__}")


def _validate_safe_payload(value: object, *, depth: int = 0) -> None:
    if depth > 16:
        raise ValueError("KPI response is too deeply nested")
    if value is None or isinstance(value, (bool, int, str, Decimal, datetime, Enum)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("KPI response contains a non-finite value")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or key.lower() in _SENSITIVE_KEYS:
                raise ValueError("KPI response contains a prohibited field")
            _validate_safe_payload(child, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _validate_safe_payload(child, depth=depth + 1)
        return
    raise TypeError(f"unsupported KPI response value: {type(value).__name__}")


def _json_bytes(value: object) -> bytes:
    _validate_safe_payload(value)
    return json.dumps(
        value,
        allow_nan=False,
        default=_json_default,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _csv_cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    if isinstance(value, (str, int, float, Decimal, bool)):
        return value
    return json.dumps(value, default=_json_default, ensure_ascii=True, sort_keys=True)


def _csv_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    rows: object = value.get("rows") if isinstance(value, Mapping) else value
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise TypeError("CSV export must be text, bytes, or a row sequence")
    if not rows:
        return b""
    if not all(isinstance(row, Mapping) for row in rows):
        raise TypeError("CSV export rows must be mappings")
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        assert isinstance(row, Mapping)
        for key in row:
            if not isinstance(key, str) or key.lower() in _SENSITIVE_KEYS:
                raise ValueError("CSV export contains a prohibited field")
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        assert isinstance(row, Mapping)
        writer.writerow({key: _csv_cell(row.get(key)) for key in fieldnames})
    return output.getvalue().encode("utf-8")


class _DashboardHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _IPv6DashboardHTTPServer(_DashboardHTTPServer):
    address_family = socket.AF_INET6


class _DashboardHandler(BaseHTTPRequestHandler):
    server: _DashboardHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._serve(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self._serve(send_body=False)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_TRACE(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_CONNECT(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def _serve(self, *, send_body: bool) -> None:
        if len(self.path) > _MAX_TARGET_LENGTH:
            self._error(HTTPStatus.REQUEST_URI_TOO_LONG, "request_too_large", send_body)
            return
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", send_body)
            return
        if not self._authorized():
            self._error(
                HTTPStatus.UNAUTHORIZED,
                "unauthorized",
                send_body,
                extra_headers={
                    "WWW-Authenticate": (
                        'Basic realm="helios-kpi", charset="UTF-8", Bearer realm="helios-kpi"'
                    )
                },
            )
            return

        if parsed.path in _STATIC_FILES:
            if parsed.query:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_query", send_body)
                return
            self._static(parsed.path, send_body)
            return
        if not parsed.path.startswith(API_PREFIX):
            self._error(HTTPStatus.NOT_FOUND, "not_found", send_body)
            return
        resource = parsed.path[len(API_PREFIX) :]
        if resource not in API_RESOURCES or "/" in resource:
            self._error(HTTPStatus.NOT_FOUND, "not_found", send_body)
            return
        if resource == "export" and not self.server.export_enabled:  # type: ignore[attr-defined]
            self._error(HTTPStatus.NOT_FOUND, "not_found", send_body)
            return
        try:
            query = _parse_query(
                parsed.query,
                resource,
                maximum_query_days=self.server.maximum_query_days,  # type: ignore[attr-defined]
                maximum_query_points=self.server.maximum_query_points,  # type: ignore[attr-defined]
                maximum_export_rows=self.server.maximum_export_rows,  # type: ignore[attr-defined]
            )
        except QueryValidationError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_query", send_body)
            return
        self._api(resource, query, send_body)

    def _authorized(self) -> bool:
        expected = self.server.auth_token  # type: ignore[attr-defined]
        if expected is None:
            return True
        header = self.headers.get("Authorization", "")
        if len(header) > 8_192:
            return False
        structurally_valid = False
        candidate = ""
        if header.startswith("Bearer "):
            candidate = header[7:]
            structurally_valid = bool(candidate) and " " not in candidate
        elif header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                decoded = ""
            username, separator, candidate = decoded.partition(":")
            structurally_valid = separator == ":" and username == "helios" and bool(candidate)
        matches = hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
        return structurally_valid and matches

    def _static(self, path: str, send_body: bool) -> None:
        filename, content_type = _STATIC_FILES[path]
        body = self.server.static_assets[filename]  # type: ignore[attr-defined]
        self._response(HTTPStatus.OK, content_type, body, send_body)

    def _api(self, resource: str, query: Mapping[str, object], send_body: bool) -> None:
        try:
            result = self._query(resource, query)
            if result is None:
                result = {"data": [], "empty": True}
            if resource == "export" and query.get("format") == "csv":
                body = _csv_bytes(result)
                content_type = "text/csv; charset=utf-8"
                filename = "helios-kpi.csv"
            else:
                body = _json_bytes(result)
                content_type = "application/json; charset=utf-8"
                filename = "helios-kpi.json" if resource == "export" else None
            if len(body) > _MAX_RESPONSE_BYTES:
                raise ValueError("KPI response exceeded the server bound")
        except Exception:
            # Exception messages are deliberately omitted: an implementation bug
            # must not turn an otherwise content-free dashboard into a log sink.
            logger.warning("KPI dashboard query failed (resource=%s)", resource)
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "query_unavailable", send_body)
            return
        headers = (
            {"Content-Disposition": f'attachment; filename="{filename}"'}
            if filename is not None
            else None
        )
        self._response(HTTPStatus.OK, content_type, body, send_body, extra_headers=headers)

    def _query(self, resource: str, parameters: Mapping[str, object]) -> object:
        service = self.server.query_service  # type: ignore[attr-defined]
        query = getattr(service, "query", None)
        if callable(query):
            return query(resource, dict(parameters))
        operation = getattr(service, resource, None)
        if not callable(operation):
            raise TypeError(f"query service does not implement {resource}")
        return operation(**dict(parameters))

    def _method_not_allowed(self) -> None:
        self._error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            send_body=True,
            extra_headers={"Allow": "GET, HEAD"},
        )

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Replace every stdlib-generated HTML error with the safe API envelope."""

        del message, explain
        if code == HTTPStatus.NOT_IMPLEMENTED and getattr(self, "command", None):
            self._method_not_allowed()
            return
        try:
            status = HTTPStatus(code)
        except ValueError:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        error_code = {
            HTTPStatus.BAD_REQUEST: "invalid_request",
            HTTPStatus.REQUEST_URI_TOO_LONG: "request_too_large",
            HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE: "request_too_large",
            HTTPStatus.HTTP_VERSION_NOT_SUPPORTED: "invalid_request",
        }.get(status, "request_failed")
        self._error(
            status,
            error_code,
            send_body=getattr(self, "command", None) != "HEAD",
        )

    def _error(
        self,
        status: HTTPStatus,
        code: str,
        send_body: bool,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        body = _json_bytes({"error": {"code": code, "message": "Request could not be served."}})
        self._response(
            status,
            "application/json; charset=utf-8",
            body,
            send_body,
            extra_headers=extra_headers,
        )

    def _response(
        self,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
        send_body: bool,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

    def log_message(self, format: str, *args: object) -> None:
        # The stdlib default includes the client address and raw request target.
        # Neither belongs in Helios logs, so access logging is intentionally off.
        return


class DashboardServer:
    """Own a bounded, local-first HTTP server over an injected query service."""

    def __init__(
        self,
        query_service: DashboardQueryService | object,
        *,
        host: str = "127.0.0.1",
        port: int = 8_765,
        allow_lan: bool = False,
        auth_token: str | None = None,
        export_enabled: bool = True,
        maximum_query_days: int = 31,
        maximum_query_points: int = 1_000,
        maximum_export_rows: int = 10_000,
        static_directory: str | Path | None = None,
    ) -> None:
        if not isinstance(host, str) or not host.strip() or any(char.isspace() for char in host):
            raise ValueError("dashboard host is invalid")
        host = host.strip()
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
            raise ValueError("dashboard port must be between zero and 65535")
        for name, value in (
            ("maximum_query_days", maximum_query_days),
            ("maximum_query_points", maximum_query_points),
            ("maximum_export_rows", maximum_export_rows),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if maximum_query_days > 90:
            raise ValueError("maximum_query_days cannot exceed 90")
        if maximum_query_points > 1_000:
            raise ValueError("maximum_query_points cannot exceed 1000")
        if maximum_export_rows > 100_000:
            raise ValueError("maximum_export_rows cannot exceed 100000")
        if not isinstance(allow_lan, bool) or not isinstance(export_enabled, bool):
            raise TypeError("dashboard switches must be booleans")
        if auth_token is not None:
            if (
                not isinstance(auth_token, str)
                or not auth_token
                or len(auth_token) > 4_096
                or any(ord(char) < 33 or ord(char) == 127 for char in auth_token)
            ):
                raise ValueError("dashboard authentication token is invalid")
        if not _is_loopback_host(host):
            if not allow_lan:
                raise ValueError("non-loopback dashboard binding requires allow_lan")
            if auth_token is None:
                raise ValueError("non-loopback dashboard binding requires authentication")
            if len(auth_token) < 24:
                raise ValueError(
                    "non-loopback dashboard authentication must contain at least 24 characters"
                )

        static_root = (
            Path(static_directory)
            if static_directory is not None
            else Path(__file__).resolve().parent / "static"
        )
        assets = {
            filename: (static_root / filename).read_bytes()
            for filename, _content_type in set(_STATIC_FILES.values())
        }
        server_type = _IPv6DashboardHTTPServer if ":" in host else _DashboardHTTPServer
        self._server = server_type((host, port), _DashboardHandler)
        self._server.query_service = query_service  # type: ignore[attr-defined]
        self._server.auth_token = auth_token  # type: ignore[attr-defined]
        self._server.export_enabled = export_enabled  # type: ignore[attr-defined]
        self._server.maximum_query_days = maximum_query_days  # type: ignore[attr-defined]
        self._server.maximum_query_points = maximum_query_points  # type: ignore[attr-defined]
        self._server.maximum_export_rows = maximum_export_rows  # type: ignore[attr-defined]
        self._server.static_assets = assets  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None
        self._serving = threading.Event()
        self._closed = False
        self._lock = threading.Lock()

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> threading.Thread:
        """Run the dashboard in one background thread and return it."""

        with self._lock:
            if self._closed:
                raise RuntimeError("dashboard server is closed")
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            thread = threading.Thread(
                target=self.serve_forever,
                name="helios-kpi-dashboard",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        self._serving.wait(timeout=1.0)
        return thread

    def serve_forever(self) -> None:
        if self._closed:
            raise RuntimeError("dashboard server is closed")
        self._serving.set()
        try:
            self._server.serve_forever(poll_interval=0.2)
        finally:
            self._serving.clear()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            serving = self._serving.is_set()
        if serving:
            self._server.shutdown()
        self._server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def __enter__(self) -> DashboardServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = [
    "API_PREFIX",
    "API_RESOURCES",
    "DashboardQueryService",
    "DashboardServer",
    "QueryValidationError",
]
