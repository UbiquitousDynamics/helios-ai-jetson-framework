from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import config
from api.connectivity import (
    ConnectivityMonitor,
    LinkState,
    LinuxNetworkInspector,
    ProbeResult,
    tls_http_probe,
)
from api.routing import Connectivity


def write_network_fixture(
    root: Path,
    *,
    interface: str = "wlan0",
    route: bool = True,
    carrier: str = "1",
    operstate: str = "up",
) -> tuple[Path, Path, Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    route_path = root / "route"
    route_path.write_text(
        "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        + (f"{interface} 00000000 0101A8C0 0003 0 0 100 00000000 0 0 0\n" if route else ""),
        encoding="ascii",
    )
    ipv6_route = root / "ipv6_route"
    ipv6_route.write_text("", encoding="ascii")
    ipv6_address = root / "if_inet6"
    ipv6_address.write_text("", encoding="ascii")
    wireless = root / "wireless"
    wireless.write_text(
        "Inter-| sta\n face | quality\n"
        f"{interface}: 0000   70.  -45.  -256        0      0      0      0      0        0\n",
        encoding="ascii",
    )
    sys_class = root / "sys-class-net"
    interface_path = sys_class / interface
    interface_path.mkdir(parents=True)
    if interface.startswith("wl"):
        (interface_path / "wireless").mkdir()
    (interface_path / "carrier").write_text(carrier, encoding="ascii")
    (interface_path / "operstate").write_text(operstate, encoding="ascii")
    return route_path, ipv6_route, ipv6_address, wireless, sys_class


def inspector_from_fixture(
    paths: tuple[Path, Path, Path, Path, Path],
    *,
    address: str | None = "192.0.2.10",
) -> LinuxNetworkInspector:
    route, ipv6_route, ipv6_address, wireless, sys_class = paths
    return LinuxNetworkInspector(
        route_path=route,
        ipv6_route_path=ipv6_route,
        ipv6_address_path=ipv6_address,
        wireless_path=wireless,
        sys_class_net=sys_class,
        ipv4_lookup=lambda _interface: address,
        platform="linux",
    )


def test_passive_gate_requires_default_route_carrier_and_usable_ip(tmp_path: Path) -> None:
    online = inspector_from_fixture(write_network_fixture(tmp_path / "online")).inspect()
    no_route = inspector_from_fixture(
        write_network_fixture(tmp_path / "no-route", route=False)
    ).inspect()
    no_carrier = inspector_from_fixture(
        write_network_fixture(tmp_path / "no-carrier", carrier="0")
    ).inspect()
    no_ip = inspector_from_fixture(
        write_network_fixture(tmp_path / "no-ip"),
        address="169.254.1.2",
    ).inspect()

    assert online.online
    assert online.interface == "wlan0"
    assert online.kind == "wifi"
    assert online.wifi_signal_dbm == -45
    assert no_route.reason == "no_default_route"
    assert no_carrier.reason == "no_carrier"
    assert no_ip.reason == "no_usable_ip"


def test_passive_gate_can_require_wifi_or_an_explicit_interface(tmp_path: Path) -> None:
    paths = write_network_fixture(tmp_path, interface="eth0")
    inspector = inspector_from_fixture(paths)

    assert inspector.inspect().online
    assert not inspector.inspect(require_wifi=True).online
    assert inspector.inspect(interface_allowlist=("wlan0",)).reason == "no_default_route"


@dataclass
class MutableInspector:
    state: LinkState

    def inspect(self, **_kwargs: object) -> LinkState:
        return self.state


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def test_quality_monitor_validates_good_path_and_fails_closed_when_stale() -> None:
    clock = FakeClock()
    inspector = MutableInspector(
        LinkState(
            True,
            True,
            interface="wlan0",
            kind="wifi",
            wifi_signal_dbm=-50,
            reason="default_route_ready",
        )
    )

    def good_probe(*_args: object, **_kwargs: object) -> ProbeResult:
        return ProbeResult(
            True,
            dns_ms=10,
            connect_ms=20,
            tls_ms=30,
            ttfb_ms=100,
            goodput_kbps=2_000,
            bytes_received=32_768,
            reason="validated",
        )

    settings = config.LLMNetworkSettings(enabled=True)
    monitor = ConnectivityMonitor(
        settings,
        inspector=inspector,  # type: ignore[arg-type]
        probe=good_probe,
        clock=clock,
        route_watcher_factory=None,
    )

    snapshot = monitor.refresh_once()
    assert snapshot.connectivity is Connectivity.ONLINE
    assert snapshot.quality_score is not None and snapshot.quality_score > 0.8
    assert monitor.connectivity() is Connectivity.ONLINE

    clock.value += settings.result_max_age_seconds + 0.1
    assert monitor.connectivity() is Connectivity.OFFLINE


def test_monitor_notifies_once_when_a_path_becomes_online() -> None:
    inspector = MutableInspector(
        LinkState(True, True, interface="wlan0", kind="wifi", reason="ready")
    )
    notifications: list[str] = []

    def good_probe(*_args: object, **_kwargs: object) -> ProbeResult:
        return ProbeResult(True, ttfb_ms=100, goodput_kbps=2_000)

    monitor = ConnectivityMonitor(
        config.LLMNetworkSettings(enabled=True),
        inspector=inspector,  # type: ignore[arg-type]
        probe=good_probe,
        route_watcher_factory=None,
    )
    monitor.set_online_callback(lambda: notifications.append("online"))

    monitor.refresh_once()
    monitor.refresh_once()

    assert notifications == ["online"]


def test_monitor_uses_sparse_payload_probes_but_keeps_fast_path_checks() -> None:
    clock = FakeClock()
    inspector = MutableInspector(
        LinkState(True, True, interface="wlan0", kind="wifi", reason="ready")
    )
    payload_flags: list[bool] = []

    def probe(*_args: object, **kwargs: object) -> ProbeResult:
        payload_flags.append(bool(kwargs["measure_payload"]))
        return ProbeResult(True, ttfb_ms=100, goodput_kbps=2_000)

    settings = config.LLMNetworkSettings(enabled=True)
    monitor = ConnectivityMonitor(
        settings,
        inspector=inspector,  # type: ignore[arg-type]
        probe=probe,
        clock=clock,
        route_watcher_factory=None,
    )

    monitor.refresh_once()
    clock.value += settings.probe_interval_seconds
    monitor.refresh_once()
    clock.value += settings.goodput_probe_interval_seconds
    monitor.refresh_once()

    assert payload_flags == [True, False, True]


def test_quality_monitor_rejects_bad_path_and_hard_link_loss_immediately() -> None:
    clock = FakeClock()
    inspector = MutableInspector(
        LinkState(True, True, interface="wlan0", kind="wifi", reason="ready")
    )

    def poor_probe(*_args: object, **_kwargs: object) -> ProbeResult:
        return ProbeResult(
            True,
            ttfb_ms=5_000,
            goodput_kbps=1,
            bytes_received=1_024,
            reason="validated",
        )

    monitor = ConnectivityMonitor(
        config.LLMNetworkSettings(enabled=True),
        inspector=inspector,  # type: ignore[arg-type]
        probe=poor_probe,
        clock=clock,
        route_watcher_factory=None,
    )

    assert monitor.refresh_once().connectivity is Connectivity.OFFLINE
    assert monitor.snapshot().reason == "quality_below_threshold"

    inspector.state = LinkState(True, False, reason="no_carrier")
    assert monitor.connectivity() is Connectivity.OFFLINE
    assert monitor.snapshot().reason == "no_carrier"


def test_failed_active_probe_marks_route_offline_without_waiting_for_llm() -> None:
    inspector = MutableInspector(
        LinkState(True, True, interface="wlan0", kind="wifi", reason="ready")
    )

    def failed_probe(*_args: object, **_kwargs: object) -> ProbeResult:
        return ProbeResult(False, reason="probe_unreachable")

    monitor = ConnectivityMonitor(
        config.LLMNetworkSettings(enabled=True),
        inspector=inspector,  # type: ignore[arg-type]
        probe=failed_probe,
        route_watcher_factory=None,
    )

    snapshot = monitor.refresh_once()
    assert snapshot.connectivity is Connectivity.OFFLINE
    assert snapshot.loss_ratio == 1
    assert snapshot.reason == "probe_unreachable"


def test_invalid_probe_url_fails_without_network_access() -> None:
    result = tls_http_probe(
        "http://example.invalid/",
        timeout_seconds=0.1,
        maximum_bytes=1_024,
        measure_payload=False,
    )

    assert not result.success
    assert result.reason == "invalid_probe_url"
