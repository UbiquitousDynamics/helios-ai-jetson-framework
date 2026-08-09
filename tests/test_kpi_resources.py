from __future__ import annotations

import math
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from observability.resources import (
    ResourceCollector,
    ResourceSampler,
    ResourceSnapshot,
    parse_tegrastats,
)


FIXED_TIME = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def test_parse_representative_tegrastats_without_identifying_data() -> None:
    snapshot = parse_tegrastats(
        "RAM 2048/7764MB (lfb 88x4MB) SWAP 256/4096MB (cached 3MB) "
        "CPU [12%@1190,off,24%@1190,0%@1190] EMC_FREQ 4%@1600 "
        "GR3D_FREQ 45%@[306] CPU@51.5C GPU@48C VDD_IN 5200mW/4900mW "
        "throttled=0x2",
        timestamp=FIXED_TIME,
    )

    assert snapshot.timestamp == FIXED_TIME
    assert snapshot.cpu_percent == pytest.approx(12.0)
    assert snapshot.cpu_frequency_mhz == pytest.approx(1190.0)
    assert snapshot.gpu_percent == pytest.approx(45.0)
    assert snapshot.gpu_frequency_mhz == pytest.approx(306.0)
    assert snapshot.memory_used_bytes == 2048 * 1024 * 1024
    assert snapshot.memory_total_bytes == 7764 * 1024 * 1024
    assert snapshot.swap_used_bytes == 256 * 1024 * 1024
    assert snapshot.swap_total_bytes == 4096 * 1024 * 1024
    assert snapshot.cpu_temperature_c == pytest.approx(51.5)
    assert snapshot.gpu_temperature_c == pytest.approx(48.0)
    assert snapshot.power_mw == pytest.approx(5200.0)
    assert snapshot.throttled is True
    assert snapshot.tegrastats_available
    assert set(snapshot.as_dict()) == {
        "timestamp",
        "cpu_percent",
        "gpu_percent",
        "memory_used_bytes",
        "memory_total_bytes",
        "swap_used_bytes",
        "swap_total_bytes",
        "cpu_temperature_c",
        "gpu_temperature_c",
        "power_mw",
        "cpu_frequency_mhz",
        "gpu_frequency_mhz",
        "disk_used_bytes",
        "disk_total_bytes",
        "throttled",
        "tegrastats_available",
    }


def test_parse_tegrastats_uses_last_line_and_ignores_malformed_values() -> None:
    snapshot = parse_tegrastats(
        "RAM 1/2MB GR3D_FREQ 4%\n"
        "RAM 9/2MB CPU [150%@999] GPU@not-a-number VDD_IN 3W/2W throttled=clear",
        timestamp=FIXED_TIME,
    )

    assert snapshot.memory_used_bytes is None
    assert snapshot.memory_total_bytes is None
    assert snapshot.cpu_percent is None
    assert snapshot.gpu_temperature_c is None
    assert snapshot.power_mw == pytest.approx(3000.0)
    assert snapshot.throttled is False
    assert snapshot.tegrastats_available
    assert not parse_tegrastats("", timestamp=FIXED_TIME).available


def test_collector_combines_proc_tegrastats_thermal_and_disk(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "stat").write_text("cpu  10 0 10 80 0 0 0 0\n", encoding="ascii")
    (proc_root / "meminfo").write_text(
        "MemTotal:       1000 kB\n"
        "MemAvailable:    400 kB\n"
        "SwapTotal:       200 kB\n"
        "SwapFree:         50 kB\n",
        encoding="ascii",
    )
    thermal_root = tmp_path / "thermal"
    cpu_zone = thermal_root / "thermal_zone0"
    gpu_zone = thermal_root / "thermal_zone1"
    cpu_zone.mkdir(parents=True)
    gpu_zone.mkdir()
    (cpu_zone / "type").write_text("CPU-therm\n", encoding="ascii")
    (cpu_zone / "temp").write_text("55000\n", encoding="ascii")
    (gpu_zone / "type").write_text("GPU-therm\n", encoding="ascii")
    (gpu_zone / "temp").write_text("50000\n", encoding="ascii")

    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [],
            0,
            stdout="CPU [25%@1000] GR3D_FREQ 40%@500 VDD_IN 4W/4W",
            stderr="",
        )

    collector = ResourceCollector(
        disk_path=tmp_path,
        proc_root=proc_root,
        thermal_root=thermal_root,
        system="Linux",
        tegrastats_command=("tegrastats", "--count", "1"),
        runner=runner,
        clock=lambda: FIXED_TIME,
    )
    first = collector.collect()
    (proc_root / "stat").write_text("cpu  20 0 20 160 0 0 0 0\n", encoding="ascii")
    second = collector.collect()

    assert first.cpu_percent == pytest.approx(25.0)
    assert second.cpu_percent == pytest.approx(20.0)
    assert second.gpu_percent == pytest.approx(40.0)
    assert second.memory_total_bytes == 1000 * 1024
    assert second.memory_used_bytes == 600 * 1024
    assert second.swap_total_bytes == 200 * 1024
    assert second.swap_used_bytes == 150 * 1024
    assert second.cpu_temperature_c == pytest.approx(55.0)
    assert second.gpu_temperature_c == pytest.approx(50.0)
    assert second.power_mw == pytest.approx(4000.0)
    assert second.disk_used_bytes is not None
    assert second.disk_total_bytes is not None


