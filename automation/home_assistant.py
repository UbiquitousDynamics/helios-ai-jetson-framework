"""Non-mutating Home Assistant compatibility and exposure diagnostics."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

from automation.contracts import ToolDescriptor
from automation.settings import ServerSettings


@dataclass(frozen=True, slots=True)
class HomeAssistantReport:
    version: str
    catalog_id: str
    tool_count: int
    scoped_tool_count: int
    status: str
    reason_code: str


def catalog_report(
    version: str,
    server: ServerSettings,
    catalog: tuple[ToolDescriptor, ...],
    *,
    expected_version: str | None = None,
) -> HomeAssistantReport:
    digest = hashlib.sha256(
        json.dumps([(tool.name, tool.catalog_id) for tool in catalog]).encode()
    ).hexdigest()
    scoped = [
        tool for tool in catalog if tool.server_id == server.server_id and tool.name in server.tools
    ]

    def report(status, reason):
        return HomeAssistantReport(version, digest, len(catalog), len(scoped), status, reason)

    if not re.fullmatch(r"20\d{2}\.\d{1,2}\.\d+(?:[a-z0-9.-]*)?", version):
        return report("unverified", "version_unknown")
    if expected_version is not None and version != expected_version:
        return report("incompatible", "version_mismatch")
    if len(scoped) != len(server.tools):
        return report("incompatible", "configured_tool_missing")
    if not scoped:
        return report("discovery_only", "no_tools_authorized")
    # Schema shape is evidence, not authorization. Intent/name/area selectors
    # cannot be assumed to uniquely identify a configured entity.
    exact = all(
        json.loads(tool.input_schema_json).get("properties", {}).get("entity_id", {}).get("type")
        == "string"
        for tool in scoped
    )
    return report(
        "schema_checked", "exact_selector_available" if exact else "exact_selector_unverified"
    )


def exposure_matches(configured: Iterable[str], exposed: Iterable[str]) -> bool:
    """No fuzzy names, unexposed entities or wildcard expansion."""
    configured, exposed = set(configured), set(exposed)
    return (
        bool(configured)
        and configured <= exposed
        and all("*" not in entity for entity in configured)
    )


async def read_version(
    server: ServerSettings, token: str, *, client: Any = None, timeout: float = 10
) -> str:
    import httpx

    parsed = urlsplit(server.endpoint)
    url = f"{parsed.scheme}://{parsed.netloc}/api/config"
    owned = client is None
    client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)
    try:
        async with client.stream(
            "GET", url, headers={"Authorization": f"Bearer {token}"}
        ) as response:
            if response.status_code in (401, 403):
                raise ValueError("home_assistant_auth_denied")
            if response.status_code != 200:
                raise ValueError("home_assistant_version_unavailable")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 65536:
                    raise ValueError("home_assistant_config_too_large")
            result = json.loads(data)
            version = result.get("version")
            if not isinstance(version, str):
                raise ValueError("home_assistant_version_unknown")
            return version
    finally:
        if owned:
            await client.aclose()
