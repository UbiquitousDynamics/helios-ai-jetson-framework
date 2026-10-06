"""Explicit opt-in, non-mutating Home Assistant/MCP diagnostic command."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import anyio

from automation.client import MCPClient
from automation.home_assistant import catalog_report, read_version
from automation.settings import load_automation_settings


def credential_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
        raise ValueError("Invalid credential file")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("Credential file must be private")
    token = path.read_text(encoding="utf-8").strip()
    if not token or "\n" in token or "\r" in token:
        raise ValueError("Invalid credential file")
    return token


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--server", default="homeassistant")
    parser.add_argument("--expected-version")
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--credential-env", default="HELIOS_HA_TOKEN")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    try:
        if args.credential_file is not None:
            env[args.credential_env] = credential_file(args.credential_file.expanduser())
        settings = load_automation_settings(args.config, environ=env)
        if not settings.enabled:
            print(json.dumps({"status": "disabled", "connected": False}))
            return 2
        server = next(item for item in settings.servers if item.server_id == args.server)

        async def diagnose():
            version = await read_version(
                server, env[server.credential_env], timeout=settings.timeout_seconds
            )
            async with MCPClient(settings, args.server, environ=env) as client:
                catalog = await client.discover()
            return catalog_report(version, server, catalog, expected_version=args.expected_version)

        report = anyio.run(diagnose)
        print(json.dumps(asdict(report), sort_keys=True))
        return 0 if report.status in {"discovery_only", "schema_checked"} else 2
    except Exception:
        # Never include exception text, token, URLs, raw catalog or home config.
        print(
            json.dumps({"status": "failed", "reason_code": "configuration_auth_or_compatibility"})
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
