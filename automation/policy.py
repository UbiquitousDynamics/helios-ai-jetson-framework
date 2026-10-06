"""Operator-owned policy. Server descriptions and model text grant no authority."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

from automation.contracts import ActionProposal, Authorization, ToolDescriptor, object_json
from automation.settings import AutomationSettings


class ActionKind(str, Enum):
    READ = "read"
    LIGHT = "light"
    SCENE = "scene"


@dataclass(frozen=True, slots=True)
class ArgumentBound:
    name: str
    minimum: float
    maximum: float

    def __post_init__(self):
        if (
            not self.name
            or not math.isfinite(self.minimum)
            or not math.isfinite(self.maximum)
            or self.minimum > self.maximum
        ):
            raise ValueError("Invalid local argument bound")


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    server_id: str
    tool_name: str
    kind: ActionKind
    entity_argument: str = "entity_id"
    area_argument: str = "area_id"
    fixed_arguments_json: str = "{}"
    bounds: tuple[ArgumentBound, ...] = ()
    require_entity: bool = True

    def __post_init__(self):
        if not isinstance(self.kind, ActionKind) or not self.server_id or not self.tool_name:
            raise ValueError("Invalid local tool classification")
        if not isinstance(self.bounds, tuple) or any(
            not isinstance(item, ArgumentBound) for item in self.bounds
        ):
            raise ValueError("Invalid argument bounds")
        if type(self.require_entity) is not bool or (
            self.kind != ActionKind.READ and not self.require_entity
        ):
            raise ValueError("Writes require explicit entity targets")
        fixed = object_json(self.fixed_arguments_json)
        names = [item.name for item in self.bounds]
        if len(names) != len(set(names)) or set(names) & {self.entity_argument, self.area_argument}:
            raise ValueError("Overlapping argument bounds")
        if set(json.loads(fixed)) & (set(names) | {self.entity_argument, self.area_argument}):
            raise ValueError("Fixed arguments overlap dynamic arguments")
        object.__setattr__(self, "fixed_arguments_json", fixed)


class LocalPolicy:
    def __init__(self, settings: AutomationSettings, rules: tuple[ToolPolicy, ...] = ()):
        if not isinstance(rules, tuple):
            raise ValueError("Rules must be immutable")
        keys = [(rule.server_id, rule.tool_name) for rule in rules]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate local tool policy")
        self.settings = settings
        self.rules = rules

    def rule_for(self, proposal: ActionProposal) -> ToolPolicy | None:
        return next(
            (
                rule
                for rule in self.rules
                if (rule.server_id, rule.tool_name) == (proposal.server_id, proposal.tool_name)
            ),
            None,
        )

    def requires_confirmation(self, proposal: ActionProposal) -> bool:
        rule = self.rule_for(proposal)
        return rule is None or rule.kind != ActionKind.READ

    def authorize(
        self, proposal: ActionProposal, now: float, *, catalog: tuple[ToolDescriptor, ...] = ()
    ) -> Authorization:
        def decision(reason: str, allowed: bool = False):
            return Authorization(proposal.fingerprint, allowed, reason)

        if not self.settings.enabled:
            return decision("disabled")
        if proposal.is_expired(now):
            return decision("expired")
        server = next(
            (item for item in self.settings.servers if item.server_id == proposal.server_id), None
        )
        rule = self.rule_for(proposal)
        if server is None or rule is None or proposal.tool_name not in server.tools:
            return decision("scope")
        descriptor = next(
            (
                item
                for item in catalog
                if (item.server_id, item.name, item.catalog_id)
                == (proposal.server_id, proposal.tool_name, proposal.catalog_id)
            ),
            None,
        )
        if descriptor is None:
            return decision("catalog_changed")
        lowered = proposal.tool_name.lower()
        if any(
            word in lowered for word in ("lock", "alarm", "script", "admin", "delete", "service")
        ):
            return decision("sensitive_operation")
        arguments = json.loads(proposal.arguments_json)
        fixed = json.loads(rule.fixed_arguments_json)
        if any(
            key not in arguments
            or json.dumps(arguments[key], sort_keys=True) != json.dumps(value, sort_keys=True)
            for key, value in fixed.items()
        ):
            return decision("fixed_arguments")
        allowed_keys = (
            set(fixed)
            | {rule.entity_argument, rule.area_argument}
            | {item.name for item in rule.bounds}
        )
        if set(arguments) - allowed_keys:
            return decision("unexpected_arguments")
        entity = arguments.get(rule.entity_argument)
        if entity is None and rule.require_entity:
            return decision("ambiguous_target")
        if entity is not None:
            if not isinstance(entity, str) or entity not in server.entities:
                return decision("entity_scope")
            domain = entity.partition(".")[0]
            if domain in {"lock", "alarm_control_panel", "script", "automation"}:
                return decision("sensitive_entity")
            if rule.kind != ActionKind.READ and domain != rule.kind.value:
                return decision("entity_kind")
        area = arguments.get(rule.area_argument)
        if area is not None and (not isinstance(area, str) or area not in server.areas):
            return decision("area_scope")
        if area is not None and rule.kind != ActionKind.READ:
            # Servers may interpret area+entity selectors as a union. Expand an
            # area locally into exact entities before producing write proposals.
            return decision("ambiguous_target")
        for bound in rule.bounds:
            value = arguments.get(bound.name)
            if value is not None and (
                type(value) not in (int, float) or not bound.minimum <= value <= bound.maximum
            ):
                return decision("argument_bounds")
        try:
            from jsonschema import Draft202012Validator
            from referencing import Registry

            Draft202012Validator(
                json.loads(descriptor.input_schema_json), registry=Registry()
            ).validate(arguments)
        except Exception:
            return decision("argument_schema")
        return decision("authorized", True)

    def remote_context(
        self, *, catalog: Any = None, home_state: Any = None, results: Any = None
    ) -> dict[str, Any]:
        """A transcript opt-in and the legacy generic flag authorize none of these."""
        if not self.settings.enabled:
            return {}
        permitted = {}
        for key, allowed, value in (
            ("catalog", self.settings.allow_remote_catalog, catalog),
            ("home_state", self.settings.allow_remote_home_state, home_state),
            ("results", self.settings.allow_remote_results, results),
        ):
            if allowed and value is not None:
                # Return a snapshot, not an alias to a caller's mutable context.
                permitted[key] = json.loads(json.dumps(value, allow_nan=False))
        return permitted
