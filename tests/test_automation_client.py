import json
from contextlib import asynccontextmanager

import anyio
import pytest

from automation.client import ClientFailure, MCPClient, MCPClientError, sdk_session
from automation.settings import AutomationSettings, ServerSettings


def settings():
    return AutomationSettings(
        enabled=True,
        servers=(
            ServerSettings(
                "home", "https://home.test/mcp", "MCP_TOKEN", ("read",), ("light.office",)
            ),
        ),
    )


class FakeSession:
    def __init__(self):
        self.pages = [{"tools": [{"name": "read", "inputSchema": {"type": "object"}}]}]
        self.version = "2025-11-25"
        self.closed = False
        self.calls = []
        self.failure = None

    async def initialize(self):
        return {"protocolVersion": self.version, "capabilities": {"tools": {}}}

    async def list_tools(self, *, params=None):
        return self.pages.pop(0)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.failure:
            raise self.failure
        return {"content": [{"type": "text", "text": "untrusted"}], "isError": False}

    @asynccontextmanager
    async def factory(self, *args):
        try:
            yield self
        finally:
            self.closed = True


def test_discovery_pagination_catalog_changes_and_call():
    fake = FakeSession()
    fake.pages = [{"tools": [], "nextCursor": "page2"}, fake.pages[0]]

    async def scenario():
        async with MCPClient(
            settings(), "home", environ={"MCP_TOKEN": "secret"}, session_factory=fake.factory
        ) as client:
            catalog = await client.discover()
            assert fake.calls == []
            result = await client.call(catalog[0], "{}")
            assert not result.is_error
            assert "untrusted" in result.payload_json
            fake.pages = [
                {
                    "tools": [
                        {"name": "read", "inputSchema": {"type": "object", "required": ["entity"]}}
                    ]
                }
            ]
            newer = await client.discover()
            assert newer[0].catalog_id != catalog[0].catalog_id
            with pytest.raises(MCPClientError):
                await client.call(catalog[0], "{}")
        assert fake.closed
        with pytest.raises(MCPClientError) as caught:
            await client.discover()
        assert caught.value.category == ClientFailure.CLOSED

    anyio.run(scenario)


@pytest.mark.parametrize("version", ["unexpected", None])
def test_protocol_mismatch_closes_session(version):
    fake = FakeSession()
    fake.version = version

    async def scenario():
        with pytest.raises(MCPClientError) as caught:
            async with MCPClient(
                settings(), "home", environ={"MCP_TOKEN": "secret"}, session_factory=fake.factory
            ):
                pass
        assert caught.value.category == ClientFailure.PROTOCOL
        assert fake.closed

    anyio.run(scenario)


@pytest.mark.parametrize(
    "page",
    [
        {"tools": "wrong"},
        {"tools": [{}]},
        {"tools": [], "nextCursor": 42},
        {"tools": [{"name": "read", "inputSchema": {"type": "object", "properties": 3}}]},
    ],
)
def test_malformed_catalog(page):
    fake = FakeSession()
    fake.pages = [page]

    async def scenario():
        async with MCPClient(
            settings(), "home", environ={"MCP_TOKEN": "secret"}, session_factory=fake.factory
        ) as client:
            with pytest.raises(MCPClientError) as caught:
                await client.discover()
            assert caught.value.category == ClientFailure.INVALID

    anyio.run(scenario)


@pytest.mark.parametrize(
    "failure,category",
    [(OSError("disconnected"), ClientFailure.TRANSPORT), (TimeoutError(), ClientFailure.TIMEOUT)],
)
def test_uncertain_calls_are_not_retried(failure, category):
    fake = FakeSession()
    fake.failure = failure

    async def scenario():
        async with MCPClient(
            settings(), "home", environ={"MCP_TOKEN": "secret"}, session_factory=fake.factory
        ) as client:
            catalog = await client.discover()
            with pytest.raises(MCPClientError) as caught:
                await client.call(catalog[0], "{}")
            assert caught.value.category == category
            assert caught.value.possible_dispatch
            assert len(fake.calls) == 1

    anyio.run(scenario)


def test_disabled_or_missing_auth_never_enters_transport():
    for configuration, env in [(AutomationSettings(), {}), (settings(), {})]:
        with pytest.raises(MCPClientError):
            MCPClient(configuration, "home", environ=env)


