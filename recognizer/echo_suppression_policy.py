"""Pure policies for suppressing likely playback echo during barge-in."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol


class EchoSuppressionPolicy(Protocol):
    """Decide whether a prospective barge-in is probably playback echo."""

    def should_suppress(
        self,
        frame_energy: float,
        elapsed_since_tts_start: float,
    ) -> bool:
        """Return true when a detection at this energy and time should be vetoed."""

        ...


@dataclass(frozen=True)
class NoEchoSuppressionPolicy:
    """Default policy that leaves every prospective detection enabled."""

    def should_suppress(
        self,
        frame_energy: float,
        elapsed_since_tts_start: float,
    ) -> bool:
        del frame_energy, elapsed_since_tts_start
        return False


@dataclass(frozen=True)
class ConservativeEchoSuppressionPolicy:
    """Suppress low-energy candidates that are likely to be speaker leakage.

    Energies are normalized RMS values in the inclusive range ``0.0`` to
    ``1.0``. The baseline threshold is derived from both an absolute floor and
    a calibrated expected echo level. A higher threshold applies briefly after
    TTS starts, when transients and direct speaker leakage are most likely.
    """

    expected_echo_energy: float = 0.04
    minimum_interrupt_energy: float = 0.06
    echo_energy_ratio: float = 1.5
    startup_window_seconds: float = 0.4
    startup_energy_multiplier: float = 1.5

    def __post_init__(self) -> None:
        self._validate_energy(self.expected_echo_energy, "expected_echo_energy")
        self._validate_energy(self.minimum_interrupt_energy, "minimum_interrupt_energy")
        if not math.isfinite(self.echo_energy_ratio) or self.echo_energy_ratio < 1:
            raise ValueError("echo_energy_ratio must be finite and at least 1")
        if not math.isfinite(self.startup_window_seconds) or self.startup_window_seconds < 0:
            raise ValueError("startup_window_seconds must be finite and non-negative")
        if not math.isfinite(self.startup_energy_multiplier) or self.startup_energy_multiplier < 1:
            raise ValueError("startup_energy_multiplier must be finite and at least 1")

    @staticmethod
    def _validate_energy(value: float, name: str) -> None:
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be finite and between 0 and 1")

    @property
    def steady_state_threshold(self) -> float:
        """Return the configured post-startup interruption threshold."""

        return min(
            1.0,
            max(
                self.minimum_interrupt_energy,
                self.expected_echo_energy * self.echo_energy_ratio,
            ),
        )

    def should_suppress(
        self,
        frame_energy: float,
        elapsed_since_tts_start: float,
    ) -> bool:
        self._validate_energy(frame_energy, "frame_energy")
        if not math.isfinite(elapsed_since_tts_start) or elapsed_since_tts_start < 0:
            raise ValueError("elapsed_since_tts_start must be finite and non-negative")

        threshold = self.steady_state_threshold
        if elapsed_since_tts_start < self.startup_window_seconds:
            threshold = min(1.0, threshold * self.startup_energy_multiplier)
        return frame_energy <= threshold
