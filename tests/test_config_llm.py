from __future__ import annotations

import logging
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


def test_environment_controls_log_level_and_destination(tmp_path: Path) -> None:
    terminal = config.Settings.from_env(
        tmp_path,
        environ={
            "HELIOS_LOG_LEVEL": "debug",
            "HELIOS_LOG_FILE": "-",
        },
    )
    file_logging = config.Settings.from_env(
        tmp_path,
        environ={"HELIOS_LOG_FILE": "logs/helios.log"},
    )

    assert terminal.log_level == logging.DEBUG
    assert terminal.log_file is None
    assert file_logging.log_file == tmp_path / "logs/helios.log"


def test_invalid_environment_log_level_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigurationError, match="HELIOS_LOG_LEVEL"):
        config.Settings.from_env(
            tmp_path,
            environ={"HELIOS_LOG_LEVEL": "verbose"},
        )


def test_barge_in_is_opt_in_and_strictly_parsed(tmp_path: Path) -> None:
    defaults = config.Settings.from_env(tmp_path, environ={})
    enabled = config.Settings.from_env(
        tmp_path,
        environ={"HELIOS_BARGE_IN_ENABLED": "true"},
    )

    assert not defaults.barge_in_enabled
    assert enabled.barge_in_enabled

    with pytest.raises(config.ConfigurationError, match="HELIOS_BARGE_IN_ENABLED"):
        config.Settings.from_env(
            tmp_path,
            environ={"HELIOS_BARGE_IN_ENABLED": "sometimes"},
        )


def test_remote_file_uses_its_enabled_default_and_environment_can_disable_it(
    tmp_path: Path,
) -> None:
    routing_path = tmp_path / "routing.toml"
    routing_path.write_text(VALID_ROUTING, encoding="utf-8")

    enabled = config.Settings.from_env(
        tmp_path,
        environ={"HELIOS_LLM_CONFIG": str(routing_path)},
    )
    disabled = config.Settings.from_env(
        tmp_path,
        environ={
            "HELIOS_LLM_CONFIG": str(routing_path),
            "HELIOS_LLM_REMOTE_ENABLED": "false",
        },
    )

    assert enabled.llm.remote_enabled
    assert enabled.llm.routing_policy == "remote_first"
    assert not disabled.llm.remote_enabled
    assert disabled.llm.routing_policy == "local_only"


def test_repository_defaults_to_codex_remote_first_with_local_fallback() -> None:
    settings = config.Settings.from_env(PROJECT_ROOT, environ={})

    assert settings.llm.routing_file == (PROJECT_ROOT / config.DEFAULT_LLM_CONFIG).resolve()
    assert settings.llm.remote_enabled
    assert settings.llm.routing_policy == "remote_first"
    assert settings.llm.talk.candidates[-1] == "local-talk"


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
        "llm-routing.codex-subscription.toml",
    ],
)
def test_committed_routing_examples_are_valid(name: str) -> None:
    settings = config.load_llm_settings(PROJECT_ROOT / "examples" / name)

    assert settings.routing_file is not None


def test_codex_subscription_uses_a_realistic_first_audio_health_objective() -> None:
    settings = config.load_llm_settings(
        PROJECT_ROOT / "examples" / "llm-routing.codex-subscription.toml"
    )

    assert settings.health.maximum_talk_first_audio_ms == 30_000


def test_codex_subscription_has_target_specific_talk_limits() -> None:
    settings = config.load_llm_settings(
        PROJECT_ROOT / "examples" / "llm-routing.codex-subscription.toml"
    )
    targets = {target.name: target for target in settings.targets}

    for name in ("codex-talk-luna", "codex-talk-terra", "codex-talk-sol"):
        assert targets[name].max_output_words == 50
        assert targets[name].max_output_tokens == 128
    assert targets["local-talk"].max_output_words == 20
    assert targets["local-talk"].max_output_tokens == 40
    assert targets["codex-think-sol"].max_output_words is None


def test_codex_subscription_has_adaptive_remote_tiers_and_fast_speech() -> None:
    settings = config.load_llm_settings(
        PROJECT_ROOT / "examples" / "llm-routing.codex-subscription.toml"
    )
    targets = {target.name: target for target in settings.targets}

    assert [
        targets[name].model for name in ("codex-talk-luna", "codex-talk-terra", "codex-talk-sol")
    ] == ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
    assert [
        targets[name].min_complexity_score
        for name in ("codex-talk-luna", "codex-talk-terra", "codex-talk-sol")
    ] == [0, 3, 5]
    assert settings.talk.first_speech_min_chars == 0
    assert settings.talk.speech_chunk_max_chars == 80
    assert settings.talk.first_visible_token_seconds == 15.0


def test_codex_subscription_fails_closed_on_stale_or_unvalidated_network() -> None:
    settings = config.load_llm_settings(
        PROJECT_ROOT / "examples" / "llm-routing.codex-subscription.toml"
    )

    assert settings.unknown_connectivity == "prefer_local"
    assert settings.network.enabled
    assert settings.network.probe_url == "https://chatgpt.com/"
    assert settings.network.probe_interval_seconds == 3.0
    assert settings.network.result_max_age_seconds == 6.0
    assert settings.network.probe_timeout_seconds == 1.2
    assert settings.network.probe_bytes == 32_768
    assert settings.network.goodput_probe_interval_seconds == 60.0


@pytest.mark.parametrize(
    "body",
    [
        "schema_version = true\n",
        'schema_version = 1\n[router]\nremote_enabled = "false"\n',
        'schema_version = 1\n[budget]\nenabled = "false"\n',
        "schema_version = 1\n[modes.talk]\nmax_output_tokens = true\n",
        "schema_version = 1\n[modes.talk]\nspeech_chunk_max_chars = true\n",
        "schema_version = 1\n[modes.talk]\nfirst_visible_token_seconds = true\n",
        'schema_version = 1\n[network]\nenabled = "true"\n',
        "schema_version = 1\n[network]\nrequire_wifi = 1\n",
        'schema_version = 1\n[network]\nprobe_url = "http://example.invalid/"\n',
        (
            "schema_version = 1\n[network]\nprobe_interval_seconds = 10.0\n"
            "goodput_probe_interval_seconds = 5.0\n"
        ),
        (
            'schema_version = 1\n[targets.local]\nprovider = "ollama"\n'
            'model = "test"\nmax_output_words = true\n'
        ),
        (
            'schema_version = 1\n[targets.remote]\nprovider = "ollama"\n'
            'model = "test"\nmin_complexity_score = true\n'
        ),
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


def test_codex_provider_requires_stdio_and_forbids_api_key_configuration() -> None:
    provider = config.LLMProviderSettings(
        name="openai-codex",
        adapter="codex_app_server",
        endpoint="stdio://codex",
        locality="remote",
    )

    assert provider.api_key_env is None

    with pytest.raises(config.ConfigurationError, match="local ChatGPT sign-in"):
        config.LLMProviderSettings(
            name="openai-codex",
            adapter="codex_app_server",
            endpoint="stdio://codex",
            locality="remote",
            api_key_env="OPENAI_API_KEY",
        )
    for invalid_endpoint in ("stdio://other", "stdio://codex/", "stdio://codex:123"):
        with pytest.raises(config.ConfigurationError, match="exactly"):
            config.LLMProviderSettings(
                name="openai-codex",
                adapter="codex_app_server",
                endpoint=invalid_endpoint,
                locality="remote",
            )
