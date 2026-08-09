"""Best-effort, content-free host and NVIDIA Jetson resource sampling."""

from __future__ import annotations

import logging
import math
import numbers
import platform
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MEBIBYTE = 1024 * 1024
_MAX_BYTES = (1 << 63) - 1
_NUMBER = r"(?:\d+(?:\.\d+)?)"
_RAM_PATTERN = re.compile(
    rf"\bRAM\s+({_NUMBER})/({_NUMBER})([KMG]?B)\b",
    re.IGNORECASE,
)
_SWAP_PATTERN = re.compile(
    rf"\bSWAP\s+({_NUMBER})/({_NUMBER})([KMG]?B)\b",
    re.IGNORECASE,
)
_CPU_BLOCK_PATTERN = re.compile(r"\bCPU\s*\[([^]]*)]", re.IGNORECASE)
_CPU_ENTRY_PATTERN = re.compile(
    rf"({_NUMBER})%\s*@\s*({_NUMBER})",
    re.IGNORECASE,
)
_GPU_PATTERN = re.compile(
    rf"\bGR3D(?:_FREQ)?\s+({_NUMBER})%"
    rf"(?:\s*@\s*\[?\s*({_NUMBER}))?",
    re.IGNORECASE,
)
_CPU_TEMPERATURE_PATTERN = re.compile(rf"\bCPU(?:-therm)?@({_NUMBER})C\b", re.IGNORECASE)
_GPU_TEMPERATURE_PATTERN = re.compile(rf"\bGPU(?:-therm)?@({_NUMBER})C\b", re.IGNORECASE)
_POWER_PATTERN = re.compile(
    rf"\b(VDD_IN|POM_5V_IN)\s+({_NUMBER})(mW|W)?(?:/|\b)",
    re.IGNORECASE,
)
_THROTTLE_PATTERN = re.compile(
    r"\bthrottl(?:e|ed|ing)(?:_state)?\s*[:=]\s*"
    r"(0x[0-9a-f]+|[a-z]+|\d+)",
    re.IGNORECASE,
)
_TRUE_STATES = {"1", "active", "on", "true", "yes"}
_FALSE_STATES = {"0", "clear", "false", "inactive", "none", "off", "no"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_timestamp(value: object) -> datetime:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return _utc_now()


def _finite_number(
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    converted = float(value)
    if not math.isfinite(converted):
        return None
    if minimum is not None and converted < minimum:
        return None
    if maximum is not None and converted > maximum:
        return None
    return converted


def _nonnegative_bytes(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_BYTES:
        return None
    return value


def _measurement(value: str, *, minimum: float, maximum: float) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return _finite_number(parsed, minimum=minimum, maximum=maximum)


def _bytes_from_measurement(value: str, unit: str) -> int | None:
    amount = _measurement(value, minimum=0.0, maximum=float(_MAX_BYTES))
    if amount is None:
        return None
    multipliers = {
        "KB": 1024,
        "MB": _MEBIBYTE,
        "GB": 1024 * _MEBIBYTE,
    }
    multiplier = multipliers.get(unit.upper())
    if multiplier is None:
        return None
    result = int(amount * multiplier)
    return result if result <= _MAX_BYTES else None


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """A closed snapshot containing only sanitized numeric and state fields."""

    timestamp: datetime
    cpu_percent: float | None = None
    gpu_percent: float | None = None
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None
    swap_used_bytes: int | None = None
    swap_total_bytes: int | None = None
    cpu_temperature_c: float | None = None
    gpu_temperature_c: float | None = None
    power_mw: float | None = None
    cpu_frequency_mhz: float | None = None
    gpu_frequency_mhz: float | None = None
    disk_used_bytes: int | None = None
    disk_total_bytes: int | None = None
    throttled: bool | None = None
    tegrastats_available: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp, datetime):
            raise TypeError("resource snapshot timestamp must be a datetime")
        if self.timestamp.tzinfo is None:
            raise ValueError("resource snapshot timestamp must be timezone-aware")
        for name in ("cpu_percent", "gpu_percent"):
            if _finite_number(getattr(self, name), minimum=0.0, maximum=100.0) is None and (
                getattr(self, name) is not None
            ):
                raise ValueError(f"{name} must be finite and between zero and 100")
        for name in ("cpu_temperature_c", "gpu_temperature_c"):
            if _finite_number(getattr(self, name), minimum=-273.15, maximum=1_000.0) is None and (
                getattr(self, name) is not None
            ):
                raise ValueError(f"{name} must be a finite temperature")
        for name in ("power_mw", "cpu_frequency_mhz", "gpu_frequency_mhz"):
            if _finite_number(getattr(self, name), minimum=0.0) is None and (
                getattr(self, name) is not None
            ):
                raise ValueError(f"{name} must be finite and non-negative")
        for name in (
            "memory_used_bytes",
            "memory_total_bytes",
            "swap_used_bytes",
            "swap_total_bytes",
            "disk_used_bytes",
            "disk_total_bytes",
        ):
            if _nonnegative_bytes(getattr(self, name)) is None and getattr(self, name) is not None:
                raise ValueError(f"{name} must be a non-negative integer")
        for used_name, total_name in (
            ("memory_used_bytes", "memory_total_bytes"),
            ("swap_used_bytes", "swap_total_bytes"),
            ("disk_used_bytes", "disk_total_bytes"),
        ):
            used = getattr(self, used_name)
            total = getattr(self, total_name)
            if used is not None and total is not None and used > total:
                raise ValueError(f"{used_name} cannot exceed {total_name}")
        if self.throttled is not None and not isinstance(self.throttled, bool):
            raise TypeError("throttled must be a boolean or None")
        if not isinstance(self.tegrastats_available, bool):
            raise TypeError("tegrastats_available must be a boolean")

    @property
    def available(self) -> bool:
        """Return whether the snapshot contains at least one resource value."""

        return any(
            getattr(self, name) is not None
            for name in (
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
            )
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a content-free JSON-compatible representation."""

        payload = asdict(self)
        payload["timestamp"] = (
            self.timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        return payload


def _parse_memory(line: str, pattern: re.Pattern[str]) -> tuple[int | None, int | None]:
    match = pattern.search(line)
    if match is None:
        return None, None
    used = _bytes_from_measurement(match.group(1), match.group(3))
    total = _bytes_from_measurement(match.group(2), match.group(3))
    if used is None or total is None or used > total:
        return None, None
    return used, total


def _parse_throttled(line: str) -> bool | None:
    match = _THROTTLE_PATTERN.search(line)
    if match is None:
        return None
    value = match.group(1).lower()
    if value.startswith("0x"):
        try:
            return int(value, 16) != 0
        except ValueError:
            return None
    if value in _TRUE_STATES:
        return True
    if value in _FALSE_STATES:
        return False
    if value.isdecimal():
        return int(value) != 0
    return None


def parse_tegrastats(
    output: str,
    *,
    timestamp: datetime | None = None,
) -> ResourceSnapshot:
    """Parse one or more ``tegrastats`` lines without retaining device identifiers.

    The last non-empty line is used because command wrappers may emit more than
    one sample. Unknown or malformed fields are ignored rather than guessed.
    """

    if not isinstance(output, str):
        raise TypeError("tegrastats output must be a string")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    line = lines[-1] if lines else ""
    observed_at = _safe_timestamp(timestamp)

    memory_used, memory_total = _parse_memory(line, _RAM_PATTERN)
    swap_used, swap_total = _parse_memory(line, _SWAP_PATTERN)

    cpu_percent = None
    cpu_frequency_mhz = None
    cpu_block = _CPU_BLOCK_PATTERN.search(line)
    if cpu_block is not None:
        entries = [
            (
                _measurement(match.group(1), minimum=0.0, maximum=100.0),
                _measurement(match.group(2), minimum=0.0, maximum=1_000_000.0),
            )
            for match in _CPU_ENTRY_PATTERN.finditer(cpu_block.group(1))
        ]
        percentages = [percent for percent, _frequency in entries if percent is not None]
        frequencies = [frequency for _percent, frequency in entries if frequency is not None]
        if percentages:
            cpu_percent = sum(percentages) / len(percentages)
        if frequencies:
            cpu_frequency_mhz = sum(frequencies) / len(frequencies)

    gpu_percent = None
    gpu_frequency_mhz = None
    gpu_match = _GPU_PATTERN.search(line)
    if gpu_match is not None:
        gpu_percent = _measurement(gpu_match.group(1), minimum=0.0, maximum=100.0)
        if gpu_match.group(2) is not None:
            gpu_frequency_mhz = _measurement(
                gpu_match.group(2),
                minimum=0.0,
                maximum=1_000_000.0,
            )

    cpu_temperature_c = None
    cpu_temperature_match = _CPU_TEMPERATURE_PATTERN.search(line)
    if cpu_temperature_match is not None:
        cpu_temperature_c = _measurement(
            cpu_temperature_match.group(1),
            minimum=-273.15,
            maximum=1_000.0,
        )

    gpu_temperature_c = None
    gpu_temperature_match = _GPU_TEMPERATURE_PATTERN.search(line)
    if gpu_temperature_match is not None:
        gpu_temperature_c = _measurement(
            gpu_temperature_match.group(1),
            minimum=-273.15,
            maximum=1_000.0,
        )

    power_mw = None
    power_match = _POWER_PATTERN.search(line)
    if power_match is not None:
        power_mw = _measurement(power_match.group(2), minimum=0.0, maximum=1_000_000_000.0)
        if power_mw is not None and (power_match.group(3) or "mW").lower() == "w":
            power_mw *= 1_000.0

    parsed_values = (
        cpu_percent,
        gpu_percent,
        memory_used,
        memory_total,
        swap_used,
        swap_total,
        cpu_temperature_c,
        gpu_temperature_c,
        power_mw,
        cpu_frequency_mhz,
        gpu_frequency_mhz,
        _parse_throttled(line),
    )
    return ResourceSnapshot(
        timestamp=observed_at,
        cpu_percent=cpu_percent,
        gpu_percent=gpu_percent,
        memory_used_bytes=memory_used,
        memory_total_bytes=memory_total,
        swap_used_bytes=swap_used,
        swap_total_bytes=swap_total,
        cpu_temperature_c=cpu_temperature_c,
        gpu_temperature_c=gpu_temperature_c,
        power_mw=power_mw,
        cpu_frequency_mhz=cpu_frequency_mhz,
        gpu_frequency_mhz=gpu_frequency_mhz,
        throttled=parsed_values[-1],
        tegrastats_available=any(value is not None for value in parsed_values),
    )


class ResourceCollector:
    """Collect resource values without requiring optional packages or privileges."""

    def __init__(
        self,
        *,
        disk_path: str | Path = ".",
        proc_root: str | Path = "/proc",
        thermal_root: str | Path = "/sys/class/thermal",
        sys_root: str | Path = "/sys",
        system: str | None = None,
        tegrastats_command: Sequence[str] | None = None,
        command_finder: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., Any] = subprocess.run,
        clock: Callable[[], datetime] = _utc_now,
        timeout_seconds: float = 2.0,
    ) -> None:
        timeout = _finite_number(timeout_seconds, minimum=0.001)
        if timeout is None:
            raise ValueError("resource command timeout must be finite and positive")
        if not callable(command_finder) or not callable(runner) or not callable(clock):
            raise TypeError("resource collector collaborators must be callable")
        if tegrastats_command is not None:
            if isinstance(tegrastats_command, (str, bytes)):
                raise TypeError("tegrastats_command must be a sequence of arguments")
            if any(not isinstance(part, str) or not part for part in tegrastats_command):
                raise ValueError("tegrastats command arguments must be non-empty strings")

        self.disk_path = Path(disk_path)
        self.proc_root = Path(proc_root)
        self.thermal_root = Path(thermal_root)
        self.sys_root = Path(sys_root)
        self.system = platform.system() if system is None else str(system)
        self._configured_command = None if tegrastats_command is None else tuple(tegrastats_command)
        self._command_finder = command_finder
        self._runner = runner
        self._clock = clock
        self._timeout_seconds = timeout
        self._resolved_command: tuple[str, ...] | None = None
        self._command_resolved = False
        self._previous_cpu: tuple[int, int] | None = None
        self._lock = threading.Lock()

    def _command(self) -> tuple[str, ...] | None:
        with self._lock:
            if self._command_resolved:
                return self._resolved_command
            self._command_resolved = True
            if self._configured_command is not None:
                self._resolved_command = self._configured_command or None
                return self._resolved_command
            if self.system.lower() != "linux":
                return None
            try:
                executable = self._command_finder("tegrastats")
            except Exception:
                return None
            if executable:
                self._resolved_command = (
                    executable,
                    "--interval",
                    "100",
                    "--count",
                    "1",
                )
            return self._resolved_command

    def _collect_tegrastats(self, observed_at: datetime) -> ResourceSnapshot:
        command = self._command()
        if command is None:
            return ResourceSnapshot(timestamp=observed_at)

        def output_text(value: object) -> str:
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return value if isinstance(value, str) else ""

        def parsed(value: object) -> ResourceSnapshot:
            return parse_tegrastats(output_text(value), timestamp=observed_at)

        def parsed_if_available(value: object) -> ResourceSnapshot | None:
            snapshot = parsed(value)
            return snapshot if snapshot.tegrastats_available else None

        def collect_legacy(command_with_count: Sequence[str]) -> ResourceSnapshot:
            legacy_command = list(command_with_count)
            try:
                count_index = legacy_command.index("--count")
            except ValueError:
                return ResourceSnapshot(timestamp=observed_at)
            del legacy_command[count_index : count_index + 2]
            try:
                legacy = self._runner(
                    legacy_command,
                    capture_output=True,
                    text=True,
                    timeout=min(self._timeout_seconds, 0.5),
                    check=False,
                )
                return parsed(getattr(legacy, "stdout", ""))
            except subprocess.TimeoutExpired as error:
                return parsed(error.stdout)

        try:
            try:
                completed = self._runner(
                    list(command),
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                # Some releases accept the unknown option but keep streaming.
                # Preserve a complete sample if one was emitted; otherwise retry
                # without ``--count`` below.
                snapshot = parsed_if_available(error.stdout)
                return snapshot if snapshot is not None else collect_legacy(command)
            if getattr(completed, "returncode", 1) == 0:
                snapshot = parsed_if_available(getattr(completed, "stdout", ""))
                if snapshot is not None:
                    return snapshot

            # JetPack releases that ship an older tegrastats do not implement
            # ``--count``. Run the streaming form briefly and retain the sample
            # captured before subprocess.run terminates it at the deadline.
            return collect_legacy(command)
        except Exception:
            return ResourceSnapshot(timestamp=observed_at)

    def _read_proc_cpu(self) -> float | None:
        if self.system.lower() != "linux":
            return None
        try:
            first_line = (self.proc_root / "stat").read_text(encoding="ascii").splitlines()[0]
            fields = first_line.split()
            if not fields or fields[0] != "cpu" or len(fields) < 5:
                return None
            values = [int(value) for value in fields[1:9]]
            if any(value < 0 for value in values):
                return None
            total = sum(values)
            idle = values[3] + (values[4] if len(values) > 4 else 0)
        except (OSError, UnicodeError, ValueError, IndexError):
            return None

        with self._lock:
            previous = self._previous_cpu
            self._previous_cpu = (total, idle)
        if previous is None:
            return None
        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
            return None
        return 100.0 * (total_delta - idle_delta) / total_delta

    @staticmethod
    def _read_measurement_file(path: Path) -> float | None:
        try:
            value = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return None
        return _measurement(value, minimum=0.0, maximum=1_000_000_000_000.0)

    def _read_gpu_frequency(self) -> float | None:
        if self.system.lower() != "linux":
            return None
        patterns = (
            "class/devfreq/*gpu*/cur_freq",
            "devices/*.gpu/devfreq/*/cur_freq",
            "devices/platform/*.gpu/devfreq/*/cur_freq",
            "devices/platform/*/*gpu*/devfreq/*/cur_freq",
        )
        for pattern in patterns:
            try:
                paths = sorted(self.sys_root.glob(pattern))
            except OSError:
                continue
            for path in paths:
                value = self._read_measurement_file(path)
                if value is None:
                    continue
                # Linux devfreq exposes ``cur_freq`` in hertz.
                frequency_mhz = value / 1_000_000.0
                return _finite_number(frequency_mhz, minimum=0.0, maximum=1_000_000.0)
        return None

    def _read_input_power(self) -> float | None:
        if self.system.lower() != "linux":
            return None
        try:
            label_paths = sorted(
                path
                for pattern in (
                    "bus/i2c/drivers/ina3221*/**/rail_name_*",
                    "bus/i2c/drivers/ina3221*/**/in*_label",
                    "bus/i2c/devices/*/iio:device*/rail_name_*",
                    "bus/i2c/devices/*/iio_device/rail_name_*",
                    "bus/i2c/devices/*/hwmon/hwmon*/in*_label",
                    "class/hwmon/hwmon*/in*_label",
                )
                for path in self.sys_root.glob(pattern)
            )
        except OSError:
            return None
        for label_path in label_paths:
            try:
                label = label_path.read_text(encoding="ascii").strip().upper()
            except (OSError, UnicodeError):
                continue
            if label not in {"VDD_IN", "POM_5V_IN", "VIN_SYS_5V0", "SYS5V"}:
                continue
            channel_match = re.fullmatch(r"rail_name_(\d+)", label_path.name)
            legacy_channel = channel_match is not None
            if channel_match is None:
                channel_match = re.fullmatch(r"in(\d+)_label", label_path.name)
            if channel_match is None:
                continue
            channel = channel_match.group(1)
            if legacy_channel:
                direct_power = self._read_measurement_file(
                    label_path.parent / f"in_power{channel}_input"
                )
                if direct_power is not None:
                    # The legacy INA3221 IIO driver reports milliwatts.
                    return direct_power
            else:
                direct_power_uw = self._read_measurement_file(
                    label_path.parent / f"power{channel}_input"
                )
                if direct_power_uw is not None:
                    # Linux hwmon power inputs are expressed in microwatts.
                    return direct_power_uw / 1_000.0
            voltage_mv = self._read_measurement_file(label_path.parent / f"in{channel}_input")
            current_ma = self._read_measurement_file(label_path.parent / f"curr{channel}_input")
            if voltage_mv is not None and current_ma is not None:
                return voltage_mv * current_ma / 1_000.0
        return None

    def _read_proc_memory(self) -> Mapping[str, int | None]:
        unavailable = {
            "memory_used_bytes": None,
            "memory_total_bytes": None,
            "swap_used_bytes": None,
            "swap_total_bytes": None,
        }
        if self.system.lower() != "linux":
            return unavailable
        values: dict[str, int] = {}
        try:
            for line in (self.proc_root / "meminfo").read_text(encoding="ascii").splitlines():
                name, separator, remainder = line.partition(":")
                if not separator:
                    continue
                fields = remainder.split()
                if not fields:
                    continue
                amount = int(fields[0])
                if amount < 0:
                    continue
                multiplier = 1024 if len(fields) < 2 or fields[1].lower() == "kb" else 1
                converted = amount * multiplier
                if converted <= _MAX_BYTES:
                    values[name] = converted
        except (OSError, UnicodeError, ValueError):
            return unavailable

        memory_total = _nonnegative_bytes(values.get("MemTotal"))
        memory_available = _nonnegative_bytes(values.get("MemAvailable"))
        swap_total = _nonnegative_bytes(values.get("SwapTotal"))
        swap_free = _nonnegative_bytes(values.get("SwapFree"))
        memory_used = (
            memory_total - memory_available
            if memory_total is not None
            and memory_available is not None
            and memory_available <= memory_total
            else None
        )
        swap_used = (
            swap_total - swap_free
            if swap_total is not None and swap_free is not None and swap_free <= swap_total
            else None
        )
        return {
            "memory_used_bytes": memory_used,
            "memory_total_bytes": memory_total,
            "swap_used_bytes": swap_used,
            "swap_total_bytes": swap_total,
        }

    def _read_temperatures(self) -> tuple[float | None, float | None]:
        if self.system.lower() != "linux":
            return None, None
        cpu_values: list[float] = []
        gpu_values: list[float] = []
        try:
            zones = tuple(self.thermal_root.glob("thermal_zone*"))
        except OSError:
            return None, None
        for zone in zones:
            try:
                label = (zone / "type").read_text(encoding="ascii").strip().lower()
                raw_value = (zone / "temp").read_text(encoding="ascii").strip()
                value = float(raw_value)
                if abs(value) > 1_000.0:
                    value /= 1_000.0
                value = _finite_number(value, minimum=-273.15, maximum=1_000.0)
            except (OSError, UnicodeError, ValueError, OverflowError):
                continue
            if value is None:
                continue
            if "gpu" in label:
                gpu_values.append(value)
            elif "cpu" in label:
                cpu_values.append(value)
        return (
            max(cpu_values) if cpu_values else None,
            max(gpu_values) if gpu_values else None,
        )

    def _read_disk(self) -> tuple[int | None, int | None]:
        try:
            usage = shutil.disk_usage(self.disk_path)
        except (OSError, ValueError):
            return None, None
        used = _nonnegative_bytes(usage.used)
        total = _nonnegative_bytes(usage.total)
        if used is None or total is None or used > total:
            return None, None
        return used, total

    def collect(self) -> ResourceSnapshot:
        """Collect one snapshot, representing failed sources with ``None`` fields."""

        try:
            observed_at = _safe_timestamp(self._clock())
        except Exception:
            observed_at = _utc_now()

        try:
            tegra = self._collect_tegrastats(observed_at)
        except Exception:
            tegra = ResourceSnapshot(timestamp=observed_at)
        try:
            proc_cpu = self._read_proc_cpu()
        except Exception:
            proc_cpu = None
        try:
            memory = self._read_proc_memory()
        except Exception:
            memory = {}
        try:
            cpu_temperature, gpu_temperature = self._read_temperatures()
        except Exception:
            cpu_temperature, gpu_temperature = None, None
        try:
            disk_used, disk_total = self._read_disk()
        except Exception:
            disk_used, disk_total = None, None
        try:
            gpu_frequency = self._read_gpu_frequency()
        except Exception:
            gpu_frequency = None
        try:
            input_power = self._read_input_power()
        except Exception:
            input_power = None

        return replace(
            tegra,
            cpu_percent=proc_cpu if proc_cpu is not None else tegra.cpu_percent,
            memory_used_bytes=(
                memory.get("memory_used_bytes")
                if memory.get("memory_used_bytes") is not None
                else tegra.memory_used_bytes
            ),
            memory_total_bytes=(
                memory.get("memory_total_bytes")
                if memory.get("memory_total_bytes") is not None
                else tegra.memory_total_bytes
            ),
            swap_used_bytes=(
                memory.get("swap_used_bytes")
                if memory.get("swap_used_bytes") is not None
                else tegra.swap_used_bytes
            ),
            swap_total_bytes=(
                memory.get("swap_total_bytes")
                if memory.get("swap_total_bytes") is not None
                else tegra.swap_total_bytes
            ),
            cpu_temperature_c=(
                cpu_temperature if cpu_temperature is not None else tegra.cpu_temperature_c
            ),
            gpu_temperature_c=(
                gpu_temperature if gpu_temperature is not None else tegra.gpu_temperature_c
            ),
            power_mw=input_power if input_power is not None else tegra.power_mw,
            gpu_frequency_mhz=(
                tegra.gpu_frequency_mhz if tegra.gpu_frequency_mhz is not None else gpu_frequency
            ),
            disk_used_bytes=disk_used,
            disk_total_bytes=disk_total,
        )


class ResourceSampler:
    """Periodically collect snapshots on one cleanly stoppable background thread."""

    def __init__(
        self,
        collector: ResourceCollector | Callable[[], ResourceSnapshot],
        callback: Callable[[ResourceSnapshot], Any],
        *,
        interval_seconds: float,
    ) -> None:
        interval = _finite_number(interval_seconds, minimum=0.001)
        if interval is None:
            raise ValueError("resource sampling interval must be finite and positive")
        collect = getattr(collector, "collect", collector)
        if not callable(collect):
            raise TypeError("resource collector must be callable or provide collect()")
        if not callable(callback):
            raise TypeError("resource callback must be callable")

        self.interval_seconds = interval
        self._collect: Callable[[], ResourceSnapshot] = collect
        self._callback = callback
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _sample_once(self) -> None:
        try:
            snapshot = self._collect()
            if not isinstance(snapshot, ResourceSnapshot):
                raise TypeError("resource collector returned an invalid snapshot")
        except Exception:
            snapshot = ResourceSnapshot(timestamp=_utc_now())
        try:
            self._callback(snapshot)
        except Exception:
            logger.debug("Resource sample callback failed", exc_info=True)

    def _run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            started = time.monotonic()
            self._sample_once()
            remaining = self.interval_seconds - (time.monotonic() - started)
            if stop_event.wait(max(0.0, remaining)):
                return

    def start(self) -> ResourceSampler:
        """Start sampling; repeated calls while running are idempotent."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(self._stop_event,),
                name="helios-resource-sampler",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return self

    def stop(self, timeout: float | None = 5.0) -> bool:
        """Request shutdown and return whether the worker stopped in time."""

        if timeout is not None and _finite_number(timeout, minimum=0.0) is None:
            raise ValueError("resource sampler shutdown timeout must be finite and non-negative")
        with self._lock:
            thread = self._thread
            stop_event = self._stop_event
            stop_event.set()
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout)
        stopped = not thread.is_alive()
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
        return stopped

    close = stop

    def __enter__(self) -> ResourceSampler:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()


__all__ = [
    "ResourceCollector",
    "ResourceSampler",
    "ResourceSnapshot",
    "parse_tegrastats",
]
