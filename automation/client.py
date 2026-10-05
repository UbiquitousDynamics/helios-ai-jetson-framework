"""Bounded MCP transport; policy and dispatch ownership belong to the executor."""

from __future__ import annotations

import hashlib
import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator, Callable, Mapping, Protocol

import anyio

from automation.contracts import ToolDescriptor, object_json
from automation.settings import AutomationSettings, ServerSettings

MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_CATALOG_PAGES = 20
MAX_CATALOG_TOOLS = 1000


class ClientFailure(str, Enum):
    AUTH = "authentication"
    PROTOCOL = "protocol"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    INVALID = "invalid_payload"
    CLOSED = "closed"


class MCPClientError(RuntimeError):
    def __init__(self, category: ClientFailure, *, possible_dispatch: bool = False):
        super().__init__(category.value)
        self.category = category
        self.possible_dispatch = possible_dispatch


def failure_category(exc: Exception) -> ClientFailure:
    if isinstance(exc, MCPClientError):
        return exc.category
    nested = getattr(exc, "exceptions", ())
    for item in nested:
        category = failure_category(item)
        if category != ClientFailure.TRANSPORT:
            return category
    if isinstance(exc, TimeoutError):
        return ClientFailure.TIMEOUT
    return ClientFailure.TRANSPORT


class Session(Protocol):
    async def initialize(self) -> Any: ...
    async def list_tools(self, *, params: Any = None) -> Any: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


def payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(by_alias=True, exclude_none=True)
    try:
        encoded = json.dumps(value, allow_nan=False)
        if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError("Payload exceeds limit")
        return (
            json.loads(object_json(encoded))
            if len(encoded.encode()) <= 65536
            else _large_object(encoded)
        )
    except (ValueError, TypeError, RecursionError):
        raise MCPClientError(ClientFailure.INVALID) from None


def _large_object(encoded: str) -> dict[str, Any]:
    value = json.loads(encoded)
    if not isinstance(value, dict):
        raise ValueError("Expected object")
    return value


@asynccontextmanager
async def sdk_session(
    server: ServerSettings, token: str, timeout: float, *, http_transport: Any = None
) -> AsyncIterator[Session]:
    """SDK handshake path only; no OAuth, roots, sampling or elicitation callbacks."""
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    origin = (
        httpx2.URL(server.endpoint).scheme,
        httpx2.URL(server.endpoint).host,
        httpx2.URL(server.endpoint).port,
    )

    class LimitedStream(httpx2.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream

        async def __aiter__(self):
            total = 0
            async for chunk in self.stream:
                total += len(chunk)
                if total > MAX_PAYLOAD_BYTES:
                    raise MCPClientError(ClientFailure.INVALID)
                yield chunk

        async def aclose(self):
            await self.stream.aclose()

    class GuardedTransport(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.inner = http_transport or httpx2.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, request):
            if (request.url.scheme, request.url.host, request.url.port) != origin:
                raise MCPClientError(ClientFailure.AUTH)
            response = await self.inner.handle_async_request(request)
            if response.status_code in (401, 403):
                await response.aclose()
                raise MCPClientError(ClientFailure.AUTH)
            # Reject redirects entirely, including SDK-controlled redirect following.
            if 300 <= response.status_code < 400:
                await response.aclose()
                raise MCPClientError(ClientFailure.PROTOCOL)
            response.stream = LimitedStream(response.stream)
            return response

        async def aclose(self):
            await self.inner.aclose()

    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        transport=GuardedTransport(),
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as http:
        async with streamable_http_client(
            server.endpoint, http_client=http, max_sse_event_size=MAX_PAYLOAD_BYTES
        ) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timeout) as session:
                yield session


@dataclass(frozen=True, slots=True)
class ToolResult:
    is_error: bool
    payload_json: str


