"""Opt-in scoped reads and denials; no audio, models or device actions."""

import json
import os
import uuid
from pathlib import Path

import anyio
import pytest

from automation.client import MCPClient
from automation.read_runtime import read_only_runtime
from automation.settings import load_automation_settings
from recognizer.speech_recognizer import RecognitionResult


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("HELIOS_MCP_LIVE_READS") != "1",
    reason="set HELIOS_MCP_LIVE_READS=1 for explicit scoped HA read verification",
)
def test_explicit_scoped_state_read_and_independent_bridge_denial(tmp_path):
    config_path = os.environ.get("HELIOS_AUTOMATION_CONFIG")
    assert config_path, "Explicit read-only configuration required"
    settings = load_automation_settings(Path(config_path))
    server = next(item for item in settings.servers if item.server_id == "homeassistant_state")
    aliases = json.loads(os.environ.get("HELIOS_HA_READ_ALIASES", "{}"))
    assert aliases, "Explicit local alias mapping required"
    alias = next(iter(aliases))
    spoken = []
    # The runtime validates every configured tool/entity before opening a client.
    with read_only_runtime(settings, state_dir=tmp_path, speak=spoken.append) as controller:

        async def scenario():
            command = "domotica stato " + alias
            await controller.handle(
                command,
                RecognitionResult(text=command, is_final=True),
                session_id=str(uuid.uuid4()),
                turn_id="explicit-live-read",
            )
            assert len(spoken) == 1
            assert spoken[0].startswith("Home Assistant indica: ")
            assert spoken[0].endswith(
                ("stato accesa.", "stato spenta.", "stato sconosciuto.", "stato non disponibile.")
            )
            # Probe the bridge's independent scope boundary, bypassing local policy.
            outside = "light.helios_scope_probe"
            while outside in server.entities:
                outside += "_excluded"
            async with MCPClient(settings, server.server_id, environ=os.environ) as client:
                catalog = await client.discover()
                assert [item.name for item in catalog] == ["GetEntityState"]
                result = await client.call(catalog[0], json.dumps({"entity_id": outside}))
                assert result.is_error, "Bridge accepted an unconfigured entity"

        anyio.run(scenario)
