from dataclasses import FrozenInstanceError, replace

import pytest

from automation.contracts import ActionProposal, Confirmation, ToolDescriptor, object_json
from automation.settings import AutomationConfigError, AutomationSettings, load_automation_settings
from config import Settings


def proposal(**overrides):
    values = dict(
        server_id="home",
        tool_name="light",
        catalog_id="catalog-v1",
        session_id="session",
        turn_id="turn",
        action_id="action",
        arguments_json='{"b":2,"a":1}',
        expires_at=100.0,
    )
    return ActionProposal(**(values | overrides))


def test_proposal_is_canonical_immutable_and_bound_to_catalog():
    action = proposal()
    assert action.arguments_json == '{"a":1,"b":2}'
    assert action.fingerprint == proposal(arguments_json='{"a":1,"b":2}').fingerprint
    assert action.fingerprint != replace(action, catalog_id="new").fingerprint
    with pytest.raises(FrozenInstanceError):
        action.action_id = "changed"


@pytest.mark.parametrize("value", ["[]", '{"x":NaN}', '{"x":1,"x":2}', '{"x":1e999}', '"text"'])
def test_invalid_arguments_rejected(value):
    with pytest.raises(ValueError):
        object_json(value)


@pytest.mark.parametrize(
    "schema", ["[]", '{"type":"string"}', "{}", '{"type":"object","properties":3}']
)
def test_invalid_input_schema(schema):
    with pytest.raises(ValueError):
        ToolDescriptor("home", "light", "catalog", schema)


def test_expiry_and_confirmation_exact_binding():
    action = proposal()
    confirmation = Confirmation(action.fingerprint, action.session_id, 90)
    assert confirmation.matches(action, 89)
    assert not confirmation.matches(action, 90)
    assert not confirmation.matches(replace(action, arguments_json='{"a":3}'), 89)
    assert not confirmation.matches(replace(action, session_id="other"), 89)
    assert action.is_expired(100)
    with pytest.raises(ValueError):
        action.is_expired(float("nan"))


def test_no_config_means_disabled_even_with_credentials(tmp_path):
    assert load_automation_settings() == AutomationSettings()
    settings = Settings.from_env(tmp_path, environ={"MCP_TOKEN": "secret"})
    assert not settings.automation.enabled
    assert not settings.automation.servers
    assert not settings.automation.allow_remote_context


def write_config(tmp_path, extra="", endpoint="https://home.example/api/mcp"):
    path = tmp_path / "automation.toml"
    path.write_text(
        f'enabled = true\n{extra}\n[[servers]]\nserver_id="home"\nendpoint="{endpoint}"\n'
        'credential_env="MCP_TOKEN"\ntools=["light"]\nentities=["light.office"]\n',
        encoding="utf-8",
    )
    return path


def test_enabled_configuration_requires_credential_and_explicit_path(tmp_path):
    path = write_config(tmp_path)
    with pytest.raises(AutomationConfigError):
        load_automation_settings(path, environ={})
    result = load_automation_settings(path, environ={"MCP_TOKEN": "private"})
    assert result.enabled
    assert "private" not in repr(result)
    assert (
        Settings.from_env(
            tmp_path, environ={"HELIOS_AUTOMATION_CONFIG": path.name, "MCP_TOKEN": "private"}
        ).automation
        == result
    )


@pytest.mark.parametrize(
    "extra",
    [
        "enabled=1",
        "unknown=true",
        "max_calls_per_turn=0",
        "timeout_seconds=nan",
        "allow_remote_context=1",
    ],
)
def test_invalid_settings_fail_closed(tmp_path, extra):
    path = write_config(tmp_path, extra)
    if extra.startswith("enabled="):
        path.write_text(path.read_text().replace("enabled = true\n", ""))
    with pytest.raises(AutomationConfigError):
        load_automation_settings(path, environ={"MCP_TOKEN": "x"})


@pytest.mark.parametrize(
    "endpoint",
    ["http://home.example/mcp", "https://u:p@home.example/mcp", "https://home.example/mcp#x"],
)
def test_unsafe_endpoints_rejected(tmp_path, endpoint):
    with pytest.raises(AutomationConfigError):
        load_automation_settings(
            write_config(tmp_path, endpoint=endpoint), environ={"MCP_TOKEN": "x"}
        )