class MCPClient:
    """Session lifetime and catalog are explicit; never retries a tools/call."""

    def __init__(
        self,
        settings: AutomationSettings,
        server_id: str,
        *,
        environ: Mapping[str, str],
        session_factory: Callable = sdk_session,
    ):
        if not settings.enabled:
            raise MCPClientError(ClientFailure.CLOSED)
        self.server = next(
            (server for server in settings.servers if server.server_id == server_id), None
        )
        if self.server is None:
            raise MCPClientError(ClientFailure.AUTH)
        self._token = environ.get(self.server.credential_env, "").strip()
        if not self._token or any(char in self._token for char in "\r\n"):
            raise MCPClientError(ClientFailure.AUTH)
        self.timeout = settings.timeout_seconds
        self._factory = session_factory
        self._context = None
        self._session = None
        self._catalog: tuple[ToolDescriptor, ...] = ()
        self._inflight_write = False

    async def _request(self, method: str, *args, **kwargs):
        if self._session is None:
            raise MCPClientError(ClientFailure.CLOSED)
        try:
            self._inflight_write = method == "call_tool"
            with anyio.fail_after(self.timeout):
                result = payload(await getattr(self._session, method)(*args, **kwargs))
            self._inflight_write = False
            return result
        except MCPClientError as exc:
            raise MCPClientError(exc.category, possible_dispatch=method == "call_tool") from None
        except TimeoutError:
            raise MCPClientError(
                ClientFailure.TIMEOUT, possible_dispatch=method == "call_tool"
            ) from None
        except Exception:
            raise MCPClientError(
                ClientFailure.TRANSPORT, possible_dispatch=method == "call_tool"
            ) from None

    async def __aenter__(self):
        if self._context is not None:
            raise MCPClientError(ClientFailure.PROTOCOL)
        self._context = self._factory(self.server, self._token, self.timeout)
        try:
            self._session = await self._context.__aenter__()
            initialized = await self._request("initialize")
            from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

            if initialized.get("protocolVersion") not in HANDSHAKE_PROTOCOL_VERSIONS:
                raise MCPClientError(ClientFailure.PROTOCOL)
            if not isinstance(initialized.get("capabilities", {}).get("tools"), dict):
                raise MCPClientError(ClientFailure.PROTOCOL)
            return self
        except (Exception, asyncio.CancelledError):
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type, exc, tb):
        context, self._context = self._context, None
        self._session = None
        self._catalog = ()
        if context is not None:
            try:
                await context.__aexit__(exc_type, exc, tb)
            except Exception as failure:
                raise MCPClientError(
                    failure_category(failure), possible_dispatch=self._inflight_write
                ) from None

    async def discover(self) -> tuple[ToolDescriptor, ...]:
        from mcp_types import PaginatedRequestParams

        self._catalog = ()
        entries = []
        cursor = None
        seen = set()
        for _ in range(MAX_CATALOG_PAGES):
            page = await self._request(
                "list_tools", params=PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            tools = page.get("tools")
            if not isinstance(tools, list):
                raise MCPClientError(ClientFailure.INVALID)
            entries.extend(tools)
            if (
                len(entries) > MAX_CATALOG_TOOLS
                or len(json.dumps(entries).encode()) > MAX_PAYLOAD_BYTES
            ):
                raise MCPClientError(ClientFailure.INVALID)
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise MCPClientError(ClientFailure.INVALID)
            seen.add(cursor)
        else:
            raise MCPClientError(ClientFailure.INVALID)
        try:
            catalog_id = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
            catalog = tuple(
                ToolDescriptor(
                    self.server.server_id,
                    entry["name"],
                    catalog_id,
                    json.dumps(entry["inputSchema"]),
                )
                for entry in entries
            )
            if len({tool.name for tool in catalog}) != len(catalog):
                raise ValueError("Duplicate tool")
        except (KeyError, TypeError, ValueError):
            raise MCPClientError(ClientFailure.INVALID) from None
        self._catalog = catalog
        return catalog

    async def call(self, tool: ToolDescriptor, arguments_json: str) -> ToolResult:
        from jsonschema import Draft202012Validator
        from referencing import Registry

        if tool not in self._catalog or tool.server_id != self.server.server_id:
            raise MCPClientError(ClientFailure.PROTOCOL)
        try:
            arguments = json.loads(object_json(arguments_json))
            # Empty registry never retrieves schemas from the network.
            Draft202012Validator(json.loads(tool.input_schema_json), registry=Registry()).validate(
                arguments
            )
        except Exception:
            raise MCPClientError(ClientFailure.INVALID) from None
        result = await self._request("call_tool", tool.name, arguments)
        from mcp_types import CallToolResult

        try:
            CallToolResult.model_validate(result)
        except Exception:
            raise MCPClientError(ClientFailure.INVALID, possible_dispatch=True) from None
        if type(result.get("isError", False)) is not bool or not isinstance(
            result.get("content"), list
        ):
            raise MCPClientError(ClientFailure.INVALID, possible_dispatch=True)
        return ToolResult(result.get("isError", False), json.dumps(result, sort_keys=True))
