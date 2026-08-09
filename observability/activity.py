"""Low-cardinality inference activity state for resource correlation."""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActivitySnapshot:
    active: bool = False
    mode: str | None = None
    locality: str | None = None
    provider: str | None = None
    model: str | None = None
    route: str | None = None


class InferenceActivityTracker:
    """Track active attempts without retaining request identifiers or content."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._activities: dict[object, ActivitySnapshot] = {}

    def begin(
        self,
        *,
        mode: str,
        locality: str,
        provider: str,
        model: str,
        route: str,
    ) -> object:
        token = object()
        snapshot = ActivitySnapshot(True, mode, locality, provider, model, route)
        with self._lock:
            self._activities[token] = snapshot
        return token

    def end(self, token: object | None) -> None:
        if token is None:
            return
        with self._lock:
            self._activities.pop(token, None)

    def snapshot(self) -> ActivitySnapshot:
        with self._lock:
            values = tuple(self._activities.values())
        if not values:
            return ActivitySnapshot()
        if len(values) == 1:
            return values[0]

        def shared(name: str) -> str | None:
            candidates = {getattr(value, name) for value in values}
            return candidates.pop() if len(candidates) == 1 else None

        locality = shared("locality")
        return ActivitySnapshot(
            active=True,
            mode=shared("mode"),
            locality=locality or "mixed",
            provider=shared("provider"),
            model=shared("model"),
            route=shared("route"),
        )


__all__ = ["ActivitySnapshot", "InferenceActivityTracker"]
