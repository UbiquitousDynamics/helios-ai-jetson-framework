import json

import anyio
import pytest

from api.metrics import SafeMetricsRecorder
from automation.audit import AuditOutcome, AuditPhase, AutomationAudit, opaque_id
from automation.executor import ActionExecutor, ReceiptLedger
from tests.test_automation_executor import Client, confirmed
from tests.test_automation_policy import action, policy


def test_closed_versioned_schema_contains_no_content_or_identifiers():
    recorder = SafeMetricsRecorder(retain=2)
    audit = AutomationAudit(recorder)
    proposal = action(
        session_id="private-session", turn_id="private-turn", action_id="private-action"
    )
    audit.emit(
        AuditPhase.PROPOSAL, AuditOutcome.PENDING, proposal=proposal, provider_id="private-provider"
    )
    payload = recorder.snapshot()[0].as_dict()
    assert payload["event"] == "automation_v1_proposal"
    assert payload["request_id"] == opaque_id("private-action")
    assert payload["attempt_id"] == opaque_id("private-turn")
    encoded = json.dumps(payload)
    for private in (
        "private-session",
        "private-turn",
        "private-action",
        "private-provider",
        "light.office",
        "entity_id",
    ):
        assert private not in encoded
    with pytest.raises(TypeError):
        audit.emit(AuditPhase.OUTCOME, AuditOutcome.FAILED, token="secret")
    with pytest.raises(ValueError):
        audit.emit("ignore policy", "secret")
    recorder.close()


def test_retention_and_cancellation_are_distinct_from_transport_failure():
    recorder = SafeMetricsRecorder(retain=2)
    audit = AutomationAudit(recorder)
    for outcome in (AuditOutcome.PENDING, AuditOutcome.CANCELLED, AuditOutcome.TRANSPORT_FAILED):
        audit.emit(AuditPhase.OUTCOME, outcome)
    assert [event.outcome for event in recorder.snapshot()] == ["cancelled", "transport_failed"]
    recorder.close()


def test_executor_audit_is_separate_from_receipts_and_redacts_errors(tmp_path):
    recorder = SafeMetricsRecorder(retain=20)
    ledger = ReceiptLedger(tmp_path / "ledger")
    client = Client()
    client.fail = True
    proposal = action()

    async def scenario():
        executor = ActionExecutor(
            policy(), client, ledger, clock=lambda: 80, audit=AutomationAudit(recorder)
        )
        await executor.execute(proposal, confirmed(proposal))
        await executor.execute(proposal, confirmed(proposal))
        assert client.calls == 1

    anyio.run(scenario)
    events = [event.as_dict() for event in recorder.snapshot()]
    assert any(event["event"] == "automation_v1_replay_suppressed" for event in events)
    assert any(event["outcome"] == "unknown" for event in events)
    assert "response lost" not in json.dumps(events)
    assert "light.office" not in json.dumps(events)
    ledger.close()
    recorder.close()
