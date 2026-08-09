"""Lifecycle owner for optional KPI persistence, sampling, and dashboard."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from api.metrics import FanoutMetricSink, SafeMetricsRecorder, record_safely
from observability.activity import InferenceActivityTracker
from observability.aggregate import KPIQueryService
from observability.dashboard import DashboardServer
from observability.resources import ResourceCollector, ResourceSampler, ResourceSnapshot
from observability.storage import SQLiteKPIStore

logger = logging.getLogger(__name__)
_MEBIBYTE = 1024 * 1024


class ObservabilityService:
    """Own every optional KPI resource and shut it down in producer-first order."""

    def __init__(
        self,
        settings: Any,
        *,
        additional_sinks: Iterable[Any] = (),
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.activity_tracker = InferenceActivityTracker()
        self.store: SQLiteKPIStore | None = None
        self.query_service: KPIQueryService | None = None
        self.sampler: ResourceSampler | None = None
        self.dashboard: DashboardServer | None = None
        self._runtime_health_provider: Callable[[], Mapping[str, object]] | None = None
        self._runtime_health_lock = threading.Lock()
        self._closed = False

        persistent = bool(settings.enabled or settings.dashboard_enabled)
        if persistent:
            self.store = SQLiteKPIStore(
                settings.storage_path,
                raw_retention_days=settings.raw_retention_days,
                rollup_retention_days=settings.rollup_retention_days,
                max_size_bytes=settings.maximum_database_mb * _MEBIBYTE,
                rollup_interval_seconds=settings.rollup_interval_seconds,
                export_max_rows=settings.maximum_export_rows,
            )
            self.query_service = KPIQueryService(self.store)

        sinks = list(additional_sinks)
        if settings.enabled and self.store is not None:
            sinks.insert(0, self.store)
        sink = FanoutMetricSink(sinks) if sinks else None
        self.recorder = SafeMetricsRecorder(
            enabled=sink is not None,
            sink=sink,
            retain=0,
            asynchronous=sink is not None,
            queue_size=settings.queue_size,
            batch_size=settings.batch_size,
            flush_interval_seconds=settings.flush_interval_seconds,
        )

        if settings.enabled:
            collector = ResourceCollector(
                disk_path=Path(settings.storage_path).parent,
            )
            self.sampler = ResourceSampler(
                collector,
                self._record_resource,
                interval_seconds=settings.resource_sample_interval_seconds,
            ).start()

        if settings.dashboard_enabled and self.query_service is not None:
            self._start_dashboard(os.environ if environ is None else environ)

    def _start_dashboard(self, environ: Mapping[str, str]) -> None:
        token = None
        token_env = self.settings.dashboard_auth_token_env
        if token_env is not None:
            token = environ.get(token_env, "").strip() or None
            if token is None:
                logger.warning("KPI dashboard authentication is unavailable; dashboard disabled")
                return
        if self.settings.dashboard_allow_lan and (token is None or len(token) < 24):
            logger.warning("KPI dashboard LAN authentication is too weak; dashboard disabled")
            return
        try:
            self.dashboard = DashboardServer(
                self,
                host=self.settings.dashboard_host,
                port=self.settings.dashboard_port,
                allow_lan=self.settings.dashboard_allow_lan,
                auth_token=token,
                export_enabled=self.settings.export_enabled,
                maximum_query_days=self.settings.maximum_query_days,
                maximum_query_points=self.settings.maximum_query_points,
                maximum_export_rows=self.settings.maximum_export_rows,
            )
            self.dashboard.start()
        except Exception:
            self.dashboard = None
            logger.warning("KPI dashboard could not start; metric collection remains active")

    @staticmethod
    def _percentage(used: int | None, total: int | None) -> float | None:
        if used is None or total is None or total <= 0:
            return None
        return min(100.0, max(0.0, used / total * 100.0))

    def _record_resource(self, snapshot: ResourceSnapshot) -> None:
        activity = self.activity_tracker.snapshot()
        storage_used_mb = None
        if self.store is not None:
            try:
                status = self.store.status()
                size = status.get("total_size_bytes") or status.get("size_bytes")
                if isinstance(size, int) and size >= 0:
                    storage_used_mb = size / _MEBIBYTE
            except Exception:
                pass
        record_safely(
            self.recorder,
            "resource_sample",
            timestamp=snapshot.timestamp,
            mode=activity.mode,
            locality=activity.locality,
            provider=activity.provider,
            model=activity.model,
            route=activity.route,
            resource_scope="inference" if activity.active else "idle",
            outcome="available" if snapshot.available else "unavailable",
            success=snapshot.available,
            cpu_percent=snapshot.cpu_percent,
            gpu_percent=snapshot.gpu_percent,
            memory_percent=self._percentage(
                snapshot.memory_used_bytes,
                snapshot.memory_total_bytes,
            ),
            swap_percent=self._percentage(
                snapshot.swap_used_bytes,
                snapshot.swap_total_bytes,
            ),
            memory_used_mb=(
                snapshot.memory_used_bytes / _MEBIBYTE
                if snapshot.memory_used_bytes is not None
                else None
            ),
            memory_total_mb=(
                snapshot.memory_total_bytes / _MEBIBYTE
                if snapshot.memory_total_bytes is not None
                else None
            ),
            swap_used_mb=(
                snapshot.swap_used_bytes / _MEBIBYTE
                if snapshot.swap_used_bytes is not None
                else None
            ),
            swap_total_mb=(
                snapshot.swap_total_bytes / _MEBIBYTE
                if snapshot.swap_total_bytes is not None
                else None
            ),
            cpu_temperature_c=snapshot.cpu_temperature_c,
            gpu_temperature_c=snapshot.gpu_temperature_c,
            power_w=snapshot.power_mw / 1_000 if snapshot.power_mw is not None else None,
            storage_used_mb=storage_used_mb,
            cpu_frequency_mhz=snapshot.cpu_frequency_mhz,
            gpu_frequency_mhz=snapshot.gpu_frequency_mhz,
            throttled=snapshot.throttled,
        )

    def set_runtime_health_provider(
        self,
        provider: Callable[[], Mapping[str, object]] | None,
    ) -> None:
        """Register a read-only, cached assistant-health snapshot source."""

        if provider is not None and not callable(provider):
            raise TypeError("runtime health provider must be callable")
        with self._runtime_health_lock:
            self._runtime_health_provider = provider

    def _runtime_health(self) -> dict[str, object]:
        unavailable: dict[str, object] = {
            "status": "unknown",
            "local_available": False,
            "remote_available": False,
            "providers": [],
        }
        with self._runtime_health_lock:
            provider = self._runtime_health_provider
        if provider is None:
            return unavailable
        try:
            result = provider()
        except Exception:
            logger.warning("Runtime provider health snapshot is unavailable")
            return unavailable
        if not isinstance(result, Mapping):
            logger.warning("Runtime provider health snapshot is invalid")
            return unavailable
        return {**unavailable, **dict(result)}

    def query(self, resource: str, parameters: Mapping[str, object]) -> object:
        if resource != "health":
            return (
                self.query_service.query(resource, parameters)
                if self.query_service is not None
                else {}
            )
        try:
            result = (
                self.query_service.query(resource, parameters)
                if self.query_service is not None
                else {}
            )
        except Exception:
            # Runtime provider health remains useful even when the storage
            # status read is temporarily unavailable.
            logger.warning("KPI storage health snapshot is unavailable")
            result = {"storage_available": False}
        if not isinstance(result, Mapping):
            result = {"storage_available": False}
        return {
            **dict(result),
            **self._runtime_health(),
            "recorder": self.recorder.stats().as_dict(),
            "dashboard_enabled": self.dashboard is not None,
            "resource_sampler_running": self.sampler.running if self.sampler is not None else False,
        }

    def status(self) -> dict[str, Any]:
        storage = self.store.status() if self.store is not None else {"available": False}
        return {
            "storage": storage,
            "recorder": self.recorder.stats().as_dict(),
            "dashboard": {
                "enabled": self.dashboard is not None,
                "address": self.dashboard.address if self.dashboard is not None else None,
            },
            "resource_sampler_running": self.sampler.running if self.sampler is not None else False,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.set_runtime_health_provider(None)
        if self.dashboard is not None:
            self.dashboard.close()
        if self.sampler is not None:
            self.sampler.stop()
        recorder_stopped = self.recorder.close()
        if self.store is not None and recorder_stopped:
            self.store.close()
        elif self.store is not None:
            # A stuck sink cannot be killed safely. Keep its connection valid
            # rather than racing the still-running daemon with a closed store.
            logger.warning("KPI storage remains open because metric shutdown timed out")

    def __enter__(self) -> ObservabilityService:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["ObservabilityService"]
