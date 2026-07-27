from __future__ import annotations

from pathlib import Path

import pytest

import config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


VALID_ROUTING = """
schema_version = 1

[router]
policy = "remote_first"
remote_enabled = true
allowlist = ["groq"]

[privacy]
default = "remote_allowed"
allow_remote_transcripts = true
allow_remote_context = false
allow_remote_rag_context = false

[budget]
catalog_path = "model-catalog.json"
ledger_path = "usage.jsonl"
zero_cost_only = true

[modes.talk]
candidates = ["groq-talk", "local-talk"]
max_output_tokens = 80

[modes.think]
candidates = ["local-think"]

[providers.groq]
adapter = "openai_chat_sse"
endpoint = "https://api.groq.com/openai/v1/chat/completions"
locality = "remote"
api_key_env = "GROQ_API_KEY"

[targets.groq-talk]
provider = "groq"
model = "example-model"
catalog_id = "groq/example-model"
languages = ["it", "en"]

[targets.local-talk]
provider = "ollama"
model_by_language = { it = "emilia-gemma3:1b", en = "emilia-en-gemma3:1b" }

[targets.local-think]
provider = "ollama"
model = "qwen3:0.6b"
"""


def test_load_llm_settings_resolves_paths_and_keeps_key_names_only(
    tmp_path: Path,
) -> None:
    routing_path = tmp_path / "routing.toml"
    routing_path.write_text(VALID_ROUTING, encoding="utf-8")

    settings = config.load_llm_settings(routing_path)

    assert settings.routing_policy == "remote_first"
    assert settings.remote_enabled
    assert settings.providers[0].api_key_env == "GROQ_API_KEY"
    assert settings.budget.catalog_path == tmp_path / "model-catalog.json"
    assert settings.budget.ledger_path == tmp_path / "usage.jsonl"
    assert settings.targets[1].model_for_language("en") == "emilia-en-gemma3:1b"


def test_environment_can_disable_but_not_create_remote_routing(tmp_path: Path) -> None:
    settings = config.Settings.from_env(
        tmp_path,
        environ={
            "HELIOS_LLM_REMOTE_ENABLED": "true",
            "HELIOS_LLM_POLICY": "remote_only",
        },
    )

    assert not settings.llm.remote_enabled
    assert settings.llm.routing_policy == "local_only"


def test_remote_file_still_requires_the_independent_environment_gate(
    tmp_path: Path,
) -> None:
    routing_path = tmp_path / "routing.toml"
    routing_path.write_text(VALID_ROUTING, encoding="utf-8")

    disabled = config.Settings.from_env(
        tmp_path,
        environ={"HELIOS_LLM_CONFIG": str(routing_path)},
    )
    enabled = config.Settings.from_env(
        tmp_path,
        environ={
            "HELIOS_LLM_CONFIG": str(routing_path),
            "HELIOS_LLM_REMOTE_ENABLED": "true",
        },
    )

    assert not disabled.llm.remote_enabled
    assert disabled.llm.routing_policy == "local_only"
    assert enabled.llm.remote_enabled
    assert enabled.llm.routing_policy == "remote_first"


def test_invalid_routing_file_fails_closed_without_exposing_content(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    routing_path = tmp_path / "routing.toml"
    routing_path.write_text(
        """
schema_version = 1
[router]
remote_enabled = true
[providers.bad]
adapter = "openai_chat_sse"
endpoint = "http://not-tls.invalid/v1/chat/completions"
locality = "remote"
api_key_env = "SUPER_SECRET_KEY"
""",
        encoding="utf-8",
    )

    settings = config.Settings.from_env(
        tmp_path,
        environ={
            "HELIOS_LLM_CONFIG": str(routing_path),
            "HELIOS_LLM_REMOTE_ENABLED": "true",
        },
    )

    assert settings.llm.emergency_local_only
    assert not settings.llm.remote_enabled
    assert "SUPER_SECRET_KEY" not in caplog.text


def test_target_options_reject_embedded_secrets(tmp_path: Path) -> None:
    routing_path = tmp_path / "routing.toml"
    routing_path.write_text(
        """
schema_version = 1
[providers.remote]
adapter = "openai_chat_sse"
endpoint = "https://example.invalid/v1/chat/completions"
locality = "remote"
api_key_env = "REMOTE_API_KEY"
[targets.remote]
provider = "remote"
model = "example"
options = { api_key = "must-not-be-here" }
""",
        encoding="utf-8",
    )

    with pytest.raises(config.ConfigurationError, match="cannot contain secrets"):
        config.load_llm_settings(routing_path)


@pytest.mark.parametrize(
    "name",
    [
        "llm-routing.offline.toml",
        "llm-routing.free-tier-first.toml",
        "llm-routing.paid-first.toml",
        "llm-routing.local-first-escalation.toml",
    ],
)
def test_committed_routing_examples_are_valid(name: str) -> None:
    settings = config.load_llm_settings(PROJECT_ROOT / "examples" / name)

    assert settings.routing_file is not None


@pytest.mark.parametrize(
    "body",
    [
        "schema_version = true\n",
        'schema_version = 1\n[router]\nremote_enabled = "false"\n',
        'schema_version = 1\n[budget]\nenabled = "false"\n',
        "schema_version = 1\n[modes.talk]\nmax_output_tokens = true\n",
        "schema_version = 1\n[timeouts]\nconnect_seconds = 1" + ("0" * 400) + "\n",
    ],
)
def test_toml_types_are_strict_and_cannot_turn_strings_into_truthy_flags(
    tmp_path: Path,
    body: str,
) -> None:
    routing_path = tmp_path / "routing.toml"
    routing_path.write_text(body, encoding="utf-8")

    with pytest.raises(config.ConfigurationError):
        config.load_llm_settings(routing_path)


def test_nested_target_options_cannot_hide_credentials(tmp_path: Path) -> None:
    routing_path = tmp_path / "routing.toml"
    routing_path.write_text(
        """
schema_version = 1
[providers.remote]
adapter = "openai_chat_sse"
endpoint = "https://example.invalid/v1"
locality = "remote"
api_key_env = "REMOTE_API_KEY"
[targets.remote]
provider = "remote"
model = "example"
options = { extension = { Authorization = "must-not-be-here" } }
""",
        encoding="utf-8",
    )

    with pytest.raises(config.ConfigurationError, match="cannot contain secrets"):
        config.load_llm_settings(routing_path)


def test_denylist_typos_and_inconsistent_adapter_locality_are_rejected() -> None:
    with pytest.raises(config.ConfigurationError, match="denylist.*unknown"):
        config.LLMSettings(denylist=("gork",))

    with pytest.raises(config.ConfigurationError, match="must be remote"):
        config.LLMProviderSettings(
            name="compatible",
            adapter="openai_chat_sse",
            endpoint="http://127.0.0.1:8000/v1",
            locality="device",
        )