def test_real_sdk_with_fake_http_handshake_and_call():
    import httpx2

    calls = []
    closed = []

    async def handler(request):
        assert str(request.url).startswith("https://home.test/")
        assert request.headers["authorization"] == "Bearer secret"
        if request.method != "POST":
            return httpx2.Response(405, request=request)
        body = json.loads(request.content)
        method = body["method"]
        calls.append(method)
        if "id" not in body:
            return httpx2.Response(202, request=request)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "read", "inputSchema": {"type": "object"}}]}
        else:
            result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
        return httpx2.Response(
            200, request=request, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    @asynccontextmanager
    async def factory(server, token, timeout):
        try:
            async with sdk_session(
                server, token, timeout, http_transport=httpx2.MockTransport(handler)
            ) as session:
                yield session
        finally:
            closed.append(True)

    async def scenario():
        async with MCPClient(
            settings(), "home", environ={"MCP_TOKEN": "secret"}, session_factory=factory
        ) as client:
            (tool,) = await client.discover()
            assert not (await client.call(tool, "{}")).is_error
        assert calls.count("tools/call") == 1
        assert closed == [True]

    anyio.run(scenario)


@pytest.mark.parametrize("status", [401, 403, 307])
def test_sdk_auth_and_redirect_failure_never_contacts_second_origin(status):
    import httpx2

    requests = []

    async def handler(request):
        requests.append(str(request.url))
        return httpx2.Response(
            status, request=request, headers={"location": "https://other.test/mcp"}
        )

    @asynccontextmanager
    async def factory(server, token, timeout):
        async with sdk_session(
            server, token, timeout, http_transport=httpx2.MockTransport(handler)
        ) as session:
            yield session

    async def scenario():
        with pytest.raises(MCPClientError):
            async with MCPClient(
                settings(), "home", environ={"MCP_TOKEN": "secret"}, session_factory=factory
            ):
                pass
        assert requests == ["https://home.test/mcp"]

    anyio.run(scenario)


def test_cancelled_request_closes_without_replay():
    fake = FakeSession()

    async def slow_call(*args):
        fake.calls.append(args)
        await anyio.sleep(5)

    fake.call_tool = slow_call

    async def scenario():
        with anyio.move_on_after(0.03) as scope:
            async with MCPClient(
                settings(), "home", environ={"MCP_TOKEN": "secret"}, session_factory=fake.factory
            ) as client:
                (tool,) = await client.discover()
                await client.call(tool, "{}")
        assert scope.cancel_called
        assert fake.closed
        assert len(fake.calls) == 1

    anyio.run(scenario)


def test_actual_timeout_bounds_call_and_closes():
    from dataclasses import replace

    fake = FakeSession()

    async def slow_call(*args):
        fake.calls.append(args)
        await anyio.sleep(5)

    fake.call_tool = slow_call

    async def scenario():
        async with MCPClient(
            replace(settings(), timeout_seconds=0.01),
            "home",
            environ={"MCP_TOKEN": "secret"},
            session_factory=fake.factory,
        ) as client:
            (tool,) = await client.discover()
            with pytest.raises(MCPClientError) as caught:
                await client.call(tool, "{}")
            assert caught.value.category == ClientFailure.TIMEOUT
            assert len(fake.calls) == 1
        assert fake.closed

    anyio.run(scenario)


def test_cursor_cycle_and_result_size_limit():
    fake = FakeSession()

    async def scenario():
        async with MCPClient(
            settings(), "home", environ={"MCP_TOKEN": "secret"}, session_factory=fake.factory
        ) as client:
            fake.pages = [{"tools": [], "nextCursor": "same"}] * 2
            with pytest.raises(MCPClientError):
                await client.discover()
            fake.pages = [{"tools": [{"name": "read", "inputSchema": {"type": "object"}}]}]
            (tool,) = await client.discover()

            async def large_call(*args):
                return {"content": [{"type": "text", "text": "x" * (1024 * 1024)}]}

            fake.call_tool = large_call
            with pytest.raises(MCPClientError) as caught:
                await client.call(tool, "{}")
            assert caught.value.possible_dispatch

    anyio.run(scenario)


@pytest.mark.parametrize(
    "status,category", [(401, ClientFailure.AUTH), (503, ClientFailure.TRANSPORT)]
)
def test_sdk_http_failure_during_call_is_typed_and_never_replayed(status, category):
    import httpx2

    calls = []

    async def handler(request):
        if request.method != "POST":
            return httpx2.Response(405, request=request)
        body = json.loads(request.content)
        if "id" not in body:
            return httpx2.Response(202, request=request)
        if body["method"] == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }
        elif body["method"] == "tools/list":
            result = {"tools": [{"name": "read", "inputSchema": {"type": "object"}}]}
        else:
            calls.append(body["method"])
            return httpx2.Response(status, request=request)
        return httpx2.Response(
            200, request=request, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    @asynccontextmanager
    async def factory(server, token, timeout):
        async with sdk_session(
            server, token, timeout, http_transport=httpx2.MockTransport(handler)
        ) as session:
            yield session

    async def scenario():
        async with MCPClient(
            settings(), "home", environ={"MCP_TOKEN": "secret"}, session_factory=factory
        ) as client:
            (tool,) = await client.discover()
            with pytest.raises(MCPClientError) as caught:
                await client.call(tool, "{}")
            assert caught.value.category == category
            assert caught.value.possible_dispatch
        assert calls == ["tools/call"]

    anyio.run(scenario)


def test_genuine_cancellation_of_sdk_call_propagates_and_closes():
    import httpx2

    closed = []
    calls = []

    async def scenario():
        started = anyio.Event()

        async def handler(request):
            if request.method != "POST":
                return httpx2.Response(405, request=request)
            body = json.loads(request.content)
            if "id" not in body:
                return httpx2.Response(202, request=request)
            if body["method"] == "initialize":
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "1"},
                }
            elif body["method"] == "tools/list":
                result = {"tools": [{"name": "read", "inputSchema": {"type": "object"}}]}
            else:
                calls.append(body["method"])
                started.set()
                await anyio.sleep_forever()
            return httpx2.Response(
                200, request=request, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
            )

        @asynccontextmanager
        async def factory(server, token, timeout):
            try:
                async with sdk_session(
                    server, token, timeout, http_transport=httpx2.MockTransport(handler)
                ) as session:
                    yield session
            finally:
                closed.append(True)

        async def request():
            async with MCPClient(
                settings(), "home", environ={"MCP_TOKEN": "secret"}, session_factory=factory
            ) as client:
                (tool,) = await client.discover()
                await client.call(tool, "{}")
            pytest.fail("Cancellation was swallowed")

        async with anyio.create_task_group() as group:
            group.start_soon(request)
            await started.wait()
            group.cancel_scope.cancel()
        assert closed == [True]
        assert calls == ["tools/call"]

    anyio.run(scenario)
