"""Candidate admission, bounded reversible duck and segment isolation."""

from concurrent.futures import Future

import pytest

import config
from assistant import VoiceAssistant, _BargeInCaptureStop
from recognizer.barge_in_detector import BargeInDetector
from recognizer.echo_suppression_policy import ConservativeEchoSuppressionPolicy
from recognizer.speech_recognizer import RecognitionResult
from test_assistant import FakeAPI, FakeRecognizer, FakeSoundPlayer, FakeTTS, ImmediateExecutor


class CandidateTTS(FakeTTS):
    is_speaking = True
    active_playback_started_at = 10.0
    active_playback_text = "A synthetic unrelated answer"
    paused = False

    def __init__(self):
        super().__init__()
        self.actions = []

    def duck(self):
        self.paused = True
        self.actions.append("duck")
        return True

    def resume(self):
        was_paused = self.paused
        self.paused = False
        if was_paused:
            self.actions.append("resume")
        return was_paused

    def interrupt(self):
        self.paused = False
        self.actions.append("interrupt")
        return True


def event(text, *, final=False, segment=1, capture=1, revision=1, energy=0.2, confidence=None):
    return RecognitionResult(
        text,
        is_final=final,
        capture_id=capture,
        segment_id=segment,
        revision=revision,
        frame_energy=energy,
        confidence=confidence,
        segment_started_at=10.2,
    )


def run_capture(script, *, now=None, **settings):
    now = now if now is not None else [10.8]
    tts, api, response = CandidateTTS(), FakeAPI(), Future()

    class Recognizer(FakeRecognizer):
        def listen_events(self, timeout=None, *, stop_event):
            yield from script(tts, api, stop_event)

    assistant = VoiceAssistant(
        settings=config.Settings(**settings),
        tts=tts,
        api_client=api,
        speech_recognizer=Recognizer([]),
        sound_player=FakeSoundPlayer(),
        sound_executor=ImmediateExecutor(),
        clock=lambda: now[0],
    )
    try:
        result = assistant._listen_for_barge_in(response)
        assert not tts.paused
        return result, list(tts.actions), api.cancelled
    finally:
        response.cancel()
        assistant.close()


@pytest.mark.parametrize("echo", ["A synthetic unrelated answer", "A synthetic unrelated answers"])
def test_echo_revision_resumes_immediately_without_cancelling(echo):
    def script(tts, api, stop):
        yield event("Please change the topic")
        assert tts.paused and not api.cancelled
        yield event(echo, revision=2)
        assert not tts.paused and not api.cancelled
        assert not stop.is_set()

    result, actions, cancelled = run_capture(script)
    assert result is None and not cancelled
    assert actions == ["duck", "resume"]


def test_old_echo_reference_cannot_suppress_a_new_segment():
    def script(tts, api, stop):
        del api, stop
        tts.active_playback_text = "Please change the topic"
        yield event("Please change the topic")
        tts.active_playback_text = "An entirely different answer"
        yield event("Please change the topic", segment=2)
        assert tts.paused
        yield event("Please change the topic", final=True, segment=2, revision=2)

    result, actions, cancelled = run_capture(script)
    assert result == "Please change the topic" and cancelled
    assert actions == ["duck", "interrupt"]


def test_new_segment_releases_prior_duck_even_when_new_event_is_noise():
    def script(tts, api, stop):
        del api, stop
        yield event("Please change the topic")
        assert tts.paused
        yield event("low energy random words", segment=2, energy=0.001)
        assert not tts.paused
        yield event("A different user request", segment=3)
        assert tts.paused
        yield event("A different user request", segment=3, final=True, revision=2)

    result, actions, cancelled = run_capture(script)
    assert result == "A different user request" and cancelled
    assert actions == ["duck", "resume", "duck", "interrupt"]


def test_candidate_revisions_cannot_extend_absolute_duck_deadline():
    now = [10.8]

    def script(tts, api, stop):
        yield event("Please change the topic")
        for revision in (2, 3, 4):
            now[0] += 0.25
            yield event("Please change the topic again", revision=revision)
            assert tts.paused and not api.cancelled
        now[0] = 11.8
        yield event("Please change the topic again", final=True, revision=5)
        assert not tts.paused and not api.cancelled
        assert not stop.is_set()

    result, actions, cancelled = run_capture(
        script,
        now=now,
        barge_in_candidate_inactivity_seconds=0.5,
        barge_in_candidate_maximum_seconds=1.0,
    )
    assert result is None and not cancelled
    assert actions == ["duck", "resume"]


def test_absolute_duck_deadline_is_checked_without_recognition_events():
    now = [0.0]
    stop = _BargeInCaptureStop(
        clock=lambda: now[0],
        follow_up_timeout_seconds=6.5,
        candidate_inactivity_seconds=1,
        candidate_maximum_seconds=2,
    )
    stop.candidate_activity()
    now[0] = 0.9
    stop.candidate_activity()
    now[0] = 1.8
    stop.candidate_activity()
    now[0] = 1.999
    assert not stop.is_set()
    now[0] = 2
    assert stop.is_set()
    assert stop.consume_candidate_timeout()
    assert not stop.is_set()
    stop.candidate_activity()
    assert not stop.is_set()


def test_expired_segment_cannot_rearm_or_confirm_until_a_new_segment():
    now = [10.8]

    def script(tts, api, stop):
        del stop
        yield event("Please change the topic")
        now[0] = 11.8
        yield event("Please change the topic again", revision=2)
        assert not tts.paused
        yield event("Please change the topic once more", revision=3)
        assert not tts.paused and not api.cancelled
        yield event("Please change the topic once more", final=True, revision=4)
        assert not api.cancelled
        yield event("A fresh user utterance", segment=2)
        assert tts.paused
        yield event("A fresh user utterance", segment=2, final=True, revision=2)

    result, actions, cancelled = run_capture(
        script,
        now=now,
        barge_in_candidate_inactivity_seconds=0.5,
        barge_in_candidate_maximum_seconds=1,
    )
    assert result == "A fresh user utterance" and cancelled
    assert actions == ["duck", "resume", "duck", "interrupt"]


