from datetime import date

import pytest

import config
from api.catalog import ModelCatalog
from api.target_compiler import TargetCompiler


MODELS = {"talk": "local-talk-model", "think": "local-think-model"}


def compiler(
    settings: config.LLMSettings,
    **overrides: object,
) -> TargetCompiler:
    values: dict[str, object] = {
        "models": MODELS,
        "language": "en",
        "default_retry_attempts": 3,
        "registered_providers": {"ollama", "remote"},
        "ollama_remote": False,
        "ollama_enabled": True,
    }
    values.update(overrides)
    return TargetCompiler(settings, **values)  # type: ignore[arg-type]


def remote_provider() -> config.LLMProviderSettings:
    return config.LLMProviderSettings(
        name="remote",
        adapter="openai_chat_sse",
        endpoint="https://provider.invalid/v1",
        locality="remote",
        api_key_env="REMOTE_API_KEY",
    )


def test_legacy_modes_compile_to_the_original_ollama_targets() -> None:
    targets = compiler(config.LLMSettings()).compile_all()

    talk = targets["talk"][0]
    think = targets["think"][0]
    assert talk.route.name == "ollama-talk"
    assert talk.route.model == "local-talk-model"
    assert think.route.name == "ollama-think"
    assert think.route.model == "local-think-model"
    assert talk.route.languages == frozenset({"en"})
    assert not talk.route.remote
    assert talk.retry_attempts == 3


def test_catalog_and_mode_caps_are_applied_while_preserving_priority() -> None:
    catalog = ModelCatalog.from_mapping(
        {
            "schema_version": 1,
            "catalog_revision": "test",
            "verified_on": "2026-07-27",
            "expires_on": "2026-08-27",
            "models": [
                {
                    "id": "remote/example",
                    "provider": "remote",
                    "model": "remote-en",
                    "input_per_million_usd": "1",
                    "output_per_million_usd": "1",
                    "context_window": 100,
                    "max_output_tokens": 32,
                    "free_tier": False,
                }
            ],
        },
        today=date(2026, 7, 27),
    )
    settings = config.LLMSettings(
        talk=config.LLMModeSettings(
            candidates=("remote-talk", "local-talk"),
            max_output_tokens=64,
        ),
        providers=(remote_provider(),),
        targets=(
            config.LLMTargetSettings(
                name="remote-talk",
                provider="remote",
                model_by_language=(("it", "remote-it"), ("en", "remote-en")),
                catalog_id="remote/example",
                context_window=999,
                max_output_tokens=128,
                max_output_words=50,
                min_complexity_score=3,
                retry_attempts=2,
                options=(("reasoning_effort", "low"),),
            ),
            config.LLMTargetSettings(
                name="local-talk",
                provider="ollama",
                model="local-model",
                max_output_tokens=40,
            ),
        ),
    )

    remote, local = compiler(settings, catalog=catalog).compile("talk")

    assert remote.route.model == "remote-en"
    assert remote.route.remote
    assert remote.route.enabled
    assert remote.route.priority == 0
    assert remote.route.context_window == 100
    assert remote.route.max_output_tokens == 32
    assert remote.max_output_tokens == 32
    assert remote.max_output_words == 50
    assert remote.retry_attempts == 2
    assert remote.options == {"reasoning_effort": "low"}
    assert remote.price is catalog.get("remote/example")
    assert local.route.priority == 1
    assert local.route.max_output_tokens == 40
    assert local.price is None


def test_unregistered_remote_is_disabled_and_emergency_local_is_appended() -> None:
    settings = config.LLMSettings(
        emergency_local_only=True,
        talk=config.LLMModeSettings(candidates=("remote-talk",)),
        providers=(remote_provider(),),
        targets=(
            config.LLMTargetSettings(
                name="remote-talk",
                provider="remote",
                model="remote-model",
            ),
        ),
    )

    remote, local = compiler(
        settings,
        registered_providers={"ollama"},
    ).compile("talk")

    assert not remote.route.enabled
    assert local.route.name == "ollama-emergency-talk"
    assert local.route.model == "local-talk-model"
    assert local.route.priority == 1
    assert local.route.enabled


def test_invalid_mode_and_retry_default_are_rejected() -> None:
    with pytest.raises(ValueError, match="retry"):
        compiler(config.LLMSettings(), default_retry_attempts=0)
    with pytest.raises(ValueError, match="Unknown model mode"):
        compiler(config.LLMSettings()).compile("other")
