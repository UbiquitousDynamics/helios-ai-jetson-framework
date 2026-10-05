"""Versioned content-free events on the existing bounded metrics infrastructure."""

from __future__ import annotations

import hashlib
from enum import Enum

from api.metrics import record_safely
from automation.contracts import ActionProposal


class AuditPhase(str, Enum):
    PROPOSAL = "proposal"
    POLICY = "policy"
    CONFIRMATION = "confirmation"
    DISPATCH = "dispatch"
    OUTCOME = "outcome"
    RECONCILIATION = "reconciliation"
    REPLAY = "replay_suppressed"
    CAPABILITY = "capability"


class AuditOutcome(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    UNSUPPORTED = "unsupported"
    AUTH_DENIED = "auth_denied"
    SCOPE_DENIED = "scope_denied"
    TRANSPORT_FAILED = "transport_failed"


def opaque_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Expected identifier")
    return hashlib.sha256(value.encode()).hexdigest()


class AutomationAudit:
    def __init__(self, recorder=None):
        self.recorder = recorder

    def emit(
        self,
        phase: AuditPhase,
        outcome: AuditOutcome,
        *,
        proposal: ActionProposal | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        action_id: str | None = None,
        provider_id: str | None = None,
        latency_ms: float | None = None,
    ):
        if not isinstance(phase, AuditPhase) or not isinstance(outcome, AuditOutcome):
            raise ValueError("Audit phase/outcome must be closed enums")
        if proposal is not None:
            session_id, turn_id, action_id = (
                proposal.session_id,
                proposal.turn_id,
                proposal.action_id,
            )
        return record_safely(
            self.recorder,
            "automation_v1_" + phase.value,
            outcome=outcome.value,
            resource_scope=opaque_id(session_id),
            attempt_id=opaque_id(turn_id),
            request_id=opaque_id(action_id),
            provider=opaque_id(provider_id),
            latency_ms=latency_ms,
        )
