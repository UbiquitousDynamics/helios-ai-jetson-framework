"""Fast Linux connectivity gate and background network-quality estimator."""

from __future__ import annotations

import ipaddress
import logging
import math
import socket
import ssl
import struct
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from api.routing import Connectivity

logger = logging.getLogger(__name__)

_SIOCGIFADDR = 0x8915
_RTMGRP_LINK = 0x1
_RTMGRP_IPV4_IFADDR = 0x10
_RTMGRP_IPV4_ROUTE = 0x40
_RTMGRP_IPV6_IFADDR = 0x100
_RTMGRP_IPV6_ROUTE = 0x400
_ROUTE_EVENT_GROUPS = (
    _RTMGRP_LINK
    | _RTMGRP_IPV4_IFADDR
    | _RTMGRP_IPV4_ROUTE
    | _RTMGRP_IPV6_IFADDR
    | _RTMGRP_IPV6_ROUTE
)


class NetworkSettings(Protocol):
    enabled: bool
    probe_url: str
    probe_interval_seconds: float
    result_max_age_seconds: float
    probe_timeout_seconds: float
    probe_bytes: int
    goodput_probe_interval_seconds: float
    minimum_quality_score: float
    quality_hysteresis: float
    target_ttfb_ms: float
    target_jitter_ms: float
    minimum_goodput_kbps: float
    history_size: int
    require_wifi: bool
    interface_allowlist: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LinkState:
    supported: bool
    online: bool
    interface: str | None = None
    kind: str | None = None
    wifi_signal_dbm: float | None = None
    reason: str = "unknown"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    success: bool
    dns_ms: float | None = None
    connect_ms: float | None = None
    tls_ms: float | None = None
    ttfb_ms: float | None = None
    goodput_kbps: float | None = None
    bytes_received: int = 0
    reason: str = "probe_failed"


@dataclass(frozen=True, slots=True)
class NetworkQualitySnapshot:
    connectivity: Connectivity = Connectivity.UNKNOWN
    interface: str | None = None
    interface_kind: str | None = None
    reason: str = "not_measured"
    observed_at: float = 0.0
    quality_score: float | None = None
    dns_ms: float | None = None
    connect_ms: float | None = None
    tls_ms: float | None = None
    ttfb_ewma_ms: float | None = None
    jitter_ewma_ms: float | None = None
    goodput_ewma_kbps: float | None = None
    loss_ratio: float | None = None
    wifi_signal_dbm: float | None = None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii", errors="strict").strip()
    except (OSError, UnicodeError):
        return None


def _usable_address(value: str | None) -> bool:
    if not value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
    )


