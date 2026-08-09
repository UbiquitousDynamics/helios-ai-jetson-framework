"""Content-free provider health and circuit tracking."""

from __future__ import annotations

import math
import numbers
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable

from api.providers.contracts import ErrorCategory, ProviderError


class HealthStatus(str, Enum):
    AVAILABLE = "available"
    COOLDOWN = "cooldown"
    RATE_LIMITED = "rate_limited"
    AUTH_BLOCKED = "auth_blocked"
    QUOTA_BLOCKED = "quota_blocked"


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    key: str
    status: HealthStatus
    retry_after_seconds: float | None
    consecutive_failures: int
    successes: int
    failures: int
    latency_ewma_seconds: float | None

    @property
    def available(self) -> bool:
        return self.status is HealthStatus.AVAILABLE


@dataclass(slots=True)
class _HealthState:
    consecutive_failures: int = 0
    successes: int = 0
    failures: int = 0
    open_count: int = 0
    cooldown_until: float = 0.0
    rate_limited_until: float = 0.0
    quota_until: float = 0.0
    auth_blocked: bool = False
    latency_ewma_seconds: float | None = None
    reported_status: HealthStatus = HealthStatus.AVAILABLE


_TRANSIENT_FAILURES = frozenset(
    {
        ErrorCategory.CONNECTIVITY,
        ErrorCategory.DNS,
        ErrorCategory.CONNECT_TIMEOUT,
        ErrorCategory.FIRST_TOKEN_TIMEOUT,
        ErrorCategory.READ_TIMEOUT,
        ErrorCategory.PROVIDER_UNAVAILABLE,
        ErrorCategory.MALFORMED_RESPONSE,
        ErrorCategory.EMPTY_COMPLETION,
        ErrorCategory.UNKNOWN,
    }
)


