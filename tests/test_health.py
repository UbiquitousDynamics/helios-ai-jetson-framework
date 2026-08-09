import pytest

from api.health import HealthStatus, HealthTracker
from api.providers.contracts import ErrorCategory, ProviderError


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_transient_failures_open_exponential_circuit_and_success_tracks_ewma():
    clock = FakeClock()
    health = HealthTracker(
        failures_to_open=2,
        cooldown_seconds=10,
        maximum_cooldown_seconds=20,
        latency_alpha=0.5,
        clock=clock,
    )

    health.record_failure("p/m", ErrorCategory.CONNECTIVITY)
    assert health.is_available("p/m")
    health.record_failure("p/m", ErrorCategory.READ_TIMEOUT)
    snapshot = health.snapshot("p/m")
    assert snapshot.status is HealthStatus.COOLDOWN
    assert snapshot.retry_after_seconds == pytest.approx(10)

    clock.advance(10)
    assert health.is_available("p/m")
    health.record_success("p/m", latency_seconds=2)
    health.record_success("p/m", latency_seconds=4)
    assert health.snapshot("p/m").latency_ewma_seconds == pytest.approx(3)

    health.record_failure("p/m", ErrorCategory.CONNECTIVITY)
    health.record_failure("p/m", ErrorCategory.CONNECTIVITY)
    assert health.snapshot("p/m").retry_after_seconds == pytest.approx(20)


def test_retry_after_auth_and_quota_have_independent_reset_paths():
    clock = FakeClock()
    health = HealthTracker(clock=clock)
    error = ProviderError(
        ErrorCategory.PROVIDER_UNAVAILABLE,
        "safe",
        provider="p",
        retry_after_seconds=7,
    )
    health.record_failure("retry", error)
    assert health.snapshot("retry").status is HealthStatus.RATE_LIMITED
    clock.advance(7)
    assert health.is_available("retry")

    health.record_failure("auth", ErrorCategory.AUTHENTICATION)
    assert health.snapshot("auth").status is HealthStatus.AUTH_BLOCKED
    health.reset_authorization("auth")
    assert health.is_available("auth")

    health.record_failure(
        "quota",
        ErrorCategory.QUOTA_EXHAUSTED,
        quota_reset_after_seconds=12,
    )
    assert health.snapshot("quota").status is HealthStatus.QUOTA_BLOCKED
    clock.advance(12)
    assert health.is_available("quota")


def test_non_retryable_request_errors_do_not_poison_provider_health():
    health = HealthTracker(failures_to_open=1)
    health.record_failure("p/m", ErrorCategory.CONTEXT_OVERFLOW)
    assert health.is_available("p/m")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"failures_to_open": True},
        {"cooldown_seconds": float("nan")},
        {"maximum_cooldown_seconds": float("inf")},
        {"latency_alpha": float("nan")},
    ],
)
def test_invalid_health_numbers_are_rejected(kwargs):
    with pytest.raises(ValueError):
        HealthTracker(**kwargs)


def test_invalid_clock_and_latency_values_are_rejected():
    health = HealthTracker(clock=lambda: float("nan"))
    with pytest.raises(ValueError, match="clock"):
        health.snapshot("p/m")
    with pytest.raises(ValueError, match="latency"):
        HealthTracker().record_success("p/m", latency_seconds=True)


def test_in_flight_failure_burst_does_not_extend_an_open_circuit():
    clock = FakeClock()
    health = HealthTracker(
        failures_to_open=1,
        cooldown_seconds=10,
        maximum_cooldown_seconds=100,
        clock=clock,
    )
    health.record_failure("p/m", ErrorCategory.CONNECTIVITY)
    clock.advance(1)

    health.record_failure("p/m", ErrorCategory.READ_TIMEOUT)

    assert health.snapshot("p/m").retry_after_seconds == pytest.approx(9)


def test_transition_observer_reports_expiry_and_explicit_resets_once():
    clock = FakeClock()
    transitions = []
    health = HealthTracker(
        failures_to_open=1,
        cooldown_seconds=5,
        clock=clock,
        transition_observer=lambda previous, current: transitions.append(
            (previous.status, current.status)
        ),
    )

    health.record_failure("p/m", ErrorCategory.CONNECTIVITY)
    health.snapshot("p/m")
    clock.advance(5)
    health.snapshot("p/m")
    health.snapshot("p/m")
    health.record_failure("auth", ErrorCategory.AUTHENTICATION)
    health.reset_authorization("auth")

    assert transitions == [
        (HealthStatus.AVAILABLE, HealthStatus.COOLDOWN),
        (HealthStatus.COOLDOWN, HealthStatus.AVAILABLE),
        (HealthStatus.AVAILABLE, HealthStatus.AUTH_BLOCKED),
        (HealthStatus.AUTH_BLOCKED, HealthStatus.AVAILABLE),
    ]
