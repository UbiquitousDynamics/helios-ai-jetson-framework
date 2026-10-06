"""Loopback MCP bridge exposing only explicitly selected light states."""

from __future__ import annotations

import hmac
import json
import os
import re
from urllib.parse import urlsplit

import httpx

STATES = frozenset({"on", "off", "unknown", "unavailable"})


class StateReader:
    def __init__(self, base_url, token, entities, *, transport=None):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            )
            or not token.strip()
            or not entities
            or len(entities) > 20
            or len(set(entities)) != len(entities)
            or any(not re.fullmatch(r"light\.[a-z0-9_]+", entity) for entity in entities)
        ):
            raise ValueError("Invalid scoped state bridge configuration")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.entities = tuple(entities)
        self.transport = transport

    async def read(self, entity_id):
        if entity_id not in self.entities:
            raise ValueError("Entity outside read scope")
        try:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, trust_env=False, transport=self.transport
            ) as client:
                async with client.stream(
                    "GET",
                    self.base_url + "/api/states/" + entity_id,
                    headers={"Authorization": "Bearer " + self.token},
                ) as response:
                    if response.status_code != 200:
                        raise ValueError("State read unavailable")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 65536:
                            raise ValueError("State response too large")
                    value = json.loads(data)
                    if value.get("entity_id") != entity_id or value.get("state") not in STATES:
                        raise ValueError("Invalid scoped state response")
                    return {"entity_id": entity_id, "state": value["state"]}
        except Exception:
            # Upstream errors may contain headers, bodies or private entity attributes.
            raise ValueError("State read unavailable") from None


class BearerGate:
    def __init__(self, app, token):
        if not token or not token.isascii() or len(token) < 32:
            raise ValueError("Invalid bridge credential")
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            values = [value for key, value in scope.get("headers", []) if key == b"authorization"]
            if len(values) != 1 or not hmac.compare_digest(values[0], b"Bearer " + self.token):
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


def create_app(reader, bridge_token):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("helios-scoped-state", log_level="WARNING")

    @server.tool(name="GetEntityState", structured_output=False)
    async def read_entity(entity_id: str) -> str:
        """Read the state of an explicitly configured light. Performs no writes."""
        return json.dumps(await reader.read(entity_id))

    app = server.streamable_http_app(
        stateless_http=True, json_response=True, max_request_body_size=65536
    )
    return BearerGate(app, bridge_token)


def main():
    import uvicorn

    entities = json.loads(os.environ["HELIOS_HA_READ_ENTITIES"])
    if not isinstance(entities, list) or any(not isinstance(value, str) for value in entities):
        raise ValueError("Invalid entity allowlist")
    reader = StateReader(
        os.environ.get("HELIOS_HA_BASE_URL", "http://127.0.0.1:8123"),
        os.environ["HELIOS_HA_TOKEN"],
        entities,
    )
    app = create_app(reader, os.environ["HELIOS_HA_BRIDGE_TOKEN"])
    uvicorn.run(app, host="127.0.0.1", port=8124, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
