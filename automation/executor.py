"""Durable reservation precedes dispatch; uncertain delivery is never replayed."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Callable, Protocol

import anyio

from automation.audit import AuditOutcome, AuditPhase, AutomationAudit
from automation.contracts import (
    ActionOutcome,
    ActionProposal,
    Confirmation,
    OutcomeStatus,
    ToolDescriptor,
)
from automation.policy import LocalPolicy


def identifier(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def ledger_operation(method):
    """Serialize one connection across primary and conversation workers."""

    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


class DispatchClient(Protocol):
    async def discover(self) -> tuple[ToolDescriptor, ...]: ...
    async def call(self, tool: ToolDescriptor, arguments_json: str): ...


class ReceiptLedger:
    """Bounded SQLite tombstones, with only hashed IDs and fingerprints on disk."""

    def __init__(self, path: Path, *, retention_seconds: float = 86400, max_records: int = 10000):
        if (
            not 300 <= retention_seconds <= 604800
            or type(max_records) is not int
            or max_records < 1
        ):
            raise ValueError("Invalid dispatch-ledger limits")
        self.retention = retention_seconds
        self.max_records = max_records
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(path, timeout=2, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS actions (action_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
            "session_key TEXT NOT NULL, turn_key TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, "
            "dispatched_at REAL NOT NULL, expires_at REAL NOT NULL)"
        )
        self.connection.commit()

    @ledger_operation
    def close(self):
        self.connection.close()

    @ledger_operation
    def lookup(self, proposal: ActionProposal) -> ActionOutcome | None:
        row = self.connection.execute(
            "SELECT fingerprint,status,reason FROM actions WHERE action_key=?",
            (identifier(proposal.action_id),),
        ).fetchone()
        if row is None:
            return None
        if row[0] != proposal.fingerprint:
            return ActionOutcome(proposal.action_id, OutcomeStatus.DENIED, "action_id_conflict")
        return ActionOutcome(proposal.action_id, OutcomeStatus(row[1]), row[2])

    @ledger_operation
    def reserve(
        self, proposal: ActionProposal, now: float, *, max_calls: int
    ) -> ActionOutcome | None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.lookup(proposal)
            if existing is not None:
                self.connection.rollback()
                return existing
            self.connection.execute(
                "DELETE FROM actions WHERE dispatched_at < ? AND expires_at < ?",
                (now - self.retention, now),
            )
            count = self.connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
            turn_count = self.connection.execute(
                "SELECT COUNT(*) FROM actions WHERE session_key=? AND turn_key=?",
                (identifier(proposal.session_id), identifier(proposal.turn_id)),
            ).fetchone()[0]
            if count >= self.max_records or turn_count >= max_calls:
                self.connection.rollback()
                return ActionOutcome(proposal.action_id, OutcomeStatus.DENIED, "dispatch_limit")
            latest = self.connection.execute("SELECT MAX(dispatched_at) FROM actions").fetchone()[0]
            if latest is not None and now < latest:
                self.connection.rollback()
                return ActionOutcome(proposal.action_id, OutcomeStatus.DENIED, "clock_rollback")
            self.connection.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?)",
                (
                    identifier(proposal.action_id),
                    proposal.fingerprint,
                    identifier(proposal.session_id),
                    identifier(proposal.turn_id),
                    OutcomeStatus.UNKNOWN.value,
                    "dispatch_pending",
                    now,
                    proposal.expires_at,
                ),
            )
            self.connection.commit()
            return None
        except Exception:
            self.connection.rollback()
            raise

    @ledger_operation
    def complete(self, proposal: ActionProposal, outcome: ActionOutcome):
        try:
            self.connection.execute(
                "UPDATE actions SET status=?,reason=? WHERE action_key=? AND fingerprint=?",
                (
                    outcome.status.value,
                    outcome.reason_code,
                    identifier(proposal.action_id),
                    proposal.fingerprint,
                ),
            )
            self.connection.commit()
        except Exception:
            # Do not expose a transaction-local success to duplicate lookup when
            # its durable commit failed; retain the pending unknown reservation.
            self.connection.rollback()
            raise


class ActionExecutor:
    def __init__(
        self,
        policy: LocalPolicy,
        client: DispatchClient,
        ledger: ReceiptLedger,
        *,
        clock: Callable[[], float] = time.time,
        cancelled: Callable[[], bool] = lambda: False,
        audit: AutomationAudit | None = None,
    ):
        self.policy = policy
        self.client = client
        self.ledger = ledger
        self.clock = clock
        self.cancelled = cancelled
        self.audit = audit or AutomationAudit()
        self._lock = anyio.Lock()

    async def execute(
        self,
        proposal: ActionProposal,
        confirmation: Confirmation | None = None,
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> ActionOutcome:
        def denied(reason):
            self.audit.emit(
                AuditPhase.POLICY,
                AuditOutcome.CANCELLED
                if reason == "cancelled_before_dispatch"
                else AuditOutcome.DENIED,
                proposal=proposal,
            )
            return ActionOutcome(proposal.action_id, OutcomeStatus.DENIED, reason)

        async with self._lock:
            if self.cancelled() or cancelled():
                return denied("cancelled_before_dispatch")
            previous = self.ledger.lookup(proposal)
            if previous is not None:
                self.audit.emit(
                    AuditPhase.REPLAY,
                    AuditOutcome.UNKNOWN
                    if previous.status == OutcomeStatus.UNKNOWN
                    else AuditOutcome.DENIED,
                    proposal=proposal,
                )
                return previous
            try:
                catalog = await self.client.discover()
            except Exception:
                return ActionOutcome(proposal.action_id, OutcomeStatus.FAILED, "discovery_failed")
            now = self.clock()
            decision = self.policy.authorize(proposal, now, catalog=catalog)
            if not decision.allowed:
                return denied(decision.reason_code)
            self.audit.emit(AuditPhase.POLICY, AuditOutcome.APPROVED, proposal=proposal)
            if proposal.expires_at > now + self.policy.settings.proposal_ttl_seconds:
                return denied("proposal_lifetime")
            if self.policy.requires_confirmation(proposal) and (
                confirmation is None or not confirmation.matches(proposal, now)
            ):
                return denied("confirmation_required")
            await anyio.lowlevel.checkpoint()
            now = self.clock()
            if self.cancelled() or cancelled():
                return denied("cancelled_before_dispatch")
            # Revalidate expiry after the cancellation checkpoint and before the
            # synchronous durable boundary. No await occurs until tools/call.
            if not self.policy.authorize(proposal, now, catalog=catalog).allowed:
                return denied("expired_before_dispatch")
            if self.policy.requires_confirmation(proposal) and not confirmation.matches(
                proposal, now
            ):
                return denied("confirmation_expired")
            try:
                previous = self.ledger.reserve(
                    proposal, now, max_calls=self.policy.settings.max_calls_per_turn
                )
            except (sqlite3.Error, OSError):
                return denied("ledger_unavailable")
            if previous is not None:
                self.audit.emit(
                    AuditPhase.REPLAY,
                    AuditOutcome.UNKNOWN
                    if previous.status == OutcomeStatus.UNKNOWN
                    else AuditOutcome.DENIED,
                    proposal=proposal,
                )
                return previous
            descriptor = next(
                item
                for item in catalog
                if item.server_id == proposal.server_id and item.name == proposal.tool_name
            )
            try:
                started_at = time.monotonic()
                self.audit.emit(AuditPhase.DISPATCH, AuditOutcome.PENDING, proposal=proposal)
                result = await self.client.call(descriptor, proposal.arguments_json)
                outcome = ActionOutcome(
                    proposal.action_id,
                    OutcomeStatus.FAILED if result.is_error else OutcomeStatus.SUCCESS,
                    "tool_error" if result.is_error else "tool_reported_success",
                )
            except asyncio.CancelledError:
                # The already durable pending record remains unknown. Propagate
                # cancellation; a caller can query it without repeating a write.
                self.audit.emit(
                    AuditPhase.OUTCOME,
                    AuditOutcome.CANCELLED,
                    proposal=proposal,
                    latency_ms=(time.monotonic() - started_at) * 1000,
                )
                raise
            except Exception:
                outcome = ActionOutcome(
                    proposal.action_id, OutcomeStatus.UNKNOWN, "delivery_unknown"
                )
            try:
                self.ledger.complete(proposal, outcome)
            except (sqlite3.Error, OSError):
                # A successful response whose receipt could not be persisted is
                # still conservatively unknown across process restarts.
                outcome = ActionOutcome(
                    proposal.action_id, OutcomeStatus.UNKNOWN, "receipt_unavailable"
                )
            self.audit.emit(
                AuditPhase.OUTCOME,
                AuditOutcome(outcome.status.value),
                proposal=proposal,
                latency_ms=(time.monotonic() - started_at) * 1000,
            )
            return outcome
