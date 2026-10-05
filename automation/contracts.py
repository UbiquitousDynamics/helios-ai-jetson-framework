"""Immutable values exchanged across the local automation boundary."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


def object_json(value: str) -> str:
    """Validate and normalize a bounded JSON object without retaining mutable values."""
    if not isinstance(value, str) or len(value.encode("utf-8")) > 65536:
        raise ValueError("Expected a bounded JSON object")

    def reject_constant(value: str) -> None:
        raise ValueError("Non-finite JSON number")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    try:
        decoded = json.loads(value, parse_constant=reject_constant, object_pairs_hook=pairs)
        if not isinstance(decoded, dict):
            raise ValueError("Expected a JSON object")
        return json.dumps(decoded, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (RecursionError, OverflowError) as exc:
        raise ValueError("Invalid JSON object") from exc


def identity(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("Identifiers must be nonempty strings")


def timestamp(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Expected a finite UTC epoch timestamp")


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    server_id: str
    name: str
    catalog_id: str
    input_schema_json: str

    def __post_init__(self) -> None:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError

        identity(self.server_id, self.name, self.catalog_id)
        schema = object_json(self.input_schema_json)
        if json.loads(schema).get("type") != "object":
            raise ValueError("Tool input schema must describe an object")
        try:
            Draft202012Validator.check_schema(json.loads(schema))
        except SchemaError as exc:
            raise ValueError("Invalid tool input schema") from exc
        object.__setattr__(self, "input_schema_json", schema)


@dataclass(frozen=True, slots=True)
class ActionProposal:
    server_id: str
    tool_name: str
    catalog_id: str
    session_id: str
    turn_id: str
    action_id: str
    arguments_json: str
    expires_at: float

    def __post_init__(self) -> None:
        identity(self.server_id, self.tool_name, self.catalog_id)
        identity(self.session_id, self.turn_id, self.action_id)
        timestamp(self.expires_at)
        object.__setattr__(self, "arguments_json", object_json(self.arguments_json))

    @property
    def fingerprint(self) -> str:
        fields = [getattr(self, name) for name in self.__dataclass_fields__]
        return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()

    def is_expired(self, now: float) -> bool:
        timestamp(now)
        return now >= self.expires_at


class OutcomeStatus(str, Enum):
    SUCCESS = "success"
    DENIED = "denied"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Authorization:
    proposal_fingerprint: str
    allowed: bool
    reason_code: str

    def __post_init__(self) -> None:
        identity(self.proposal_fingerprint, self.reason_code)
        if type(self.allowed) is not bool:
            raise ValueError("Authorization must be a boolean")


@dataclass(frozen=True, slots=True)
class Confirmation:
    proposal_fingerprint: str
    session_id: str
    expires_at: float

    def __post_init__(self) -> None:
        identity(self.proposal_fingerprint, self.session_id)
        timestamp(self.expires_at)

    def matches(self, proposal: ActionProposal, now: float) -> bool:
        timestamp(now)
        return (
            self.proposal_fingerprint == proposal.fingerprint
            and self.session_id == proposal.session_id
            and now < self.expires_at
            and not proposal.is_expired(now)
        )


@dataclass(frozen=True, slots=True)
class DispatchReceipt:
    action_id: str
    proposal_fingerprint: str
    dispatched_at: float

    def __post_init__(self) -> None:
        identity(self.action_id, self.proposal_fingerprint)
        timestamp(self.dispatched_at)


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    action_id: str
    status: OutcomeStatus
    reason_code: str

    def __post_init__(self) -> None:
        identity(self.action_id, self.reason_code)
        if not isinstance(self.status, OutcomeStatus):
            raise ValueError("Invalid action outcome status")


class Authorizer(Protocol):
    def authorize(self, proposal: ActionProposal, now: float) -> Authorization: ...


class Executor(Protocol):
    def execute(
        self, proposal: ActionProposal, confirmation: Confirmation | None
    ) -> ActionOutcome: ...