class LinuxNetworkInspector:
    """Read the kernel's selected default path without subprocesses or packets."""

    def __init__(
        self,
        *,
        route_path: str | Path = "/proc/net/route",
        ipv6_route_path: str | Path = "/proc/net/ipv6_route",
        ipv6_address_path: str | Path = "/proc/net/if_inet6",
        wireless_path: str | Path = "/proc/net/wireless",
        sys_class_net: str | Path = "/sys/class/net",
        ipv4_lookup: Callable[[str], str | None] | None = None,
        platform: str = sys.platform,
    ) -> None:
        self.route_path = Path(route_path)
        self.ipv6_route_path = Path(ipv6_route_path)
        self.ipv6_address_path = Path(ipv6_address_path)
        self.wireless_path = Path(wireless_path)
        self.sys_class_net = Path(sys_class_net)
        self._ipv4_lookup = ipv4_lookup or self._linux_ipv4_address
        self.platform = platform

    def inspect(
        self,
        *,
        require_wifi: bool = False,
        interface_allowlist: tuple[str, ...] = (),
    ) -> LinkState:
        if self.platform != "linux":
            return LinkState(False, False, reason="unsupported_platform")

        routes = self._default_routes()
        if interface_allowlist:
            allowed = set(interface_allowlist)
            routes = tuple(route for route in routes if route[1] in allowed)
        if not routes:
            return LinkState(True, False, reason="no_default_route")

        rejected_reason = "no_usable_interface"
        for _metric, interface in routes:
            kind = self._interface_kind(interface)
            if require_wifi and kind != "wifi":
                rejected_reason = "wifi_required"
                continue

            operstate = _read_text(self.sys_class_net / interface / "operstate")
            if operstate not in {"up", "unknown"}:
                rejected_reason = "interface_down"
                continue
            carrier = _read_text(self.sys_class_net / interface / "carrier")
            if carrier == "0":
                rejected_reason = "no_carrier"
                continue
            if not self._has_usable_address(interface):
                rejected_reason = "no_usable_ip"
                continue
            return LinkState(
                True,
                True,
                interface=interface,
                kind=kind,
                wifi_signal_dbm=self._wifi_signal(interface),
                reason="default_route_ready",
            )
        return LinkState(True, False, reason=rejected_reason)

    def _default_routes(self) -> tuple[tuple[int, str], ...]:
        routes: list[tuple[int, str]] = []
        content = _read_text(self.route_path)
        if content:
            for line in content.splitlines()[1:]:
                fields = line.split()
                if len(fields) < 8 or fields[1] != "00000000" or fields[7] != "00000000":
                    continue
                try:
                    flags = int(fields[3], 16)
                    metric = int(fields[6])
                except ValueError:
                    continue
                if flags & 0x1:
                    routes.append((metric, fields[0]))

        ipv6_content = _read_text(self.ipv6_route_path)
        if ipv6_content:
            for line in ipv6_content.splitlines():
                fields = line.split()
                if len(fields) < 10 or fields[0] != "0" * 32 or fields[1] != "00":
                    continue
                try:
                    metric = int(fields[5], 16)
                except ValueError:
                    continue
                routes.append((metric, fields[-1]))
        return tuple(sorted(set(routes), key=lambda item: (item[0], item[1])))

    def _has_usable_address(self, interface: str) -> bool:
        try:
            if _usable_address(self._ipv4_lookup(interface)):
                return True
        except OSError:
            pass
        content = _read_text(self.ipv6_address_path)
        if not content:
            return False
        for line in content.splitlines():
            fields = line.split()
            if len(fields) < 6 or fields[-1] != interface:
                continue
            try:
                address = str(ipaddress.IPv6Address(int(fields[0], 16)))
            except ValueError:
                continue
            if _usable_address(address):
                return True
        return False

    @staticmethod
    def _linux_ipv4_address(interface: str) -> str | None:
        if sys.platform != "linux":
            return None
        try:
            import fcntl

            request = struct.pack("256s", interface[:15].encode("ascii"))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
                response = fcntl.ioctl(channel.fileno(), _SIOCGIFADDR, request)
            return socket.inet_ntoa(response[20:24])
        except (ImportError, OSError, UnicodeError):
            return None

    def _interface_kind(self, interface: str) -> str:
        if (self.sys_class_net / interface / "wireless").exists() or interface.startswith("wl"):
            return "wifi"
        if interface.startswith(("ww", "ppp", "usb")):
            return "wwan"
        if interface.startswith(("eth", "en")):
            return "ethernet"
        return "other"

    def _wifi_signal(self, interface: str) -> float | None:
        content = _read_text(self.wireless_path)
        if not content:
            return None
        for line in content.splitlines()[2:]:
            if ":" not in line:
                continue
            name, values = line.split(":", maxsplit=1)
            if name.strip() != interface:
                continue
            fields = values.split()
            if len(fields) < 3:
                return None
            try:
                return float(fields[2].rstrip("."))
            except ValueError:
                return None
        return None


