from __future__ import annotations

import pytest

import config


@pytest.fixture(autouse=True)
def force_offline_default_llm_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep ordinary tests local even when the developer shell enables remote."""

    monkeypatch.setattr(config, "LLM_SETTINGS", config.LLMSettings())
