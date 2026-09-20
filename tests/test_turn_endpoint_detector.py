from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace

import pytest

import config
from api.transcripts import TranscriptBoundaryError, TranscriptSegment, authoritative_text
from recognizer.speech_recognizer import RecognitionResult
from recognizer.turn_endpoint_detector import (
    EndpointAction as A,
    EndpointObservation as Observation,
    EndpointState as S,
    TurnEndpointConfig,
    TurnEndpointDetector,
)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def setup_detector():
    clock = Clock()
    policy = TurnEndpointConfig(
        short_pause_seconds=0.5, finalization_seconds=2,
        inactivity_seconds=5, maximum_utterance_seconds=8,
        revision_stability_seconds=0.5, final_result_timeout_seconds=1,
    )
    return TurnEndpointDetector(policy, clock=clock), clock


def speech(**kwargs):
    return Observation(speech_active=True, has_text=True, **kwargs)


def test_thinking_pause_resumed_speech_and_long_pause(setup_detector):
    detector, clock = setup_detector
    assert detector.update(speech()).action is A.CONTINUE
    clock.now = 0.499
    assert detector.update().action is A.CONTINUE
    clock.now = 0.5
    assert detector.update().action is A.PAUSE
    assert detector.snapshot().state is S.PAUSED
    clock.now = 1
    assert detector.update(speech()).action is A.CONTINUE
    assert detector.snapshot().state is S.SPEAKING
    clock.now = 2.999
    assert detector.update().action is A.PAUSE
    clock.now = 3
    decision = detector.update()
    assert decision.action is A.REQUEST_FINAL_RESULT
    assert decision.reason == "stable_pause"
    assert detector.snapshot().state is S.FINALIZING
    assert detector.snapshot().final_result_deadline == 4
    assert detector.update().action is A.NONE


def test_recent_revision_delays_flush_but_energy_reemit_does_not(setup_detector):
    detector, clock = setup_detector
    segment = TranscriptSegment(1, 1)
    detector.update(speech(segment=segment, revision=1))
    clock.now = 1.9
    detector.update(Observation(has_text=True, segment=segment, revision=2))
    clock.now = 2
    assert detector.update().action is A.PAUSE
    clock.now = 2.2
    detector.update(Observation(has_text=True, segment=segment, revision=3, energy_reemit=True))
    assert detector.snapshot().stable_since == 1.9
    clock.now = 2.5
    assert detector.update().action is A.REQUEST_FINAL_RESULT


@pytest.mark.parametrize("metadata", [dict(confidence=0.1), dict(speech_duration_seconds=0.01)])
def test_low_confidence_or_short_speech_waits_for_inactivity_bound(setup_detector, metadata):
    detector, clock = setup_detector
    detector.update(speech(**metadata))
    clock.now = 2
    assert detector.update().action is A.PAUSE
    clock.now = 5
    decision = detector.update()
    assert decision.action is A.REQUEST_FINAL_RESULT
    assert decision.reason == "inactivity"


def test_noisy_silence_does_not_start_utterance_or_postpone_inactivity(setup_detector):
    detector, clock = setup_detector
    for revision in range(1, 6):
        clock.now = revision - 1
        decision = detector.update(Observation(
            has_text=True, confidence=0.1, speech_duration_seconds=0.01, revision=revision
        ))
        assert decision.action is A.CONTINUE
        assert detector.snapshot().state is S.ARMED
    clock.now = 4.999
    assert detector.update().action is A.CONTINUE
    clock.now = 5
    assert detector.update().action is A.EXPIRE
    assert detector.update(speech()).action is A.NONE


def test_confident_timed_text_can_start_without_current_vad_activity(setup_detector):
    detector, clock = setup_detector
    detector.update(Observation(has_text=True, confidence=0.9, speech_duration_seconds=0.8))
    assert detector.snapshot().started_at == 0
    assert detector.snapshot().state is S.SPEAKING
    clock.now = 2
    assert detector.update().action is A.REQUEST_FINAL_RESULT


@pytest.mark.parametrize("has_text", [False, True])
def test_maximum_duration_bounds_continuous_activity(setup_detector, has_text):
    detector, clock = setup_detector
    for moment in (0, 1, 3, 5, 7.999):
        clock.now = moment
        assert detector.update(Observation(speech_active=True, has_text=has_text)).action is A.CONTINUE
    clock.now = 8
    decision = detector.update(Observation(speech_active=True, has_text=has_text))
    assert (decision.action, decision.reason) == (A.REQUEST_FINAL_RESULT, "maximum_utterance")
    clock.now = 8.999
    assert detector.update().action is A.NONE
    clock.now = 9
    decision = detector.update()
    assert (decision.action, decision.reason) == (A.DISCARD, "final_result_timeout")


@pytest.mark.parametrize("during_flush", [False, True])
def test_explicit_final_is_emitted_exactly_once(setup_detector, during_flush):
    detector, clock = setup_detector
    detector.update(speech())
    if during_flush:
        clock.now = 2
        assert detector.update().action is A.REQUEST_FINAL_RESULT
    decision = detector.update(Observation(has_text=True, is_final=True))
    assert decision.action is A.FINALIZE
    for _ in range(100):
        assert detector.update(Observation(has_text=True, is_final=True)).action is A.NONE
    assert detector.snapshot().state is S.ENDED


@pytest.mark.parametrize("final_at,expected", [(2.999, A.FINALIZE), (3.0, A.DISCARD), (3.001, A.DISCARD)])
def test_final_result_timeout_has_exact_boundary(setup_detector, final_at, expected):
    detector, clock = setup_detector
    detector.update(speech())
    clock.now = 2
    detector.update()
    clock.now = final_at
    assert detector.update(Observation(has_text=True, is_final=True)).action is expected


