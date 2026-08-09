from __future__ import annotations

from array import array

import pytest

from recognizer.barge_in_detector import BargeInDetector, pcm16_rms
from recognizer.speech_recognizer import RecognitionResult


def pcm_frame(amplitude: int, samples: int = 1_600) -> bytes:
    return array("h", [amplitude] * samples).tobytes()


class RecordingPolicy:
    def __init__(self, *, suppress: bool) -> None:
        self.suppress = suppress
        self.calls: list[tuple[float, float]] = []

    def should_suppress(
        self,
        frame_energy: float,
        elapsed_since_tts_start: float,
    ) -> bool:
        self.calls.append((frame_energy, elapsed_since_tts_start))
        return self.suppress


def test_pcm_energy_distinguishes_silence_from_signal() -> None:
    assert pcm16_rms(pcm_frame(0)) == 0.0
    assert pcm16_rms(pcm_frame(8_192)) == pytest.approx(0.25)


def test_sustained_signal_detects_once_and_reset_rearms() -> None:
    detector = BargeInDetector(
        sample_rate=16_000,
        energy_threshold=0.1,
        minimum_active_seconds=0.15,
    )
    silence = pcm_frame(0)
    signal = pcm_frame(8_192)

    assert not detector.process_frame(silence, elapsed_since_tts_start=0.1)
    assert not detector.process_frame(signal, elapsed_since_tts_start=0.2)
    assert detector.process_frame(signal, elapsed_since_tts_start=0.3)
    assert detector.detected
    assert not detector.process_frame(signal, elapsed_since_tts_start=0.4)

    detector.reset()
    assert not detector.detected
    assert not detector.process_frame(signal, elapsed_since_tts_start=0.5)
    assert detector.process_frame(signal, elapsed_since_tts_start=0.6)


def test_silence_resets_accumulated_signal() -> None:
    detector = BargeInDetector(
        energy_threshold=0.1,
        minimum_active_seconds=0.15,
    )
    signal = pcm_frame(8_192)

    assert not detector.process_frame(signal, elapsed_since_tts_start=0.1)
    assert not detector.process_frame(pcm_frame(0), elapsed_since_tts_start=0.2)
    assert not detector.process_frame(signal, elapsed_since_tts_start=0.3)


def test_injected_policy_is_consulted_and_can_veto_detection() -> None:
    policy = RecordingPolicy(suppress=True)
    detector = BargeInDetector(
        energy_threshold=0.1,
        minimum_active_seconds=0.0,
        suppression_policy=policy,
    )

    assert not detector.process_frame(pcm_frame(8_192), elapsed_since_tts_start=0.25)
    assert len(policy.calls) == 1
    assert policy.calls[0][0] == pytest.approx(0.25)
    assert policy.calls[0][1] == 0.25
    assert not detector.detected


def test_nonempty_recognition_event_is_a_candidate() -> None:
    policy = RecordingPolicy(suppress=False)
    detector = BargeInDetector(suppression_policy=policy)

    assert detector.process_recognition(
        RecognitionResult("  hello  ", is_final=False),
        frame_energy=0.12,
        elapsed_since_tts_start=0.6,
    )
    assert policy.calls == [(0.12, 0.6)]


def test_recognition_event_uses_configured_nominal_energy_when_pcm_is_unavailable() -> None:
    policy = RecordingPolicy(suppress=False)
    detector = BargeInDetector(
        recognition_event_energy=0.14,
        suppression_policy=policy,
    )

    assert detector.process_recognition(
        RecognitionResult("hello", is_final=False),
        elapsed_since_tts_start=0.6,
    )
    assert policy.calls == [(0.14, 0.6)]


def test_empty_recognition_event_is_ignored() -> None:
    policy = RecordingPolicy(suppress=False)
    detector = BargeInDetector(suppression_policy=policy)

    assert not detector.process_recognition(
        RecognitionResult(" ", is_final=True),
        elapsed_since_tts_start=1.0,
    )
    assert policy.calls == []


def test_pcm_frame_rejects_partial_sample() -> None:
    with pytest.raises(ValueError, match="complete 16-bit samples"):
        pcm16_rms(b"\x00")


def test_detector_rejects_negative_elapsed_time() -> None:
    detector = BargeInDetector(minimum_active_seconds=0.0)

    with pytest.raises(ValueError, match="elapsed_since_tts_start"):
        detector.process_frame(pcm_frame(8_192), elapsed_since_tts_start=-0.1)
