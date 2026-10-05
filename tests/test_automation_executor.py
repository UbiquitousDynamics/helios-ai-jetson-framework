from dataclasses import replace

import anyio

from automation.client import ToolResult
from automation.contracts import Confirmation, OutcomeStatus
from automation.executor import ActionExecutor, ReceiptLedger
from tests.test_automation_policy import action, catalog, policy


class Client:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.catalog = catalog()

    async def discover(self):
        return self.catalog

    async def call(self, *args):
        self.calls += 1
        if self.fail:
            raise OSError("response lost after successful write")
        return ToolResult(False, "{}")


def confirmed(proposal):
    return Confirmation(proposal.fingerprint, proposal.session_id, 100)


def test_success_and_replays_across_restart(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = ReceiptLedger(path)
    client = Client()
    proposal = action()

    async def scenario():
        executor = ActionExecutor(policy(), client, ledger, clock=lambda: 80)
        assert (
            await executor.execute(proposal, confirmed(proposal))
        ).status == OutcomeStatus.SUCCESS
        assert (
            await executor.execute(proposal, confirmed(proposal))
        ).status == OutcomeStatus.SUCCESS
        ledger.close()
        restarted = ReceiptLedger(path)
        assert (
            await ActionExecutor(policy(), client, restarted, clock=lambda: 81).execute(
                proposal, confirmed(proposal)
            )
        ).status == OutcomeStatus.SUCCESS
        assert client.calls == 1
        assert (
            await ActionExecutor(policy(), client, restarted, clock=lambda: 81).execute(
                replace(proposal, arguments_json='{"entity_id":"light.other"}'), confirmed(proposal)
            )
        ).status == OutcomeStatus.DENIED
        restarted.close()

    anyio.run(scenario)
    assert b"light.office" not in path.read_bytes()


def test_lost_ack_and_crash_pending_are_unknown_not_replayed(tmp_path):
    ledger = ReceiptLedger(tmp_path / "ledger")
    client = Client()
    client.fail = True
    proposal = action()

    async def scenario():
        executor = ActionExecutor(policy(), client, ledger, clock=lambda: 80)
        assert (
            await executor.execute(proposal, confirmed(proposal))
        ).status == OutcomeStatus.UNKNOWN
        client.fail = False
        assert (
            await executor.execute(proposal, confirmed(proposal))
        ).status == OutcomeStatus.UNKNOWN
        assert client.calls == 1
        pending = replace(proposal, action_id="crashed")
        ledger.reserve(pending, 80, max_calls=3)
        assert (await executor.execute(pending, confirmed(pending))).status == OutcomeStatus.UNKNOWN
        assert client.calls == 1

    anyio.run(scenario)
    ledger.close()


def test_denial_confirmation_expiry_catalog_and_cancel_have_zero_calls(tmp_path):
    ledger = ReceiptLedger(tmp_path / "ledger")
    client = Client()
    proposal = action()

    async def scenario():
        executor = ActionExecutor(policy(), client, ledger, clock=lambda: 80)
        assert (await executor.execute(proposal)).status == OutcomeStatus.DENIED
        client.catalog = ()
        assert (
            await executor.execute(proposal, confirmed(proposal))
        ).status == OutcomeStatus.DENIED
        client.catalog = catalog()
        assert (
            await ActionExecutor(policy(), client, ledger, clock=lambda: 101).execute(
                proposal, confirmed(proposal)
            )
        ).status == OutcomeStatus.DENIED
        assert (
            await ActionExecutor(
                policy(), client, ledger, clock=lambda: 80, cancelled=lambda: True
            ).execute(proposal, confirmed(proposal))
        ).status == OutcomeStatus.DENIED
        assert client.calls == 0

    anyio.run(scenario)
    ledger.close()


def test_cancel_after_dispatch_leaves_unknown(tmp_path):
    ledger = ReceiptLedger(tmp_path / "ledger")
    client = Client()

    async def slow(*args):
        client.calls += 1
        await anyio.sleep(5)

    client.call = slow
    proposal = action()

    async def scenario():
        with anyio.move_on_after(0.02):
            await ActionExecutor(policy(), client, ledger, clock=lambda: 80).execute(
                proposal, confirmed(proposal)
            )
        assert ledger.lookup(proposal).status == OutcomeStatus.UNKNOWN
        assert client.calls == 1

    anyio.run(scenario)
    ledger.close()


def test_capacity_and_turn_limits_fail_closed(tmp_path):
    ledger = ReceiptLedger(tmp_path / "ledger", max_records=1)
    client = Client()
    proposal = action()

    async def scenario():
        executor = ActionExecutor(policy(), client, ledger, clock=lambda: 80)
        await executor.execute(proposal, confirmed(proposal))
        other = replace(proposal, action_id="other")
        assert (await executor.execute(other, confirmed(other))).reason_code == "dispatch_limit"
        assert client.calls == 1

    anyio.run(scenario)
    ledger.close()
