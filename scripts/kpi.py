"""Inspect, export, clear, or serve Helios' optional content-free KPI store."""

from __future__ import annotations

import argparse
import csv
import inspect
import io
import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _json_default(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TypeError("naive datetime")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported KPI value: {type(value).__name__}")


def _json_text(value: object, *, pretty: bool) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        default=_json_default,
        ensure_ascii=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        sort_keys=True,
    )


def _settings() -> Any:
    import config

    return config.SETTINGS.kpi


@contextmanager
def _store(settings: Any) -> Iterator[Any]:
    # Imported lazily so ordinary assistant startup and ``--help`` never load
    # SQLite or initialize KPI storage.
    from observability.storage import SQLiteKPIStore

    store = SQLiteKPIStore(
        settings.storage_path,
        raw_retention_days=settings.raw_retention_days,
        rollup_retention_days=settings.rollup_retention_days,
        max_size_bytes=settings.maximum_database_mb * 1024 * 1024,
        rollup_interval_seconds=settings.rollup_interval_seconds,
        export_max_rows=settings.maximum_export_rows,
    )
    try:
        yield store
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


def _call_with_limit(operation: Any, limit: int) -> object:
    signature = inspect.signature(operation)
    for name in ("limit", "maximum_rows", "max_rows"):
        if name in signature.parameters:
            return operation(**{name: limit})
    return operation()


def _csv_text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    rows = value.get("rows") if isinstance(value, Mapping) else value
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise TypeError("KPI CSV export returned an unsupported value")
    if not rows:
        return ""
    if not all(isinstance(row, Mapping) for row in rows):
        raise TypeError("KPI CSV export rows must be mappings")
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        assert isinstance(row, Mapping)
        for key in row:
            if not isinstance(key, str):
                raise TypeError("KPI CSV field names must be strings")
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        assert isinstance(row, Mapping)
        writer.writerow(
            {
                key: (
                    _json_text(row.get(key), pretty=False)
                    if isinstance(row.get(key), (Mapping, Sequence))
                    and not isinstance(row.get(key), (str, bytes, bytearray))
                    else row.get(key)
                )
                for key in fieldnames
            }
        )
    return output.getvalue()


def _write_export(text: str, destination: str) -> None:
    if destination == "-":
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")
        return
    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    print(f"KPI export written: {path}", file=sys.stderr)


def status_command(settings: Any) -> int:
    with _store(settings) as store:
        status = store.status()
    print(_json_text(status, pretty=True))
    return 0


def clear_command(settings: Any, *, confirmed: bool) -> int:
    if not confirmed:
        print("Refusing to clear KPI data without --yes.", file=sys.stderr)
        return 2
    with _store(settings) as store:
        result = store.clear()
    output = {"cleared": True}
    if result is not None:
        output["result"] = result
    print(_json_text(output, pretty=True))
    return 0


def export_command(
    settings: Any,
    *,
    export_format: str,
    destination: str,
    limit: int | None,
) -> int:
    if not settings.export_enabled:
        print("KPI export is disabled by configuration.", file=sys.stderr)
        return 2
    selected_limit = settings.maximum_export_rows if limit is None else limit
    if not 1 <= selected_limit <= settings.maximum_export_rows:
        print(
            f"Export limit must be between 1 and {settings.maximum_export_rows}.",
            file=sys.stderr,
        )
        return 2
    with _store(settings) as store:
        operation = store.export_json if export_format == "json" else store.export_csv
        result = _call_with_limit(operation, selected_limit)
    text = (
        result.decode("utf-8")
        if isinstance(result, bytes)
        else result
        if isinstance(result, str)
        else _json_text(result, pretty=True)
        if export_format == "json"
        else _csv_text(result)
    )
    _write_export(text, destination)
    return 0


class _DashboardQueryAdapter:
    """Bridge the dashboard's stable route API to storage and aggregates."""

    def __init__(self, store: Any, aggregates: Any) -> None:
        self.store = store
        self.aggregates = aggregates

    def query(self, resource: str, parameters: Mapping[str, object]) -> object:
        if resource == "health":
            # Do not expose filesystem paths or internal storage details over HTTP.
            self.store.status()
            return {"status": "healthy", "storage_available": True}
        return self.aggregates.query(resource, dict(parameters))


def _auth_token(settings: Any, override_name: str | None) -> str | None:
    environment_name = override_name or settings.dashboard_auth_token_env
    if environment_name is None:
        return None
    token = os.environ.get(environment_name)
    if not token:
        raise RuntimeError("configured dashboard authentication token is unavailable")
    return token


def serve_command(
    settings: Any,
    *,
    host: str | None,
    port: int | None,
    allow_lan: bool,
    auth_token_env: str | None,
) -> int:
    from observability.aggregate import KPIQueryService
    from observability.dashboard import DashboardServer

    selected_host = settings.dashboard_host if host is None else host
    selected_port = settings.dashboard_port if port is None else port
    selected_allow_lan = settings.dashboard_allow_lan or allow_lan
    token = _auth_token(settings, auth_token_env)
    with _store(settings) as store:
        aggregates = KPIQueryService(store)
        query_service = _DashboardQueryAdapter(store, aggregates)
        with DashboardServer(
            query_service,
            host=selected_host,
            port=selected_port,
            allow_lan=selected_allow_lan,
            auth_token=token,
            export_enabled=settings.export_enabled,
            maximum_query_days=settings.maximum_query_days,
            maximum_query_points=settings.maximum_query_points,
            maximum_export_rows=settings.maximum_export_rows,
        ) as server:
            bound_host, bound_port = server.address
            print(f"Helios KPI dashboard: http://{bound_host}:{bound_port}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nStopping Helios KPI dashboard.", file=sys.stderr)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage Helios' optional content-free KPI storage and dashboard."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show sanitized KPI storage status as JSON.")

    clear_parser = commands.add_parser("clear", help="Explicitly remove all KPI data.")
    clear_parser.add_argument("--yes", action="store_true", help="Confirm the destructive clear.")

    export_parser = commands.add_parser("export", help="Export sanitized KPI rows.")
    export_parser.add_argument("format", choices=("json", "csv"), help="Export encoding.")
    export_parser.add_argument(
        "--output",
        default="-",
        metavar="PATH",
        help="Destination path, or '-' for standard output (default).",
    )
    export_parser.add_argument("--limit", type=int, help="Maximum exported rows.")

    serve_parser = commands.add_parser("serve", help="Run the read-only KPI dashboard.")
    serve_parser.add_argument("--host", help="Bind host; defaults to configured localhost.")
    serve_parser.add_argument("--port", type=int, help="Bind port; defaults to configuration.")
    serve_parser.add_argument(
        "--allow-lan",
        action="store_true",
        help="Explicitly allow a non-loopback bind; authenticated access is still required.",
    )
    serve_parser.add_argument(
        "--auth-token-env",
        metavar="ENV_NAME",
        help="Environment variable containing the Bearer/API or browser Basic password.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        settings = _settings()
        if arguments.command == "status":
            return status_command(settings)
        if arguments.command == "clear":
            return clear_command(settings, confirmed=arguments.yes)
        if arguments.command == "export":
            return export_command(
                settings,
                export_format=arguments.format,
                destination=arguments.output,
                limit=arguments.limit,
            )
        return serve_command(
            settings,
            host=arguments.host,
            port=arguments.port,
            allow_lan=arguments.allow_lan,
            auth_token_env=arguments.auth_token_env,
        )
    except Exception as exc:
        print(f"KPI command failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
