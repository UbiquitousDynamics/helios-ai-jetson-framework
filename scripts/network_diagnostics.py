"""Inspect the passive network gate and run one sanitized HTTPS quality probe."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from enum import Enum
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from api.connectivity import ConnectivityMonitor, LinuxNetworkInspector  # noqa: E402


def _json_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported diagnostic value: {type(value).__name__}")


def main() -> int:
    settings = config.LLM_SETTINGS.network
    inspector = LinuxNetworkInspector()
    passive = inspector.inspect(
        require_wifi=settings.require_wifi,
        interface_allowlist=settings.interface_allowlist,
    )
    monitor = ConnectivityMonitor(
        settings,
        inspector=inspector,
        route_watcher_factory=None,
    )
    active = monitor.refresh_once()
    output = {
        "configured_enabled": settings.enabled,
        "passive_gate": asdict(passive),
        "active_quality": asdict(active),
        "decision": active.connectivity.value,
    }
    print(json.dumps(output, default=_json_value, indent=2, sort_keys=True))
    return 0 if active.connectivity.value == "online" else 1


if __name__ == "__main__":
    raise SystemExit(main())
