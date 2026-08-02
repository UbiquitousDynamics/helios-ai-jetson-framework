"""Strict, dated model-pricing catalog with exact decimal arithmetic."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from api._strict_json import duplicate_key_rejecting_hook
from api.providers.contracts import Usage


class CatalogError(RuntimeError):
    """The catalog is absent, malformed, stale, or missing a requested model."""


_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "catalog_revision", "verified_on", "expires_on", "models"}
)
_MODEL_REQUIRED_KEYS = frozenset(
    {
        "id",
        "provider",
        "model",
        "input_per_million_usd",
        "output_per_million_usd",
        "context_window",
        "max_output_tokens",
        "free_tier",
    }
)
_MODEL_OPTIONAL_KEYS = frozenset(
    {"cached_input_per_million_usd", "reasoning_output_per_million_usd"}
)
_MILLION = Decimal(1_000_000)


_strict_json_object = duplicate_key_rejecting_hook(
    CatalogError,
    "model catalog contains a duplicate key",
)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    id: str
    provider: str
    model: str
    input_per_million_usd: Decimal
    output_per_million_usd: Decimal
    context_window: int
    max_output_tokens: int
    free_tier: bool
    cached_input_per_million_usd: Decimal | None = None
    reasoning_output_per_million_usd: Decimal | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.id, str)
            or not isinstance(self.provider, str)
            or not isinstance(self.model, str)
            or not self.id.strip()
            or not self.provider.strip()
            or not self.model.strip()
        ):
            raise ValueError("model price identifiers cannot be empty")
        for name in (
            "input_per_million_usd",
            "output_per_million_usd",
            "cached_input_per_million_usd",
            "reasoning_output_per_million_usd",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite() or value < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative Decimal")
        if (
            isinstance(self.context_window, bool)
            or not isinstance(self.context_window, int)
            or isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.context_window < 1
            or self.max_output_tokens < 1
        ):
            raise ValueError("model token limits must be positive")
        if self.max_output_tokens > self.context_window:
            raise ValueError("max_output_tokens cannot exceed context_window")
        if not isinstance(self.free_tier, bool):
            raise ValueError("free_tier must be a boolean")

    def estimate(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> Decimal:
        values = (input_tokens, output_tokens, cached_input_tokens, reasoning_tokens)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values
        ):
            raise ValueError("token counts must be non-negative integers")
        if cached_input_tokens > input_tokens:
            raise ValueError("cached input tokens cannot exceed input tokens")
        if reasoning_tokens > output_tokens:
            raise ValueError("reasoning tokens cannot exceed output tokens")
        uncached = input_tokens - cached_input_tokens
        visible_output = output_tokens - reasoning_tokens
        cached_rate = (
            self.input_per_million_usd
            if self.cached_input_per_million_usd is None
            else self.cached_input_per_million_usd
        )
        reasoning_rate = (
            self.output_per_million_usd
            if self.reasoning_output_per_million_usd is None
            else self.reasoning_output_per_million_usd
        )
        return (
            Decimal(uncached) * self.input_per_million_usd
            + Decimal(cached_input_tokens) * cached_rate
            + Decimal(visible_output) * self.output_per_million_usd
            + Decimal(reasoning_tokens) * reasoning_rate
        ) / _MILLION

    def estimate_usage(self, usage: Usage) -> Decimal | None:
        if usage.input_tokens is None or usage.output_tokens is None:
            return None
        return self.estimate(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens or 0,
            reasoning_tokens=usage.reasoning_tokens or 0,
        )


class ModelCatalog:
    """Immutable catalog parsed from a strict JSON schema."""

    def __init__(
        self,
        *,
        revision: str,
        verified_on: date,
        expires_on: date,
        models: Mapping[str, ModelPrice],
        today: date | None = None,
    ) -> None:
        self.revision = revision
        self.verified_on = verified_on
        self.expires_on = expires_on
        self._models = dict(models)
        self._today = today

    @classmethod
    def load(cls, path: str | Path, *, today: date | None = None) -> ModelCatalog:
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                payload = json.load(handle, object_pairs_hook=_strict_json_object)
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise CatalogError("model catalog could not be loaded") from None
        return cls.from_mapping(payload, today=today)

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        today: date | None = None,
    ) -> ModelCatalog:
        if not isinstance(payload, Mapping):
            raise CatalogError("model catalog root must be an object")
        cls._require_exact_keys(payload, _TOP_LEVEL_KEYS, "catalog")
        if isinstance(payload["schema_version"], bool) or payload["schema_version"] != 1:
            raise CatalogError("unsupported model catalog schema version")
        revision = cls._nonempty_string(payload["catalog_revision"], "catalog_revision")
        verified = cls._parse_date(payload["verified_on"], "verified_on")
        expires = cls._parse_date(payload["expires_on"], "expires_on")
        if expires < verified:
            raise CatalogError("catalog expiry precedes its verification date")
        raw_models = payload["models"]
        if not isinstance(raw_models, list) or not raw_models:
            raise CatalogError("catalog models must be a non-empty list")

        models: dict[str, ModelPrice] = {}
        allowed_model_keys = _MODEL_REQUIRED_KEYS | _MODEL_OPTIONAL_KEYS
        for index, raw in enumerate(raw_models):
            if not isinstance(raw, Mapping):
                raise CatalogError(f"model entry {index} must be an object")
            cls._require_keys(raw, _MODEL_REQUIRED_KEYS, allowed_model_keys, f"model {index}")
            model_id = cls._nonempty_string(raw["id"], f"model {index} id")
            if model_id in models:
                raise CatalogError(f"duplicate catalog model id: {model_id}")
            context_window = cls._positive_int(raw["context_window"], "context_window")
            max_output = cls._positive_int(raw["max_output_tokens"], "max_output_tokens")
            if max_output > context_window:
                raise CatalogError("max_output_tokens cannot exceed context_window")
            if not isinstance(raw["free_tier"], bool):
                raise CatalogError("free_tier must be a boolean")
            models[model_id] = ModelPrice(
                id=model_id,
                provider=cls._nonempty_string(raw["provider"], "provider"),
                model=cls._nonempty_string(raw["model"], "model"),
                input_per_million_usd=cls._decimal_rate(
                    raw["input_per_million_usd"], "input_per_million_usd"
                ),
                output_per_million_usd=cls._decimal_rate(
                    raw["output_per_million_usd"], "output_per_million_usd"
                ),
                cached_input_per_million_usd=cls._optional_rate(
                    raw.get("cached_input_per_million_usd"),
                    "cached_input_per_million_usd",
                ),
                reasoning_output_per_million_usd=cls._optional_rate(
                    raw.get("reasoning_output_per_million_usd"),
                    "reasoning_output_per_million_usd",
                ),
                context_window=context_window,
                max_output_tokens=max_output,
                free_tier=raw["free_tier"],
            )
        return cls(
            revision=revision,
            verified_on=verified,
            expires_on=expires,
            models=models,
            today=today,
        )

    def is_stale(self, *, on_date: date | None = None) -> bool:
        current = on_date or self._today or date.today()
        return current > self.expires_on

    def require_fresh(self, *, on_date: date | None = None) -> None:
        current = on_date or self._today or date.today()
        if current < self.verified_on:
            raise CatalogError("model catalog verification date is in the future")
        if current > self.expires_on:
            raise CatalogError("model catalog is stale")

    def get(
        self,
        model_id: str,
        *,
        require_fresh: bool = True,
        on_date: date | None = None,
    ) -> ModelPrice:
        if require_fresh:
            self.require_fresh(on_date=on_date)
        try:
            return self._models[model_id]
        except KeyError:
            raise CatalogError(f"model is absent from catalog: {model_id}") from None

    @property
    def model_ids(self) -> tuple[str, ...]:
        return tuple(self._models)

    @staticmethod
    def _require_exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
        actual = frozenset(value)
        if actual != expected:
            unknown = sorted(actual - expected)
            missing = sorted(expected - actual)
            raise CatalogError(f"{label} has unknown keys {unknown} or missing keys {missing}")

    @staticmethod
    def _require_keys(
        value: Mapping[str, Any],
        required: frozenset[str],
        allowed: frozenset[str],
        label: str,
    ) -> None:
        actual = frozenset(value)
        unknown = actual - allowed
        missing = required - actual
        if unknown or missing:
            raise CatalogError(
                f"{label} has unknown keys {sorted(unknown)} or missing keys {sorted(missing)}"
            )

    @staticmethod
    def _nonempty_string(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise CatalogError(f"{label} must be a non-empty string")
        return value

    @staticmethod
    def _parse_date(value: Any, label: str) -> date:
        if not isinstance(value, str):
            raise CatalogError(f"{label} must be an ISO date string")
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise CatalogError(f"{label} must be an ISO date string") from None
        if parsed.isoformat() != value:
            raise CatalogError(f"{label} must use YYYY-MM-DD format")
        return parsed

    @staticmethod
    def _positive_int(value: Any, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise CatalogError(f"{label} must be a positive integer")
        return value

    @classmethod
    def _optional_rate(cls, value: Any, label: str) -> Decimal | None:
        if value is None:
            return None
        return cls._decimal_rate(value, label)

    @staticmethod
    def _decimal_rate(value: Any, label: str) -> Decimal:
        if not isinstance(value, str):
            raise CatalogError(f"{label} must be a decimal string")
        try:
            amount = Decimal(value)
        except InvalidOperation:
            raise CatalogError(f"{label} must be a decimal string") from None
        if not amount.is_finite() or amount < 0:
            raise CatalogError(f"{label} must be finite and non-negative")
        return amount


__all__ = ["CatalogError", "ModelCatalog", "ModelPrice"]