def test_final_boundary_releases_echo_references_without_segment_metadata():
    def script(tts, api, stop):
        del api, stop
        tts.active_playback_text = "Please change the topic"
        yield RecognitionResult("Please change the topic", is_final=True, frame_energy=0.2)
        tts.active_playback_text = "An entirely different answer"
        yield RecognitionResult("Please change the topic", is_final=False, frame_energy=0.2)
        assert tts.paused
        yield RecognitionResult("Please change the topic", is_final=True, frame_energy=0.2)

    result, actions, cancelled = run_capture(script)
    assert result == "Please change the topic" and cancelled
    assert actions == ["duck", "interrupt"]


def test_reused_segment_id_in_new_capture_cannot_confirm_old_candidate():
    detector = BargeInDetector(minimum_active_seconds=0)
    assert not detector.process_recognition(
        event("Please change the topic"), elapsed_since_tts_start=1
    )
    assert detector.recognition_candidate_pending
    assert not detector.process_recognition(
        event("Please change the topic", final=True, capture=2),
        elapsed_since_tts_start=2,
    )
    assert not detector.recognition_candidate_pending and not detector.detected


@pytest.mark.parametrize(
    "field",
    [
        "barge_in_minimum_active_seconds",
        "barge_in_minimum_recognition_confidence",
        "barge_in_candidate_inactivity_seconds",
        "barge_in_candidate_maximum_seconds",
        "barge_in_echo_energy_ratio",
        "barge_in_startup_window_seconds",
        "barge_in_startup_energy_multiplier",
    ],
)
@pytest.mark.parametrize("value", [True, "1", float("nan"), float("inf"), -1])
def test_settings_reject_invalid_candidate_thresholds(field, value):
    with pytest.raises(config.ConfigurationError):
        config.Settings(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("barge_in_minimum_partial_words", True),
        ("barge_in_minimum_partial_words", 1.5),
        ("barge_in_minimum_partial_words", 0),
        ("barge_in_minimum_recognition_confidence", 1.1),
        ("barge_in_echo_energy_ratio", 0.9),
        ("barge_in_startup_energy_multiplier", 0.9),
        ("barge_in_candidate_inactivity_seconds", 0),
        ("barge_in_candidate_maximum_seconds", 0),
        ("barge_in_candidate_maximum_seconds", 1),
    ],
)
def test_settings_reject_out_of_range_and_inconsistent_bounds(field, value):
    with pytest.raises(config.ConfigurationError):
        config.Settings(**{field: value})


def test_candidate_environment_thresholds_reach_detector_and_echo_policy():
    settings = config.Settings.from_env(
        environ={
            "HELIOS_BARGE_IN_MINIMUM_ACTIVE_SECONDS": "0.3",
            "HELIOS_BARGE_IN_MINIMUM_PARTIAL_WORDS": "4",
            "HELIOS_BARGE_IN_MINIMUM_RECOGNITION_CONFIDENCE": "0.75",
            "HELIOS_BARGE_IN_CANDIDATE_INACTIVITY_SECONDS": "0.8",
            "HELIOS_BARGE_IN_CANDIDATE_MAXIMUM_SECONDS": "2",
            "HELIOS_BARGE_IN_ECHO_ENERGY_RATIO": "2",
            "HELIOS_BARGE_IN_STARTUP_WINDOW_SECONDS": "0.7",
            "HELIOS_BARGE_IN_STARTUP_ENERGY_MULTIPLIER": "2",
        }
    )
    assistant = VoiceAssistant(
        settings=settings,
        tts=FakeTTS(),
        api_client=FakeAPI(),
        speech_recognizer=FakeRecognizer([]),
        sound_player=FakeSoundPlayer(),
        sound_executor=ImmediateExecutor(),
    )
    try:
        detector = assistant._barge_in_detector
        assert detector.minimum_active_seconds == 0.3
        assert detector.minimum_partial_words == 4
        assert detector.minimum_recognition_confidence == 0.75
        assert settings.barge_in_candidate_inactivity_seconds == 0.8
        assert settings.barge_in_candidate_maximum_seconds == 2
        assert detector.suppression_policy.should_suppress(0.12, 0.6)
        assert not detector.suppression_policy.should_suppress(0.12, 0.7)
    finally:
        assistant.close()


@pytest.mark.parametrize(
    "factory,field",
    [
        (BargeInDetector, "sample_rate"),
        (BargeInDetector, "energy_threshold"),
        (BargeInDetector, "minimum_active_seconds"),
        (BargeInDetector, "recognition_event_energy"),
        (BargeInDetector, "minimum_recognition_confidence"),
        (ConservativeEchoSuppressionPolicy, "expected_echo_energy"),
        (ConservativeEchoSuppressionPolicy, "minimum_interrupt_energy"),
        (ConservativeEchoSuppressionPolicy, "echo_energy_ratio"),
        (ConservativeEchoSuppressionPolicy, "startup_window_seconds"),
        (ConservativeEchoSuppressionPolicy, "startup_energy_multiplier"),
    ],
)
@pytest.mark.parametrize("value", [True, "1", float("nan")])
def test_direct_detector_and_policy_constructors_validate_types(factory, field, value):
    with pytest.raises(ValueError):
        factory(**{field: value})
