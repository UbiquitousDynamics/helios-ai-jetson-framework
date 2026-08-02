from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import config
from api.provider_factory import configured_provider_factory


def test_import_and_factory_creation_keep_remote_adapters_lazy() -> None:
    project_root = Path(__file__).resolve().parents[1]
    code = """
import sys
import config
from api.provider_factory import configured_provider_factory

adapter_modules = {
    "api.providers.codex_app_server",
    "api.providers.openai_chat_sse",
}
assert adapter_modules.isdisjoint(sys.modules)
settings = config.LLMProviderSettings(
    name="remote",
    adapter="openai_chat_sse",
    endpoint="https://provider.invalid/v1",
    locality="remote",
    api_key_env="REMOTE_API_KEY",
)
assert configured_provider_factory(settings) is not None
assert adapter_modules.isdisjoint(sys.modules)
"""

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_openai_factory_builds_the_configured_adapter_only_when_called() -> None:
    settings = config.LLMProviderSettings(
        name="remote",
        adapter="openai_chat_sse",
        endpoint="https://provider.invalid/v1",
        locality="remote",
        api_key_env="REMOTE_API_KEY",
    )

    factory = configured_provider_factory(settings)

    assert factory is not None
    provider = factory()
    assert provider.identity.name == "remote"
    assert provider.identity.endpoint == "https://provider.invalid/v1"
    provider.close()


def test_codex_factory_builds_an_isolated_app_server_adapter() -> None:
    settings = config.LLMProviderSettings(
        name="codex",
        adapter="codex_app_server",
        endpoint="stdio://codex",
        locality="remote",
    )

    factory = configured_provider_factory(settings)

    assert factory is not None
    provider = factory()
    assert provider.identity.name == "codex"
    assert provider.identity.endpoint == "stdio://codex"
    provider.close()


def test_ollama_remains_owned_by_the_api_client_composition_root() -> None:
    settings = config.LLMProviderSettings(
        name="ollama",
        adapter="ollama",
        endpoint="http://127.0.0.1:11434",
        locality="device",
    )

    assert configured_provider_factory(settings) is None
