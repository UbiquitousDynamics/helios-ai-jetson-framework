"""Explicit opt-in configuration; credentials are references, never stored values."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


class AutomationConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ServerSettings:
    server_id: str
    endpoint: str
    credential_env: str
    tools: tuple[str, ...]
    entities: tuple[str, ...]

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if (
            not self.server_id.strip()
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query
        ):
            raise AutomationConfigError("Invalid MCP server identity or endpoint")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise AutomationConfigError("Non-loopback MCP endpoints require HTTPS")
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", self.credential_env):
            raise AutomationConfigError("Invalid credential environment reference")
        for scope in (self.tools, self.entities):
            if not isinstance(scope, tuple) or any(
                not isinstance(item, str) or not item.strip() or "*" in item for item in scope
            ):
                raise AutomationConfigError("Scopes require explicit names")


@dataclass(frozen=True, slots=True)
class AutomationSettings:
    enabled: bool = False
    servers: tuple[ServerSettings, ...] = ()
    timeout_seconds: float = 10.0
    max_calls_per_turn: int = 3
    proposal_ttl_seconds: float = 30.0
    allow_remote_context: bool = False

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.allow_remote_context) is not bool:
            raise AutomationConfigError("Automation flags must be booleans")
        for value in (self.timeout_seconds, self.proposal_ttl_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 300:
                raise AutomationConfigError("Automation time limits must be in (0, 300]")
        if type(self.max_calls_per_turn) is not int or not 1 <= self.max_calls_per_turn <= 10:
            raise AutomationConfigError("Call limit must be in [1, 10]")
        if not isinstance(self.servers, tuple) or any(
            not isinstance(server, ServerSettings) for server in self.servers
        ):
            raise AutomationConfigError("Invalid MCP servers")
        ids = [server.server_id for server in self.servers]
        if len(ids) != len(set(ids)) or (self.enabled and not ids):
            raise AutomationConfigError("Enabled automation requires distinct servers")


def load_automation_settings(
    path: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> AutomationSettings:
    if path is None:
        return AutomationSettings()
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        with Path(path).open("rb") as handle:
            values = tomllib.load(handle)
        raw_servers = values.pop("servers", [])
        servers = []
        for raw in raw_servers:
            server = dict(raw)
            for field in ("tools", "entities"):
                scope = server.get(field, [])
                if not isinstance(scope, list):
                    raise AutomationConfigError("Scopes must be arrays")
                server[field] = tuple(scope)
            servers.append(ServerSettings(**server))
        settings = AutomationSettings(servers=tuple(servers), **values)
        env = os.environ if environ is None else environ
        if settings.enabled and any(
            not env.get(server.credential_env, "").strip() for server in servers
        ):
            raise AutomationConfigError("Enabled MCP server credential is missing")
        return settings
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise AutomationConfigError("Invalid automation configuration") from exc
