from dataclasses import replace

import anyio
import httpx
import pytest

from automation.contracts import ToolDescriptor
from automation.home_assistant import catalog_report, exposure_matches, read_version
from automation.settings import ServerSettings, load_automation_settings
from scripts.mcp_doctor import main


def server():
    return ServerSettings(
        "ha", "https://home.test/api/mcp/assist", "TOKEN", ("fixture_read",), ("light.test",)
    )


def test_sanitized_catalog_checks_actual_selected_schema_not_hints():
    tool = ToolDescriptor(
        "ha",
        "fixture_read",
        "catalog",
        '{"type":"object","properties":{"entity_id":{"type":"string"}}}',
    )
    report = catalog_report("2026.9.4", server(), (tool,))
    assert report.reason_code == "exact_selector_available"
    assert "light.test" not in repr(report)
    assert (
        catalog_report("2026.9.4", server(), (), expected_version="2026.9.4").status
        == "incompatible"
    )
    assert (
        catalog_report("2026.9.4", server(), (tool,), expected_version="2025.1.0").reason_code
        == "version_mismatch"
    )
    intent = replace(
        tool, input_schema_json='{"type":"object","properties":{"name":{"type":"string"}}}'
    )
    assert (
        catalog_report("2026.9.4", server(), (intent,)).reason_code == "exact_selector_unverified"
    )
    assert catalog_report("unknown", server(), (tool,)).status == "unverified"


def test_exposure_is_exact_and_nonempty():
    assert exposure_matches(["light.test"], ["light.test", "sensor.test"])
    assert not exposure_matches(["light.hidden"], ["light.test"])
    assert not exposure_matches([], ["light.test"])
    assert not exposure_matches(["light.*"], ["light.test"])


def test_verified_2025_12_assist_schema_does_not_grant_exact_entity_actions():
    # Sanitized shape observed on the installed server; no entity names or state.
    observed = ToolDescriptor(
        "ha",
        "HassTurnOn",
        "observed-catalog",
        '{"type":"object","properties":{"name":{"type":"string"},'
        '"area":{"type":"string"},"domain":{"type":"array","items":{"type":"string"}}}}',
    )
    selected = replace(server(), endpoint="http://127.0.0.1:8123/api/mcp", tools=("HassTurnOn",))
    assert (
        catalog_report("2025.12.3", selected, (observed,)).reason_code
        == "exact_selector_unverified"
    )
    discovery = replace(selected, tools=(), entities=())
    report = catalog_report("2025.12.3", discovery, (observed,), expected_version="2025.12.3")
    assert report.status == "discovery_only"
    assert report.scoped_tool_count == 0
    assert (
        catalog_report("2025.12.3", discovery, (observed,), expected_version="2026.9.4").status
        == "incompatible"
    )


@pytest.mark.parametrize("status", [200, 401, 403, 307])
def test_version_probe_is_get_only_and_origin_bound(status):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://home.test/api/config"
        return httpx.Response(
            status,
            json={"version": "2026.9.4", "private": "never returned"},
            headers={"location": "https://other.test/"},
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            if status == 200:
                assert await read_version(server(), "secret", client=client) == "2026.9.4"
            else:
                with pytest.raises(ValueError):
                    await read_version(server(), "secret", client=client)
        assert len(requests) == 1

    anyio.run(scenario)


def test_inactive_example_does_not_activate_or_require_token():
    from config import PROJECT_ROOT

    settings = load_automation_settings(
        PROJECT_ROOT / "examples/automation.home-assistant.toml", environ={}
    )
    assert not settings.enabled
    assert settings.servers[0].tools == ()


def test_doctor_disabled_never_connects(tmp_path, capsys):
    config = tmp_path / "disabled.toml"
    config.write_text("enabled=false\n")
    assert main(["--config", str(config)]) == 2
    assert '"connected": false' in capsys.readouterr().out
