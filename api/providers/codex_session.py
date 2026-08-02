"""Shared, provider-independent safety helpers for isolated Codex sessions."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "CODEX_DISABLED_FEATURES",
    "codex_child_environment",
    "copy_chatgpt_auth",
    "field_value",
]


CODEX_DISABLED_FEATURES = (
    "features.apps=false",
    "features.plugins=false",
    "features.search_tool=false",
    "features.shell_tool=false",
    "features.skill_search=false",
    "features.standalone_web_search=false",
    "features.tool_search=false",
    "features.unified_exec=false",
    "features.web_search=false",
    "features.web_search_request=false",
)


def codex_child_environment(codex_home: Path | None = None) -> dict[str, str]:
    """Force a Codex child process to use sign-in auth instead of API keys."""

    child_environment = {
        "OPENAI_API_KEY": "",
        "CODEX_API_KEY": "",
    }
    if codex_home is not None:
        child_environment["CODEX_HOME"] = str(codex_home)
    return child_environment


def copy_chatgpt_auth(source_home: Path, isolated_home: Path) -> None:
    """Copy only Codex authentication, never tools or project settings."""

    isolated_home.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = source_home / "auth.json"
    if not source.is_file() or source.is_symlink():
        return
    destination = isolated_home / "auth.json"
    shutil.copyfile(source, destination)
    destination.chmod(0o600)


def field_value(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either an SDK mapping or an SDK response object."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)
