from __future__ import annotations

from pathlib import Path

import pytest

import config as helios_config


def pytest_configure(config: pytest.Config) -> None:
    """Create the parent of an explicitly nested ``--basetemp`` path.

    Pytest creates the final basetemp directory itself with ``parents=False``.
    Consequently, a clean checkout fails before fixture setup when invoked as
    ``--basetemp=.test-tmp/run-name`` and ``.test-tmp`` does not yet exist.
    """

    raw_basetemp = config.getoption("basetemp")
    if raw_basetemp is None:
        return
    Path(raw_basetemp).expanduser().parent.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def force_offline_default_llm_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep ordinary tests local even when the developer shell enables remote."""

    monkeypatch.setattr(
        helios_config,
        "LLM_SETTINGS",
        helios_config.LLMSettings(),
    )