def test_empty_final_and_partial_timeout_never_create_transcript_authority(setup_detector):
    detector, clock = setup_detector
    detector.update(speech())
    clock.now = 2
    flush = detector.update()
    assert flush.action is A.REQUEST_FINAL_RESULT
    with pytest.raises(TranscriptBoundaryError):
        authoritative_text(flush)
    decision = detector.update(Observation(is_final=True))
    assert (decision.action, decision.reason) == (A.DISCARD, "empty_final")


@pytest.mark.parametrize("segment,revision", [(TranscriptSegment(1, 9), 9), (TranscriptSegment(2, 1), 9), (TranscriptSegment(2, 2), 1)])
def test_stale_observation_cannot_finalize_or_extend_activity(setup_detector, segment, revision):
    detector, clock = setup_detector
    detector.update(speech(segment=TranscriptSegment(2, 2), revision=2))
    clock.now = 1
    assert detector.update(Observation(
        speech_active=True, has_text=True, is_final=True, segment=segment, revision=revision
    )).action is A.PAUSE
    assert detector.snapshot().last_activity_at == 0
    clock.now = 2
    assert detector.update().action is A.REQUEST_FINAL_RESULT


def test_recognizer_adapter_keeps_metadata_but_never_text(setup_detector, caplog):
    detector, _ = setup_detector
    raw = RecognitionResult(
        "private synthetic content", False, capture_id=2, segment_id=1, revision=3,
        confidence=0.9, speech_duration_seconds=0.8, energy_reemit=True,
    )
    observation = Observation.from_recognition(raw, speech_active=True)
    assert observation.segment == TranscriptSegment(2, 1)
    assert observation.revision == 3
    assert observation.energy_reemit is True
    assert observation.confidence == 0.9 and observation.speech_duration_seconds == 0.8
    detector.update(observation)
    assert "private synthetic content" not in repr(observation)
    assert "private synthetic content" not in repr(vars(detector))
    assert "private synthetic content" not in repr(detector.snapshot()) + caplog.text
    with pytest.raises(FrozenInstanceError):
        observation.has_text = False


@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan"), "secret-invalid-clock"])
def test_bad_clock_is_rejected_without_changing_snapshot(setup_detector, value):
    detector, clock = setup_detector
    before = detector.snapshot()
    clock.now = value
    with pytest.raises(ValueError):
        detector.update(speech())
    assert detector.snapshot() == before
    clock.now = 1
    assert detector.update(speech()).action is A.CONTINUE
    before = detector.snapshot()
    clock.now = 0.9
    with pytest.raises(ValueError, match="backwards"):
        detector.update()
    assert detector.snapshot() == before


@pytest.mark.parametrize("name", [f.name for f in fields(TurnEndpointConfig)])
@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan"), "secret-invalid-bound"])
def test_invalid_configuration_numbers(name, value):
    with pytest.raises(ValueError) as error:
        replace(TurnEndpointConfig(), **{name:value})
    assert "secret-invalid-bound" not in str(error.value)


@pytest.mark.parametrize("kwargs", [
    dict(short_pause_seconds=0), dict(short_pause_seconds=1.2),
    dict(inactivity_seconds=1), dict(maximum_utterance_seconds=1),
    dict(final_result_timeout_seconds=0), dict(minimum_confidence=1.01),
])
def test_inconsistent_configuration_bounds(kwargs):
    with pytest.raises(ValueError):
        TurnEndpointConfig(**kwargs)


@pytest.mark.parametrize("kwargs", [
    dict(speech_active=1), dict(has_text="secret"), dict(is_final=1),
    dict(energy_reemit=1), dict(segment=(1, 1)), dict(revision=True),
    dict(revision=0), dict(confidence=1.01), dict(confidence=float("nan")),
    dict(speech_duration_seconds=-1),
])
def test_malformed_observations_are_rejected(kwargs):
    with pytest.raises((TypeError, ValueError)):
        Observation(**kwargs)


def test_settings_configure_endpoint_policy_without_runtime_activation():
    settings = config.Settings.from_env(environ={
        "HELIOS_ENDPOINT_SHORT_PAUSE_SECONDS":"0.5",
        "HELIOS_ENDPOINT_FINALIZATION_SECONDS":"2",
        "HELIOS_ENDPOINT_INACTIVITY_SECONDS":"6",
        "HELIOS_ENDPOINT_MAXIMUM_UTTERANCE_SECONDS":"15",
        "HELIOS_ENDPOINT_REVISION_STABILITY_SECONDS":"0.75",
        "HELIOS_ENDPOINT_FINAL_RESULT_TIMEOUT_SECONDS":"0.5",
        "HELIOS_ENDPOINT_MINIMUM_CONFIDENCE":"0.7",
        "HELIOS_ENDPOINT_MINIMUM_SPEECH_SECONDS":"0.1",
    })
    assert settings.endpointing == TurnEndpointConfig(0.5, 2, 6, 15, 0.75, 0.5, 0.7, 0.1)
    assert settings.listen_timeout == config.Settings().listen_timeout


@pytest.mark.parametrize("value", ["nan", "-1", "secret-invalid-bound"])
def test_invalid_endpoint_environment_fails_without_printing_values(value):
    with pytest.raises(config.ConfigurationError, match="invalid turn endpoint") as error:
        config.Settings.from_env(environ={"HELIOS_ENDPOINT_FINALIZATION_SECONDS":value})
    assert value not in str(error.value)
    assert error.value.__suppress_context__
    with pytest.raises(config.ConfigurationError):
        config.Settings(endpointing={})
