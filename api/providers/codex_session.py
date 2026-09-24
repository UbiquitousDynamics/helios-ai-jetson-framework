"""Shared, provider-independent safety helpers for isolated Codex sessions."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "CODEX_DISABLED_FEATURES",
    "codex_child_environment",
    "copy_chatgpt_auth",
    "ensure_persistent_chatgpt_auth",
    "field_value",
    "persistent_codex_auth_home",
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


def persistent_codex_auth_home(source_home: Path) -> Path:
    """Return Helios's durable, auth-only Codex home.

    Codex refresh tokens rotate. Per-runtime copies can therefore invalidate
    one another as soon as any child refreshes its token. The Helios profile
    deliberately contains only ``auth.json`` and persists across child
    runtimes; it never imports the user's Codex configuration, tools, or MCP
    settings. ``HELIOS_CODEX_AUTH_HOME`` permits an explicit private location.
    """

    configured = os.environ.get("HELIOS_CODEX_AUTH_HOME")
    if configured:
        return Path(configured).expanduser()
    return source_home.parent / ".helios-codex"


def ensure_persistent_chatgpt_auth(source_home: Path, auth_home: Path) -> Path:
    """Create an auth-only durable profile, bootstrapping it once if possible."""

    if auth_home.is_symlink():
        raise RuntimeError("Helios Codex auth home must not be a symbolic link")
    auth_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    auth_home.chmod(0o700)
    destination = auth_home / "auth.json"
    if destination.is_symlink():
        raise RuntimeError("Helios Codex auth profile must not be a symbolic link")
    if destination.is_file():
        return auth_home
    if destination.exists():
        raise RuntimeError("Helios Codex auth profile must be a regular file")

    source = source_home / "auth.json"
    if source == destination:
        return auth_home
    if not source.is_file() or source.is_symlink():
        return auth_home
    staged = auth_home / "auth.json.helios.tmp"
    if staged.exists() or staged.is_symlink():
        raise RuntimeError("Helios Codex auth staging file already exists")
    shutil.copyfile(source, staged)
    staged.chmod(0o600)
    os.replace(staged, destination)
    return auth_home


def field_value(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either an SDK mapping or an SDK response object."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)
