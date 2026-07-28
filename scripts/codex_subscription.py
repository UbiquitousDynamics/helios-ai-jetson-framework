"""Manage the ChatGPT account used by Helios' Codex app-server provider."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.providers.codex_app_server import (  # noqa: E402
    _DISABLED_CODEX_FEATURES,
    _codex_child_env,
    _copy_chatgpt_auth,
)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


@contextmanager
def _client(*, persist_auth: bool = False) -> Iterator[Any]:
    try:
        from openai_codex import Codex, CodexConfig
    except ImportError:
        raise RuntimeError(
            "openai-codex is missing; install requirements-remote.txt first"
        ) from None

    source_home = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    with tempfile.TemporaryDirectory(prefix="helios-codex-admin-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        isolated_home = root / "codex-home"
        _copy_chatgpt_auth(source_home, isolated_home)
        config = CodexConfig(
            cwd=str(workspace),
            env=_codex_child_env(isolated_home),
            config_overrides=_DISABLED_CODEX_FEATURES,
            client_name="helios_admin",
            client_title="Helios Codex Account Setup",
        )
        with Codex(config) as client:
            yield client
        if persist_auth:
            isolated_auth = isolated_home / "auth.json"
            if not isolated_auth.is_file() or isolated_auth.is_symlink():
                raise RuntimeError("Codex sign-in did not produce an auth profile")
            source_home.mkdir(mode=0o700, parents=True, exist_ok=True)
            staged = source_home / "auth.json.helios.tmp"
            shutil.copyfile(isolated_auth, staged)
            staged.chmod(0o600)
            os.replace(staged, source_home / "auth.json")


def _account_root(response: Any) -> Any:
    account = _field(response, "account")
    return _field(account, "root", account) if account is not None else None


def status() -> int:
    with _client() as client:
        root = _account_root(client.account(refresh_token=False))
    if root is None:
        print("Codex account: not signed in")
        return 1
    kind = _field(root, "type", "unknown")
    plan = _field(root, "plan_type", _field(root, "planType"))
    print(f"Codex account type: {kind}")
    if plan is not None:
        print(f"ChatGPT plan: {getattr(plan, 'value', plan)}")
    if kind != "chatgpt":
        print("Helios will reject this account and use the local fallback.")
        return 2
    print("Helios subscription routing: ready")
    return 0


def login() -> int:
    with _client(persist_auth=True) as client:
        handle = client.login_chatgpt_device_code()
        print(f"Open: {handle.verification_url}")
        print(f"Code: {handle.user_code}")
        print("Waiting for ChatGPT sign-in...")
        handle.wait()
    print("ChatGPT sign-in completed.")
    return status()


def models() -> int:
    with _client() as client:
        root = _account_root(client.account(refresh_token=False))
        if _field(root, "type") != "chatgpt":
            print("A ChatGPT Codex sign-in is required.", file=sys.stderr)
            return 2
        response = client.models(include_hidden=False)
    items = _field(response, "data", ())
    ids = sorted(
        identifier
        for item in items
        if isinstance((identifier := _field(item, "id")), str)
    )
    if not ids:
        print("No Codex models were returned for this account.", file=sys.stderr)
        return 1
    print("\n".join(ids))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manage the ChatGPT Codex session used by Helios."
    )
    parser.add_argument("command", choices=("login", "status", "models"))
    command = parser.parse_args().command
    try:
        return {"login": login, "status": status, "models": models}[command]()
    except Exception as exc:
        print(f"Codex setup failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