def tls_http_probe(
    url: str,
    *,
    timeout_seconds: float,
    maximum_bytes: int,
    measure_payload: bool = True,
    clock: Callable[[], float] = time.monotonic,
    ssl_context: ssl.SSLContext | None = None,
) -> ProbeResult:
    """Measure a bounded HTTPS path without depending on an HTTP client package."""

    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return ProbeResult(False, reason="invalid_probe_url")
    host = parsed.hostname
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    deadline = clock() + timeout_seconds

    def remaining() -> float:
        return max(0.001, deadline - clock())

    raw_socket: socket.socket | None = None
    started = clock()
    stage = "dns"
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        dns_done = clock()
        if not addresses:
            return ProbeResult(False, reason="dns_no_address")
        last_error: OSError | None = None
        connected_at = dns_done
        stage = "tcp"
        for family, socktype, protocol, _canonical, address in addresses:
            candidate = socket.socket(family, socktype, protocol)
            candidate.settimeout(remaining())
            try:
                candidate.connect(address)
            except OSError as error:
                candidate.close()
                last_error = error
                continue
            raw_socket = candidate
            connected_at = clock()
            break
        if raw_socket is None:
            raise last_error or OSError("connect failed")

        context = ssl_context or ssl.create_default_context()
        raw_socket.settimeout(remaining())
        stage = "tls"
        with context.wrap_socket(raw_socket, server_hostname=host) as secured:
            raw_socket = None
            tls_done = clock()
            stage = "http"
            method = "GET" if measure_payload else "HEAD"
            range_header = f"Range: bytes=0-{maximum_bytes - 1}\r\n" if measure_payload else ""
            request = (
                f"{method} {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "User-Agent: Helios-Connectivity/1\r\n"
                "Accept: */*\r\n"
                "Accept-Encoding: identity\r\n"
                f"{range_header}"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            secured.sendall(request)
            first = secured.recv(min(4096, maximum_bytes))
            first_byte_at = clock()
            if not first.startswith(b"HTTP/"):
                return ProbeResult(False, reason="invalid_https_response")

            payload = b""
            header_end = first.find(b"\r\n\r\n")
            if header_end >= 0:
                payload = first[header_end + 4 :]
            received = len(payload)
            payload_started = first_byte_at if received else None
            while measure_payload and received < maximum_bytes and clock() < deadline:
                secured.settimeout(remaining())
                chunk = secured.recv(min(8192, maximum_bytes - received))
                if not chunk:
                    break
                if payload_started is None:
                    payload_started = clock()
                received += len(chunk)
            finished = clock()
            transfer_seconds = (
                max(0.001, finished - payload_started) if payload_started is not None else None
            )
            goodput = (
                received * 8 / transfer_seconds / 1_000
                if transfer_seconds is not None and received >= 1024
                else None
            )
            return ProbeResult(
                True,
                dns_ms=(dns_done - started) * 1_000,
                connect_ms=(connected_at - dns_done) * 1_000,
                tls_ms=(tls_done - connected_at) * 1_000,
                ttfb_ms=(first_byte_at - tls_done) * 1_000,
                goodput_kbps=goodput,
                bytes_received=received,
                reason="validated",
            )
    except socket.gaierror:
        return ProbeResult(False, reason="dns_failed")
    except ssl.SSLError:
        return ProbeResult(False, reason="tls_failed")
    except TimeoutError:
        return ProbeResult(False, reason=f"{stage}_timeout")
    except OSError:
        return ProbeResult(False, reason=f"{stage}_failed")
    finally:
        if raw_socket is not None:
            raw_socket.close()


class LinuxRouteEventWatcher:
    """Wake the monitor immediately on Linux route/link/address changes."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if sys.platform != "linux" or not hasattr(socket, "AF_NETLINK"):
            return
        try:
            channel = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
            channel.bind((0, _ROUTE_EVENT_GROUPS))
            channel.settimeout(1.0)
        except OSError:
            return
        self._socket = channel
        self._thread = threading.Thread(
            target=self._run,
            name="helios-route-events",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        channel = self._socket
        if channel is None:
            return
        while not self._stop.is_set():
            try:
                if channel.recv(65535):
                    self._callback()
            except TimeoutError:
                continue
            except OSError:
                return

    def close(self) -> None:
        self._stop.set()
        channel = self._socket
        self._socket = None
        if channel is not None:
            try:
                channel.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.2)


class ConnectivityMonitor:
    """Conservative connectivity gate with EWMA quality and hysteresis."""

    def __init__(
        self,
        settings: NetworkSettings,
        *,
        inspector: LinuxNetworkInspector | None = None,
        probe: Callable[..., ProbeResult] = tls_http_probe,
        clock: Callable[[], float] = time.monotonic,
        route_watcher_factory: Callable[[Callable[[], None]], Any] | None = (
            LinuxRouteEventWatcher
        ),
        snapshot_observer: (
            Callable[[NetworkQualitySnapshot, NetworkQualitySnapshot], None] | None
        ) = None,
    ) -> None:
        self.settings = settings
        self.inspector = inspector or LinuxNetworkInspector()
        self._probe = probe
        self._clock = clock
        self._route_watcher_factory = route_watcher_factory
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._trigger = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._watcher: Any | None = None
        self._online_callback: Callable[[], None] | None = None
        self._snapshot_observer = snapshot_observer
        self._history: deque[bool] = deque(maxlen=settings.history_size)
        self._snapshot = NetworkQualitySnapshot()
        self._last_goodput_probe_at: float | None = None

    def set_online_callback(self, callback: Callable[[], None] | None) -> None:
        with self._lock:
            self._online_callback = callback

    def set_snapshot_observer(
        self,
        observer: Callable[[NetworkQualitySnapshot, NetworkQualitySnapshot], None] | None,
    ) -> None:
        with self._lock:
            self._snapshot_observer = observer

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._stop.is_set():
                return
            if self._route_watcher_factory is not None:
                self._watcher = self._route_watcher_factory(self.trigger)
                self._watcher.start()
            self._thread = threading.Thread(
                target=self._run,
                name="helios-connectivity",
                daemon=True,
            )
            self._thread.start()

    def trigger(self) -> None:
        self._trigger.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh_once()
            self._trigger.wait(self.settings.probe_interval_seconds)
            self._trigger.clear()

    def connectivity(self) -> Connectivity:
        link = self.inspector.inspect(
            require_wifi=self.settings.require_wifi,
            interface_allowlist=self.settings.interface_allowlist,
        )
        if not link.supported:
            return Connectivity.UNKNOWN
        if not link.online:
            self._publish_hard_offline(link)
            return Connectivity.OFFLINE

        with self._lock:
            snapshot = self._snapshot
        if snapshot.interface != link.interface:
            self.trigger()
            return Connectivity.OFFLINE
        if self._clock() - snapshot.observed_at > self.settings.result_max_age_seconds:
            self.trigger()
            self._publish(
                replace(
                    snapshot,
                    connectivity=Connectivity.OFFLINE,
                    reason="stale_probe",
                )
            )
            return Connectivity.OFFLINE
        return snapshot.connectivity

    def refresh_once(self) -> NetworkQualitySnapshot:
        if not self._refresh_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            link = self.inspector.inspect(
                require_wifi=self.settings.require_wifi,
                interface_allowlist=self.settings.interface_allowlist,
            )
            if not link.supported:
                return self._publish(
                    NetworkQualitySnapshot(
                        Connectivity.UNKNOWN,
                        reason=link.reason,
                        observed_at=self._clock(),
                    )
                )
            if not link.online:
                return self._publish_hard_offline(link)

            now = self._clock()
            measure_payload = (
                self._last_goodput_probe_at is None
                or now - self._last_goodput_probe_at >= self.settings.goodput_probe_interval_seconds
            )
            result = self._probe(
                self.settings.probe_url,
                timeout_seconds=self.settings.probe_timeout_seconds,
                maximum_bytes=self.settings.probe_bytes,
                measure_payload=measure_payload,
                clock=self._clock,
            )
            if result.success and measure_payload:
                self._last_goodput_probe_at = self._clock()
            return self._update_quality(link, result)
        finally:
            self._refresh_lock.release()

    def _update_quality(
        self,
        link: LinkState,
        result: ProbeResult,
    ) -> NetworkQualitySnapshot:
        with self._lock:
            previous = self._snapshot
            self._history.append(result.success)
            loss_ratio = 1.0 - sum(self._history) / len(self._history)
            if not result.success:
                snapshot = NetworkQualitySnapshot(
                    Connectivity.OFFLINE,
                    interface=link.interface,
                    interface_kind=link.kind,
                    reason=result.reason,
                    observed_at=self._clock(),
                    quality_score=0.0,
                    loss_ratio=loss_ratio,
                    wifi_signal_dbm=link.wifi_signal_dbm,
                )
            else:
                ttfb, jitter = self._smooth_delay(
                    previous.ttfb_ewma_ms,
                    previous.jitter_ewma_ms,
                    result.ttfb_ms,
                )
                goodput = self._ewma(
                    previous.goodput_ewma_kbps,
                    result.goodput_kbps,
                    0.25,
                )
                score = self._quality_score(
                    ttfb_ms=ttfb,
                    jitter_ms=jitter,
                    loss_ratio=loss_ratio,
                    goodput_kbps=goodput,
                    wifi_signal_dbm=link.wifi_signal_dbm,
                )
                was_online = previous.connectivity is Connectivity.ONLINE
                threshold = self.settings.minimum_quality_score + (
                    -self.settings.quality_hysteresis
                    if was_online
                    else self.settings.quality_hysteresis
                )
                state = Connectivity.ONLINE if score >= threshold else Connectivity.OFFLINE
                snapshot = NetworkQualitySnapshot(
                    state,
                    interface=link.interface,
                    interface_kind=link.kind,
                    reason=(
                        "quality_validated"
                        if state is Connectivity.ONLINE
                        else "quality_below_threshold"
                    ),
                    observed_at=self._clock(),
                    quality_score=score,
                    dns_ms=result.dns_ms,
                    connect_ms=result.connect_ms,
                    tls_ms=result.tls_ms,
                    ttfb_ewma_ms=ttfb,
                    jitter_ewma_ms=jitter,
                    goodput_ewma_kbps=goodput,
                    loss_ratio=loss_ratio,
                    wifi_signal_dbm=link.wifi_signal_dbm,
                )
        return self._publish(snapshot)

    def _quality_score(
        self,
        *,
        ttfb_ms: float | None,
        jitter_ms: float | None,
        loss_ratio: float,
        goodput_kbps: float | None,
        wifi_signal_dbm: float | None,
    ) -> float:
        latency_quality = self._inverse_square(
            ttfb_ms,
            self.settings.target_ttfb_ms,
            neutral=0.5,
        )
        jitter_quality = self._inverse_square(
            jitter_ms,
            self.settings.target_jitter_ms,
            neutral=0.75,
        )
        loss_quality = max(0.0, 1.0 - loss_ratio) ** 2
        goodput_quality = (
            min(1.0, max(0.0, goodput_kbps / self.settings.minimum_goodput_kbps))
            if goodput_kbps is not None
            else 0.5
        )
        signal_quality = (
            min(1.0, max(0.0, (wifi_signal_dbm + 90.0) / 40.0))
            if wifi_signal_dbm is not None
            else 0.75
        )
        return min(
            1.0,
            max(
                0.0,
                0.55 * latency_quality
                + 0.20 * loss_quality
                + 0.10 * jitter_quality
                + 0.10 * goodput_quality
                + 0.05 * signal_quality,
            ),
        )

    @staticmethod
    def _ewma(
        previous: float | None,
        current: float | None,
        alpha: float,
    ) -> float | None:
        if current is None or not math.isfinite(current):
            return previous
        if previous is None:
            return float(current)
        return alpha * current + (1.0 - alpha) * previous

    @staticmethod
    def _smooth_delay(
        previous_smoothed: float | None,
        previous_variation: float | None,
        sample: float | None,
    ) -> tuple[float | None, float | None]:
        """Apply the RFC 6298 SRTT/RTTVAR estimator to endpoint TTFB samples."""
        if sample is None or not math.isfinite(sample):
            return previous_smoothed, previous_variation
        sample = max(0.0, float(sample))
        if previous_smoothed is None:
            return sample, sample / 2.0
        variation = previous_variation
        if variation is None:
            variation = abs(previous_smoothed - sample) / 2.0
        variation = 0.75 * variation + 0.25 * abs(previous_smoothed - sample)
        smoothed = 0.875 * previous_smoothed + 0.125 * sample
        return smoothed, variation

    @staticmethod
    def _inverse_square(
        value: float | None,
        target: float,
        *,
        neutral: float,
    ) -> float:
        if value is None:
            return neutral
        ratio = max(0.0, value) / target
        return 1.0 / (1.0 + ratio * ratio)

    def _publish_hard_offline(self, link: LinkState) -> NetworkQualitySnapshot:
        return self._publish(
            NetworkQualitySnapshot(
                Connectivity.OFFLINE,
                interface=link.interface,
                interface_kind=link.kind,
                reason=link.reason,
                observed_at=self._clock(),
                quality_score=0.0,
                wifi_signal_dbm=link.wifi_signal_dbm,
            )
        )

    def _publish(self, snapshot: NetworkQualitySnapshot) -> NetworkQualitySnapshot:
        with self._lock:
            previous = self._snapshot
            self._snapshot = snapshot
        if (
            previous.connectivity is not snapshot.connectivity
            or previous.reason != snapshot.reason
            or previous.interface != snapshot.interface
        ):
            logger.info(
                "Network gate state=%s reason=%s interface=%s kind=%s "
                "quality=%s ttfb_ms=%s jitter_ms=%s goodput_kbps=%s loss=%s signal_dbm=%s",
                snapshot.connectivity.value,
                snapshot.reason,
                snapshot.interface,
                snapshot.interface_kind,
                self._rounded(snapshot.quality_score, 3),
                self._rounded(snapshot.ttfb_ewma_ms, 1),
                self._rounded(snapshot.jitter_ewma_ms, 1),
                self._rounded(snapshot.goodput_ewma_kbps, 1),
                self._rounded(snapshot.loss_ratio, 3),
                self._rounded(snapshot.wifi_signal_dbm, 1),
            )
        if (
            previous.connectivity is not Connectivity.ONLINE
            and snapshot.connectivity is Connectivity.ONLINE
        ):
            with self._lock:
                callback = self._online_callback
            if callback is not None:
                try:
                    callback()
                except Exception:
                    logger.warning("Network-online callback failed", exc_info=True)
        with self._lock:
            observer = self._snapshot_observer
        if observer is not None:
            try:
                observer(previous, snapshot)
            except Exception:
                logger.warning("Network snapshot observer failed")
        return snapshot

    @staticmethod
    def _rounded(value: float | None, digits: int) -> float | None:
        return None if value is None else round(value, digits)

    def snapshot(self) -> NetworkQualitySnapshot:
        with self._lock:
            return replace(self._snapshot)

    def close(self) -> None:
        self._stop.set()
        self._trigger.set()
        watcher = self._watcher
        self._watcher = None
        if watcher is not None:
            watcher.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.2)


__all__ = [
    "ConnectivityMonitor",
    "LinkState",
    "LinuxNetworkInspector",
    "LinuxRouteEventWatcher",
    "NetworkQualitySnapshot",
    "ProbeResult",
    "tls_http_probe",
]