def test_collector_supports_tegrastats_without_count_option(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "--count" in command:
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="unsupported")
        raise subprocess.TimeoutExpired(
            command,
            kwargs["timeout"],
            output=(
                b"RAM 100/1000MB CPU [10%@1020] GR3D_FREQ 35%@510 GPU@48C POM_5V_IN 4200/4000\n"
            ),
        )

    snapshot = ResourceCollector(
        disk_path=tmp_path,
        proc_root=tmp_path / "missing-proc",
        thermal_root=tmp_path / "missing-thermal",
        system="Linux",
        tegrastats_command=("tegrastats", "--interval", "100", "--count", "1"),
        runner=runner,
        clock=lambda: FIXED_TIME,
    ).collect()

    assert len(commands) == 2
    assert "--count" in commands[0]
    assert "--count" not in commands[1]
    assert snapshot.gpu_percent == pytest.approx(35.0)
    assert snapshot.gpu_frequency_mhz == pytest.approx(510.0)
    assert snapshot.power_mw == pytest.approx(4200.0)


def test_collector_retries_without_count_when_initial_command_times_out_without_data(
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "--count" in command:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"")
        raise subprocess.TimeoutExpired(
            command,
            kwargs["timeout"],
            output=b"GR3D_FREQ 22%@[307] POM_5V_IN 3100/3000\n",
        )

    snapshot = ResourceCollector(
        disk_path=tmp_path,
        proc_root=tmp_path / "missing-proc",
        thermal_root=tmp_path / "missing-thermal",
        system="Linux",
        tegrastats_command=("tegrastats", "--interval", "100", "--count", "1"),
        runner=runner,
        clock=lambda: FIXED_TIME,
    ).collect()

    assert len(commands) == 2
    assert "--count" in commands[0]
    assert "--count" not in commands[1]
    assert snapshot.gpu_percent == pytest.approx(22.0)
    assert snapshot.gpu_frequency_mhz == pytest.approx(307.0)
    assert snapshot.power_mw == pytest.approx(3100.0)


def test_collector_reads_jetson_nano_gpu_clock_and_input_power_from_sysfs(
    tmp_path: Path,
) -> None:
    sys_root = tmp_path / "sys"
    gpu_root = sys_root / "devices" / "57000000.gpu" / "devfreq" / "57000000.gpu"
    gpu_root.mkdir(parents=True)
    (gpu_root / "cur_freq").write_text("921600000\n", encoding="ascii")
    power_root = sys_root / "bus" / "i2c" / "drivers" / "ina3221x" / "6-0040" / "iio-device0"
    power_root.mkdir(parents=True)
    (power_root / "rail_name_0").write_text("POM_5V_IN\n", encoding="ascii")
    (power_root / "in_power0_input").write_text("2875\n", encoding="ascii")

    snapshot = ResourceCollector(
        disk_path=tmp_path,
        proc_root=tmp_path / "missing-proc",
        thermal_root=tmp_path / "missing-thermal",
        sys_root=sys_root,
        system="Linux",
        tegrastats_command=("tegrastats",),
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="GR3D_FREQ 0% GPU@46.5C",
            stderr="",
        ),
        clock=lambda: FIXED_TIME,
    ).collect()

    assert snapshot.gpu_percent == pytest.approx(0.0)
    assert snapshot.gpu_frequency_mhz == pytest.approx(921.6)
    assert snapshot.power_mw == pytest.approx(2875.0)


def test_collector_reads_input_power_from_i2c_device_hwmon(tmp_path: Path) -> None:
    sys_root = tmp_path / "sys"
    hwmon_root = sys_root / "bus" / "i2c" / "devices" / "1-0040" / "hwmon" / "hwmon2"
    hwmon_root.mkdir(parents=True)
    (hwmon_root / "in3_label").write_text("VIN_SYS_5V0\n", encoding="ascii")
    (hwmon_root / "power3_input").write_text("4125000\n", encoding="ascii")

    snapshot = ResourceCollector(
        disk_path=tmp_path,
        proc_root=tmp_path / "missing-proc",
        thermal_root=tmp_path / "missing-thermal",
        sys_root=sys_root,
        system="Linux",
        tegrastats_command=(),
        clock=lambda: FIXED_TIME,
    ).collect()

    assert snapshot.power_mw == pytest.approx(4125.0)


