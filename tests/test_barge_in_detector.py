from __future__ import annotations

from array import array

import pytest

from recognizer.barge_in_detector import BargeInDetector, pcm16_rms
from recognizer.echo_suppression_policy import ConservativeEchoSuppressionPolicy
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


class ThresholdPolicy:
    def __init__(self, threshold: float) -> None:
        self.threshold = threshold
        self.calls: list[tuple[float, float]] = []

    def should_suppress(
        self,
        frame_energy: float,
        elapsed_since_tts_start: float,
    ) -> bool:
        self.calls.append((frame_energy, elapsed_since_tts_start))
        return frame_energy < self.threshold


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


def test_partial_recognition_requires_two_sustained_updates() -> None:
    policy = RecordingPolicy(suppress=False)
    detector = BargeInDetector(
        minimum_active_seconds=0.12,
        suppression_policy=policy,
    )

    assert not detector.process_recognition(
        RecognitionResult("hello from user", is_final=False, segment_id=1),
        frame_energy=0.12,
        elapsed_since_tts_start=0.6,
    )
    assert detector.recognition_candidate_pending
    assert detector.recognition_candidate_segment_id == 1
    assert not detector.process_recognition(
        RecognitionResult("hello from the user", is_final=False, segment_id=1),
        frame_energy=0.12,
        elapsed_since_tts_start=0.7,
    )
    assert detector.process_recognition(
        RecognitionResult("hello from the user again", is_final=False, segment_id=1),
        frame_energy=0.12,
        elapsed_since_tts_start=0.73,
    )
    assert policy.calls == [(0.12, 0.6), (0.12, 0.7), (0.12, 0.73)]


def test_recognition_event_uses_configured_nominal_energy_when_pcm_is_unavailable() -> None:
    policy = RecordingPolicy(suppress=False)
    detector = BargeInDetector(
        recognition_event_energy=0.14,
        suppression_policy=policy,
    )

    assert detector.process_recognition(
        RecognitionResult("hello", is_final=True),
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


def test_energy_only_reemit_never_arms_or_confirms_partial() -> None:
    detector = BargeInDetector(minimum_active_seconds=0.0)

    assert not detector.process_recognition(
        RecognitionResult(
            "stale partial words",
            is_final=False,
            segment_id=7,
            energy_reemit=True,
        ),
        frame_energy=0.4,
        elapsed_since_tts_start=0.1,
    )
    assert not detector.recognition_candidate_pending

    assert not detector.process_recognition(
        RecognitionResult(
            "real correction begins",
            is_final=False,
            segment_id=8,
        ),
        frame_energy=0.4,
        elapsed_since_tts_start=0.2,
    )
    assert not detector.process_recognition(
        RecognitionResult(
            "real correction begins",
            is_final=False,
            segment_id=8,
            energy_reemit=True,
        ),
        frame_energy=0.7,
        elapsed_since_tts_start=0.3,
    )
    assert not detector.detected


def test_new_recognition_segment_must_be_confirmed_independently() -> None:
    detector = BargeInDetector(minimum_active_seconds=0.0)

    assert not detector.process_recognition(
        RecognitionResult("first segment words", is_final=False, segment_id=1),
        elapsed_since_tts_start=0.1,
    )
    assert not detector.process_recognition(
        RecognitionResult("second segment words", is_final=False, segment_id=2),
        elapsed_since_tts_start=0.2,
    )
    assert detector.process_recognition(
        RecognitionResult("second segment words grow", is_final=False, segment_id=2),
        elapsed_since_tts_start=0.3,
    )


def test_short_partial_waits_for_final_even_after_confirmation_interval() -> None:
    detector = BargeInDetector(
        minimum_active_seconds=0.0,
        minimum_partial_words=3,
        suppression_policy=ThresholdPolicy(0.1),
    )

    assert not detector.process_recognition(
        RecognitionResult("no", is_final=False, frame_energy=0.2, segment_id=1),
        frame_energy=0.2,
        elapsed_since_tts_start=0.1,
    )
    assert not detector.process_recognition(
        RecognitionResult("no basta", is_final=False, frame_energy=0.2, segment_id=1),
        frame_energy=0.2,
        elapsed_since_tts_start=0.3,
    )
    assert detector.process_recognition(
        RecognitionResult("no basta", is_final=True, frame_energy=0.01, segment_id=1),
        frame_energy=0.01,
        elapsed_since_tts_start=0.4,
    )


def test_low_energy_final_without_an_armed_partial_is_suppressed() -> None:
    detector = BargeInDetector(
        minimum_active_seconds=0.0,
        suppression_policy=ThresholdPolicy(0.1),
    )

    assert not detector.process_recognition(
        RecognitionResult("no basta", is_final=True, frame_energy=0.01, segment_id=1),
        frame_energy=0.01,
        elapsed_since_tts_start=0.4,
    )


def test_partial_duration_uses_absolute_observation_time_across_tts_epochs() -> None:
    detector = BargeInDetector(minimum_active_seconds=0.12)

    assert not detector.process_recognition(
        RecognitionResult("correction begins now", is_final=False, segment_id=1),
        elapsed_since_tts_start=4.0,
        observed_at=100.0,
    )
    assert detector.process_recognition(
        RecognitionResult("correction begins right now", is_final=False, segment_id=1),
        elapsed_since_tts_start=0.3,
        observed_at=100.15,
    )


def test_final_in_new_tts_startup_window_inherits_accepted_pending_partial() -> None:
    detector = BargeInDetector(
        minimum_active_seconds=0.0,
        suppression_policy=ConservativeEchoSuppressionPolicy(),
    )

    # 0.08 is above the steady-state threshold (0.06), but below the next
    # fragment's startup threshold (0.09).
    assert not detector.process_recognition(
        RecognitionResult("no cambia", is_final=False, segment_id=7),
        frame_energy=0.08,
        elapsed_since_tts_start=1.0,
        observed_at=100.0,
    )
    assert detector.recognition_candidate_pending
    assert detector.process_recognition(
        RecognitionResult("no cambia argomento", is_final=True, segment_id=7),
        frame_energy=0.01,
        elapsed_since_tts_start=0.2,
        observed_at=100.2,
    )


def test_pcm_frame_rejects_partial_sample() -> None:
    with pytest.raises(ValueError, match="complete 16-bit samples"):
        pcm16_rms(b"\x00")


def test_detector_rejects_negative_elapsed_time() -> None:
    detector = BargeInDetector(minimum_active_seconds=0.0)

    with pytest.raises(ValueError, match="elapsed_since_tts_start"):
        detector.process_frame(pcm_frame(8_192), elapsed_since_tts_start=-0.1)
