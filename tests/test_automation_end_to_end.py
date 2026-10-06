"""Whole fake stack: SDK transport, planner, local consent and durable execution."""

import json
from contextlib import asynccontextmanager

import anyio
import httpx2
import pytest

from automation.audit import AutomationAudit
from automation.client import MCPClient, sdk_session
from automation.contracts import OutcomeStatus
from automation.executor import ActionExecutor, ReceiptLedger
from automation.planner import ProposalCapability, ProposalPlanner
from automation.policy import ActionKind, LocalPolicy, ToolPolicy
from automation.settings import AutomationSettings, ServerSettings
from automation.voice import VoiceActionController
from recognizer.speech_recognizer import RecognitionResult


@pytest.mark.parametrize("lose_response", [False, True])
def test_confirmed_fake_device_action_and_response_loss_are_not_replayed(tmp_path, lose_response):
    physical_changes = []
    speech = []
    schema = {
        "type": "object",
        "properties": {"entity_id": {"type": "string"}},
        "required": ["entity_id"],
        "additionalProperties": False,
    }
    settings = AutomationSettings(
        enabled=True,
        servers=(
            ServerSettings(
                "fixture",
                "https://fixture.test/mcp",
                "FIXTURE_TOKEN",
                ("fixture_light_on",),
                ("light.generated_test",),
            ),
        ),
    )
    policy = LocalPolicy(settings, (ToolPolicy("fixture", "fixture_light_on", ActionKind.LIGHT),))

    async def handler(request):
        assert str(request.url) == "https://fixture.test/mcp"
        if request.method != "POST":
            return httpx2.Response(405, request=request)
        message = json.loads(request.content)
        if "id" not in message:
            return httpx2.Response(202, request=request)
        method = message["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "generated-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "fixture_light_on", "inputSchema": schema}]}
        else:
            assert method == "tools/call"
            assert message["params"]["arguments"] == {"entity_id": "light.generated_test"}
            physical_changes.append("on")
            if lose_response:
                # The fake device acts, but the terminal response cannot confirm
                # completion. No transcript or real HA request is involved.
                return httpx2.Response(
                    200,
                    request=request,
                    json={
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32000, "message": "generated lost-response fixture"},
                    },
                )
            result = {"content": [{"type": "text", "text": "generated result"}], "isError": False}
        return httpx2.Response(
            200, request=request, json={"jsonrpc": "2.0", "id": message["id"], "result": result}
        )

    @asynccontextmanager
    async def factory(server, token, timeout):
        async with sdk_session(
            server, token, timeout, http_transport=httpx2.MockTransport(handler)
        ) as session:
            yield session

    class Provider:
        capability = ProposalCapability("generated-fixture", True, False, "model-free")

        async def generate(self, prompt, output_schema):
            return json.dumps(
                {
                    "kind": "propose",
                    "actions": [
                        {
                            "server_id": "fixture",
                            "tool_name": "fixture_light_on",
                            "arguments": {"entity_id": "light.generated_test"},
                        }
                    ],
                }
            )

    class Verifier:
        calibration_id = "test-only-generated-fixture"

        def accepts(self, result, *, after):
            return True

    ledger_path = tmp_path / "receipts.sqlite"
    ledger = ReceiptLedger(ledger_path)

    async def scenario():
        async with MCPClient(
            settings,
            "fixture",
            environ={"FIXTURE_TOKEN": "generated-token"},
            session_factory=factory,
        ) as client:
            executor = ActionExecutor(
                policy, client, ledger, clock=lambda: 100, audit=AutomationAudit()
            )
            voice = VoiceActionController(
                ProposalPlanner(policy, Provider(), clock=lambda: 100),
                discover=client.discover,
                executor=executor,
                speak=speech.append,
                describe=lambda proposal: "accendere la luce di prova (light.generated_test)",
                verifier=Verifier(),
                clock=lambda: 100,
                capture_clock=lambda: 20,
            )
            await voice.handle(
                "domotica accendi la luce di prova",
                RecognitionResult(
                    "domotica accendi la luce di prova", True, capture_id=1, segment_started_at=10
                ),
                session_id="generated-session",
                turn_id="generated-turn",
            )
            proposal = voice.pending.proposal
            result = await voice.handle(
                "confermo",
                RecognitionResult("confermo", True, capture_id=2, segment_started_at=21),
                session_id="generated-session",
                turn_id="confirmation-turn",
            )
            assert result.status == (
                OutcomeStatus.UNKNOWN if lose_response else OutcomeStatus.SUCCESS
            )
            # Simulate fallback/reconnect trying to execute the accepted action.
            assert (await executor.execute(proposal)).status == result.status
            assert physical_changes == ["on"]
            if lose_response:
                assert "Non posso confermare" in speech[-1]
        ledger.close()
        restarted = ReceiptLedger(ledger_path)
        assert restarted.lookup(proposal).status == result.status
        restarted.close()

    anyio.run(scenario)