def test_windows_style_fallback_skips_tegrastats_and_returns_disk_only(tmp_path: Path) -> None:
    called = False

    def finder(_name: str) -> str | None:
        nonlocal called
        called = True
        return "tegrastats"

    def runner(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("tegrastats must not run on Windows")

    snapshot = ResourceCollector(
        disk_path=tmp_path,
        system="Windows",
        command_finder=finder,
        runner=runner,
        clock=lambda: FIXED_TIME,
    ).collect()

    assert not called
    assert snapshot.timestamp == FIXED_TIME
    assert snapshot.cpu_percent is None
    assert snapshot.gpu_percent is None
    assert snapshot.memory_total_bytes is None
    assert not snapshot.tegrastats_available
    assert snapshot.disk_used_bytes is not None
    assert snapshot.disk_total_bytes is not None


def test_unavailable_or_failing_tegrastats_never_raises(tmp_path: Path) -> None:
    absent = ResourceCollector(
        disk_path=tmp_path,
        proc_root=tmp_path / "missing-proc",
        thermal_root=tmp_path / "missing-thermal",
        system="Linux",
        command_finder=lambda _name: None,
        clock=lambda: FIXED_TIME,
    ).collect()
    failing = ResourceCollector(
        disk_path=tmp_path / "missing-disk",
        proc_root=tmp_path / "missing-proc",
        thermal_root=tmp_path / "missing-thermal",
        system="Linux",
        tegrastats_command=("tegrastats",),
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unavailable")),
        clock=lambda: FIXED_TIME,
    ).collect()

    assert not absent.tegrastats_available
    assert absent.disk_total_bytes is not None
    assert not failing.available
    assert not failing.tegrastats_available


def test_sampler_invokes_callback_and_stops_cleanly() -> None:
    received: list[ResourceSnapshot] = []
    sampled_twice = threading.Event()

    def collect() -> ResourceSnapshot:
        return ResourceSnapshot(timestamp=FIXED_TIME, cpu_percent=1.0)

    def callback(snapshot: ResourceSnapshot) -> None:
        received.append(snapshot)
        if len(received) >= 2:
            sampled_twice.set()

    sampler = ResourceSampler(collect, callback, interval_seconds=0.01)
    assert sampler.start() is sampler
    assert sampler.start() is sampler
    assert sampled_twice.wait(timeout=1.0)
    assert sampler.running
    assert sampler.stop(timeout=1.0)
    assert not sampler.running
    count_after_stop = len(received)
    time.sleep(0.03)
    assert len(received) == count_after_stop
    assert sampler.close()


def test_sampler_survives_collector_and_callback_failures() -> None:
    callback_calls = 0
    completed = threading.Event()

    def broken_collect() -> ResourceSnapshot:
        raise OSError("sample unavailable")

    def callback(snapshot: ResourceSnapshot) -> None:
        nonlocal callback_calls
        callback_calls += 1
        assert not snapshot.available
        if callback_calls == 1:
            raise RuntimeError("temporary sink failure")
        completed.set()

    sampler = ResourceSampler(broken_collect, callback, interval_seconds=0.01).start()
    assert completed.wait(timeout=1.0)
    assert sampler.stop(timeout=1.0)
    assert callback_calls >= 2


@pytest.mark.parametrize("interval", [True, 0, -1, math.nan, math.inf])
def test_sampler_validates_interval(interval: object) -> None:
    with pytest.raises(ValueError, match="interval"):
        ResourceSampler(
            lambda: ResourceSnapshot(timestamp=FIXED_TIME),
            lambda _snapshot: None,
            interval_seconds=interval,  # type: ignore[arg-type]
        )


def test_resource_types_and_collector_collaborators_are_validated() -> None:
    with pytest.raises(ValueError, match="cpu_percent"):
        ResourceSnapshot(timestamp=FIXED_TIME, cpu_percent=101.0)
    with pytest.raises(ValueError, match="timezone-aware"):
        ResourceSnapshot(timestamp=datetime(2026, 8, 3))
    with pytest.raises(TypeError, match="sequence"):
        ResourceCollector(tegrastats_command="tegrastats")
    with pytest.raises(ValueError, match="timeout"):
        ResourceCollector(timeout_seconds=0)
    with pytest.raises(TypeError, match="collector"):
        ResourceSampler(object(), lambda _snapshot: None, interval_seconds=1.0)
    with pytest.raises(TypeError, match="callback"):
        ResourceSampler(
            lambda: ResourceSnapshot(timestamp=FIXED_TIME),
            object(),  # type: ignore[arg-type]
            interval_seconds=1.0,
        )
