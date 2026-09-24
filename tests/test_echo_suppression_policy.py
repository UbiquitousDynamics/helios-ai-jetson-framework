from __future__ import annotations

import pytest

from recognizer.echo_suppression_policy import (
    ConservativeEchoSuppressionPolicy,
    NoEchoSuppressionPolicy,
)


def test_no_op_policy_never_suppresses() -> None:
    policy = NoEchoSuppressionPolicy()

    assert not policy.should_suppress(0.0, 0.0)
    assert not policy.should_suppress(1.0, 10.0)


def test_conservative_policy_uses_higher_threshold_during_startup() -> None:
    policy = ConservativeEchoSuppressionPolicy(
        expected_echo_energy=0.04,
        minimum_interrupt_energy=0.05,
        echo_energy_ratio=1.5,
        startup_window_seconds=0.4,
        startup_energy_multiplier=2.0,
    )

    assert policy.steady_state_threshold == pytest.approx(0.06)
    assert policy.should_suppress(0.08, 0.2)
    assert not policy.should_suppress(0.08, 0.4)


def test_conservative_policy_suppresses_at_decision_boundary() -> None:
    policy = ConservativeEchoSuppressionPolicy(
        expected_echo_energy=0.03,
        minimum_interrupt_energy=0.05,
        echo_energy_ratio=1.5,
        startup_window_seconds=0.0,
    )

    assert policy.should_suppress(0.05, 1.0)
    assert not policy.should_suppress(0.0501, 1.0)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"expected_echo_energy": -0.1}, "expected_echo_energy"),
        ({"echo_energy_ratio": 0.9}, "echo_energy_ratio"),
        ({"startup_window_seconds": -1.0}, "startup_window_seconds"),
    ],
)
def test_conservative_policy_rejects_invalid_configuration(
    kwargs: dict[str, float],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ConservativeEchoSuppressionPolicy(**kwargs)
