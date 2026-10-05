"""Explicit metadata-only live check. No device actions or model requests."""

import os
from pathlib import Path

import anyio
import pytest

from automation.client import MCPClient
from automation.home_assistant import catalog_report, read_version
from automation.settings import load_automation_settings


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("HELIOS_MCP_LIVE_DISCOVERY") != "1",
    reason="set HELIOS_MCP_LIVE_DISCOVERY=1 for explicit HA metadata-only verification",
)
def test_explicit_home_assistant_version_and_catalog():
    config_path = os.environ.get("HELIOS_AUTOMATION_CONFIG")
    assert config_path, "Explicit automation configuration required"
    settings = load_automation_settings(Path(config_path))
    assert settings.enabled
    server_id = os.environ.get("HELIOS_MCP_LIVE_SERVER", "homeassistant")
    server = next(item for item in settings.servers if item.server_id == server_id)

    async def scenario():
        version = await read_version(
            server, os.environ[server.credential_env], timeout=settings.timeout_seconds
        )
        async with MCPClient(settings, server_id, environ=os.environ) as client:
            catalog = await client.discover()
        report = catalog_report(
            version, server, catalog, expected_version=os.environ.get("HELIOS_HA_EXPECTED_VERSION")
        )
        assert report.status in {"discovery_only", "schema_checked"}

    anyio.run(scenario)