class HealthTracker:
    """Tracks independent provider/model circuits using a monotonic clock."""

    def __init__(
        self,
        *,
        failures_to_open: int = 3,
        cooldown_seconds: float = 60.0,
        maximum_cooldown_seconds: float = 900.0,
        latency_alpha: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
        transition_observer: Callable[[HealthSnapshot, HealthSnapshot], None] | None = None,
    ) -> None:
        if (
            isinstance(failures_to_open, bool)
            or not isinstance(failures_to_open, int)
            or failures_to_open < 1
        ):
            raise ValueError("failures_to_open must be at least one")
        if not self._is_finite_number(cooldown_seconds) or cooldown_seconds <= 0:
            raise ValueError("invalid cooldown bounds")
        if (
            not self._is_finite_number(maximum_cooldown_seconds)
            or maximum_cooldown_seconds < cooldown_seconds
        ):
            raise ValueError("invalid cooldown bounds")
        if not self._is_finite_number(latency_alpha) or not 0 < latency_alpha <= 1:
            raise ValueError("latency_alpha must be in (0, 1]")
        self._failures_to_open = failures_to_open
        self._cooldown_seconds = float(cooldown_seconds)
        self._maximum_cooldown_seconds = float(maximum_cooldown_seconds)
        self._latency_alpha = float(latency_alpha)
        self._clock = clock
        self._transition_observer = transition_observer
        self._states: dict[str, _HealthState] = {}
        self._lock = threading.RLock()

    def set_transition_observer(
        self,
        observer: Callable[[HealthSnapshot, HealthSnapshot], None] | None,
    ) -> None:
        with self._lock:
            self._transition_observer = observer

    def snapshot(self, key: str) -> HealthSnapshot:
        key = self._validate_key(key)
        transition: tuple[HealthSnapshot, HealthSnapshot] | None = None
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return self._snapshot_for(key, _HealthState(), self._now())
            now = self._now()
            current = self._snapshot_for(key, state, now)
            transition = self._reported_transition(state, current)
        self._notify_transition(transition)
        return current

    def is_available(self, key: str) -> bool:
        return self.snapshot(key).available

    def record_success(self, key: str, *, latency_seconds: float | None = None) -> None:
        key = self._validate_key(key)
        if latency_seconds is not None and (
            not self._is_finite_number(latency_seconds) or latency_seconds < 0
        ):
            raise ValueError("latency_seconds must be finite and non-negative")
        transition: tuple[HealthSnapshot, HealthSnapshot] | None = None
        with self._lock:
            state = self._states.setdefault(key, _HealthState())
            now = self._now()
            before = self._snapshot_for(key, state, now)
            state.successes += 1
            state.consecutive_failures = 0
            state.cooldown_until = 0.0
            state.rate_limited_until = 0.0
            if latency_seconds is not None:
                if state.latency_ewma_seconds is None:
                    state.latency_ewma_seconds = float(latency_seconds)
                else:
                    alpha = self._latency_alpha
                    state.latency_ewma_seconds = (
                        alpha * latency_seconds + (1.0 - alpha) * state.latency_ewma_seconds
                    )
            after = self._snapshot_for(key, state, now)
            transition = self._reported_transition(state, after, before=before)
        self._notify_transition(transition)

    def record_failure(
        self,
        key: str,
        failure: ErrorCategory | ProviderError,
        *,
        retry_after_seconds: float | None = None,
        quota_reset_after_seconds: float | None = None,
    ) -> None:
        key = self._validate_key(key)
        if isinstance(failure, ProviderError):
            category = failure.category
            if retry_after_seconds is None:
                retry_after_seconds = failure.retry_after_seconds
        else:
            category = ErrorCategory(failure)
        self._validate_delay(retry_after_seconds, "retry_after_seconds")
        self._validate_delay(quota_reset_after_seconds, "quota_reset_after_seconds")

        transition: tuple[HealthSnapshot, HealthSnapshot] | None = None
        with self._lock:
            now = self._now()
            state = self._states.setdefault(key, _HealthState())
            before = self._snapshot_for(key, state, now)
            state.failures += 1
            if retry_after_seconds is not None:
                state.rate_limited_until = max(state.rate_limited_until, now + retry_after_seconds)

            if category in {ErrorCategory.AUTHENTICATION, ErrorCategory.PERMISSION}:
                state.auth_blocked = True
            elif category is ErrorCategory.QUOTA_EXHAUSTED:
                state.quota_until = (
                    now + quota_reset_after_seconds
                    if quota_reset_after_seconds is not None
                    else math.inf
                )
            elif category is ErrorCategory.RATE_LIMITED:
                delay = (
                    self._cooldown_seconds if retry_after_seconds is None else retry_after_seconds
                )
                state.rate_limited_until = max(state.rate_limited_until, now + delay)
            elif category in _TRANSIENT_FAILURES:
                state.consecutive_failures += 1
                if (
                    state.consecutive_failures >= self._failures_to_open
                    and state.cooldown_until <= now
                ):
                    exponent = min(state.open_count, 30)
                    delay = min(
                        self._cooldown_seconds * (2**exponent),
                        self._maximum_cooldown_seconds,
                    )
                    if retry_after_seconds is not None:
                        delay = max(delay, retry_after_seconds)
                    state.cooldown_until = max(state.cooldown_until, now + delay)
                    state.open_count += 1
            after = self._snapshot_for(key, state, now)
            transition = self._reported_transition(state, after, before=before)
        self._notify_transition(transition)

    @staticmethod
    def _reported_transition(
        state: _HealthState,
        current: HealthSnapshot,
        *,
        before: HealthSnapshot | None = None,
    ) -> tuple[HealthSnapshot, HealthSnapshot] | None:
        previous_status = state.reported_status
        if previous_status is current.status:
            return None
        previous = (
            before
            if before is not None and before.status is previous_status
            else replace(
                current,
                status=previous_status,
                retry_after_seconds=None,
            )
        )
        state.reported_status = current.status
        return previous, current

    def _notify_transition(
        self,
        transition: tuple[HealthSnapshot, HealthSnapshot] | None,
    ) -> None:
        if transition is None:
            return
        with self._lock:
            observer = self._transition_observer
        if observer is not None:
            try:
                observer(*transition)
            except Exception:
                return

    @classmethod
    def _snapshot_for(
        cls,
        key: str,
        state: _HealthState,
        now: float,
    ) -> HealthSnapshot:
        status, retry_after = cls._status(state, now)
        return HealthSnapshot(
            key=key,
            status=status,
            retry_after_seconds=retry_after,
            consecutive_failures=state.consecutive_failures,
            successes=state.successes,
            failures=state.failures,
            latency_ewma_seconds=state.latency_ewma_seconds,
        )

    def reset_authorization(self, key: str) -> None:
        key = self._validate_key(key)
        transition: tuple[HealthSnapshot, HealthSnapshot] | None = None
        with self._lock:
            state = self._states.setdefault(key, _HealthState())
            now = self._now()
            before = self._snapshot_for(key, state, now)
            state.auth_blocked = False
            after = self._snapshot_for(key, state, now)
            transition = self._reported_transition(state, after, before=before)
        self._notify_transition(transition)

    def reset_quota(self, key: str) -> None:
        key = self._validate_key(key)
        transition: tuple[HealthSnapshot, HealthSnapshot] | None = None
        with self._lock:
            state = self._states.setdefault(key, _HealthState())
            now = self._now()
            before = self._snapshot_for(key, state, now)
            state.quota_until = 0.0
            after = self._snapshot_for(key, state, now)
            transition = self._reported_transition(state, after, before=before)
        self._notify_transition(transition)

    def reset(self, key: str) -> None:
        key = self._validate_key(key)
        transition: tuple[HealthSnapshot, HealthSnapshot] | None = None
        with self._lock:
            state = self._states.pop(key, None)
            if state is not None and state.reported_status is not HealthStatus.AVAILABLE:
                now = self._now()
                before = self._snapshot_for(key, state, now)
                after = self._snapshot_for(key, _HealthState(), now)
                transition = (
                    before
                    if before.status is state.reported_status
                    else replace(
                        before,
                        status=state.reported_status,
                        retry_after_seconds=None,
                    ),
                    after,
                )
        self._notify_transition(transition)

    @staticmethod
    def _validate_key(key: str) -> str:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("health key cannot be empty")
        return key

    @staticmethod
    def _validate_delay(value: float | None, name: str) -> None:
        if value is not None and (not HealthTracker._is_finite_number(value) or value < 0):
            raise ValueError(f"{name} must be finite and non-negative")

    def _now(self) -> float:
        value = self._clock()
        if not self._is_finite_number(value):
            raise ValueError("health clock must return a finite number")
        return float(value)

    @staticmethod
    def _is_finite_number(value: object) -> bool:
        return (
            not isinstance(value, bool) and isinstance(value, numbers.Real) and math.isfinite(value)
        )

    @staticmethod
    def _status(state: _HealthState, now: float) -> tuple[HealthStatus, float | None]:
        if state.auth_blocked:
            return HealthStatus.AUTH_BLOCKED, None
        if state.quota_until > now:
            delay = None if math.isinf(state.quota_until) else state.quota_until - now
            return HealthStatus.QUOTA_BLOCKED, delay
        if state.rate_limited_until > now:
            return HealthStatus.RATE_LIMITED, state.rate_limited_until - now
        if state.cooldown_until > now:
            return HealthStatus.COOLDOWN, state.cooldown_until - now
        return HealthStatus.AVAILABLE, None


__all__ = ["HealthSnapshot", "HealthStatus", "HealthTracker"]
