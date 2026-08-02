"""Opt-in live certification; excluded from normal CI and local test runs."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import config
from api.api_client import APIClient
from api.routing import Connectivity


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.mark.remote_live
def test_configured_remote_provider_streams_without_exposing_content() -> None:
    if not _enabled(os.environ.get("HELIOS_LLM_LIVE")):
        pytest.skip("set HELIOS_LLM_LIVE=1 to authorize a live remote request")
    routing_value = os.environ.get("HELIOS_LLM_LIVE_CONFIG", "").strip()
    if not routing_value:
        pytest.skip("set HELIOS_LLM_LIVE_CONFIG to a reviewed remote-only TOML file")

    routing_path = Path(routing_value).expanduser().resolve()
    llm = config.load_llm_settings(routing_path)
    if not llm.remote_enabled or llm.routing_policy != "remote_only":
        pytest.skip("live certification requires remote_enabled and remote_only")
    missing_keys = [
        provider.api_key_env
        for provider in llm.providers
        if provider.enabled
        and provider.locality == "remote"
        and provider.api_key_env
        and not os.environ.get(provider.api_key_env)
    ]
    if missing_keys:
        pytest.skip("one or more configured credential variables are absent")

    client = APIClient(
        llm_settings=llm,
        language=os.environ.get("HELIOS_LANGUAGE", "en"),
        connectivity=Connectivity.ONLINE,
        retry_attempts=1,
        retry_wait=0,
    )
    try:
        response = client.think(
            "Reply with the single word OK.",
            tts=False,
            connectivity=Connectivity.ONLINE,
        )
        assert response.strip()
        successes = [
            event for event in client.metrics.snapshot() if event.event == "llm_attempt_succeeded"
        ]
        assert successes
        assert successes[-1].provider != "ollama"
    finally:
        client.close()
