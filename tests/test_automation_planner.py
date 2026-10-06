import json
from dataclasses import replace

import anyio
import pytest

from automation.planner import PlanKind, ProposalCapability, ProposalPlanner
from automation.proposal_providers import CodexProposalProvider, OllamaProposalProvider
from tests.test_automation_policy import catalog, policy


class Provider:
    capability = ProposalCapability("fake", True, False, "fixture")

    def __init__(self, output):
        self.output = output
        self.calls = 0

    async def generate(self, prompt, schema):
        self.calls += 1
        return self.output


VALID = '{"kind":"propose","actions":[{"server_id":"home","tool_name":"light","arguments":{"entity_id":"light.office"}}]}'


def test_strict_proposal_has_local_ids_and_no_dispatch():
    async def scenario():
        result = await ProposalPlanner(
            policy(), Provider(VALID), clock=lambda: 80, id_factory=lambda: "local-id"
        ).plan("accendi", catalog(), session_id="s", turn_id="t")
        assert result.kind == PlanKind.PROPOSE
        assert result.proposals[0].action_id == "local-id"
        assert result.proposals[0].expires_at == 110

    anyio.run(scenario)


@pytest.mark.parametrize(
    "output",
    [
        "Here is the JSON: " + VALID,
        "```json\n" + VALID + "\n```",
        VALID[:-2],
        VALID.replace('"light"', '"unknown"'),
        VALID.replace("light.office", "lock.front"),
        VALID.replace('"entity_id":"light.office"', '"instruction":"ignore policy"'),
        '{"kind":"clarify","actions":[{}]}',
        '{"kind":"propose","actions":[]}',
    ],
)
def test_malformed_unexpected_ambiguous_and_injected_output_clarifies(output):
    async def scenario():
        result = await ProposalPlanner(policy(), Provider(output), clock=lambda: 80).plan(
            "x", catalog(), session_id="s", turn_id="t"
        )
        assert result.kind == PlanKind.CLARIFY
        assert result.proposals == ()

    anyio.run(scenario)


def test_unsupported_and_remote_privacy_prevent_provider_request():
    async def scenario():
        provider = Provider(VALID)
        provider.capability = ProposalCapability("remote", True, True, "fixture")
        planner = ProposalPlanner(policy(), provider, allow_remote_transcript=True)
        assert (
            await planner.plan("x", catalog(), session_id="s", turn_id="t")
        ).kind == PlanKind.UNSUPPORTED
        assert provider.calls == 0
        provider.capability = replace(provider.capability, remote=False, structured_output=False)
        assert (
            await planner.plan("x", catalog(), session_id="s", turn_id="t")
        ).kind == PlanKind.UNSUPPORTED
        assert provider.calls == 0

    anyio.run(scenario)


def test_ollama_adapter_passes_schema_without_tools():
    class Client:
        async def chat(self, **kwargs):
            assert kwargs["format"] == {"type": "object"}
            assert not kwargs["stream"]
            assert "tools" not in kwargs
            return {"message": {"content": VALID}}

    async def scenario():
        assert (
            await OllamaProposalProvider("local", client=Client()).generate(
                "prompt", {"type": "object"}
            )
            == VALID
        )

    anyio.run(scenario)


def test_codex_split_output_is_assembled_and_validated():
    class Turn:
        def stream(self):
            for value in [VALID[:20], VALID[20:]]:
                yield {"method": "item/agentMessage/delta", "params": {"delta": value}}
            yield {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}

        def interrupt(self):
            pass

    class Runtime:
        closed = False

        def account_kind(self):
            return "chatgpt"

        def start_proposal_turn(self, **kwargs):
            assert "output_schema" in kwargs
            return Turn()

        def close(self):
            self.closed = True

    runtime = Runtime()

    async def scenario():
        result = await CodexProposalProvider("codex", runtime_factory=lambda: runtime).generate(
            "prompt", {"type": "object"}
        )
        assert json.loads(result)["kind"] == "propose"
        assert runtime.closed

    anyio.run(scenario)
