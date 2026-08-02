"""Small strict-JSON helpers shared by fail-closed persistence readers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def duplicate_key_rejecting_hook(
    error_type: type[Exception],
    message: str,
) -> Callable[[list[tuple[str, Any]]], dict[str, Any]]:
    """Build an ``object_pairs_hook`` that rejects duplicate JSON keys."""

    def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise error_type(message)
            value[key] = item
        return value

    return strict_object
