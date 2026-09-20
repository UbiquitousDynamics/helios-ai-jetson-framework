from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from api.conversation_control import ConversationFloorState as S
from api.realtime_conversation import RealtimeBusyError, RealtimeConversationController, ResponseEvent as R, SpeechStopSignal
from api.transcripts import AuthoritativeUtterance
from recognizer.speech_recognizer import RecognitionResult
from recognizer.turn_endpoint_detector import EndpointAction as A, TurnEndpointConfig


def controller(clock=lambda: 0.0, **kwargs):
    return RealtimeConversationController(endpointing=TurnEndpointConfig(), activity_energy=0.08, clock=clock, **kwargs)


def result(text="synthetic", final=False, revision=1):
    return RecognitionResult(text, final, capture_id=1, segment_id=1, revision=revision)


def test_speech_stop_view_observes_external_and_local_cancellation_without_workers():
    external = threading.Event()
    stop = SpeechStopSignal(external.is_set)
    assert not stop.wait(0)
    external.set()
    assert stop.wait(0)
    external.clear()
    stop.set()
    assert stop.is_set()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), "secret"])
def test_speech_stop_wait_rejects_unbounded_invalid_input(timeout):
    with pytest.raises(ValueError):
        SpeechStopSignal(lambda: False).wait(timeout)


def test_complete_floor_lifecycle_and_generation_eof_before_playback_drain():
    now = [0.0]
    control = controller(clock=lambda: now[0])
    assert control.snapshot().floor.state is S.IDLE
    with control.capture() as lease:
        assert control.snapshot().floor.state is S.ARMED
        control.observe(result())
        assert lease.on_frame(result(), 0.1) is A.CONTINUE
        assert control.snapshot().floor.state is S.USER_SPEAKING
        now[0] = 0.4
        assert lease.on_frame(None, 0) is A.PAUSE
        assert control.snapshot().floor.state is S.USER_PAUSED
        now[0] = 0.5
        control.observe(result("revised", revision=2))
        lease.on_frame(result("revised", revision=2), 0.1)
        assert control.snapshot().floor.state is S.USER_SPEAKING
    assert not control.snapshot().capture_active
    final = control.observe(result("final wording", True, 3))
    assert isinstance(final, AuthoritativeUtterance)
    assert control.snapshot().floor.state is S.FINALIZING
    response = control.begin_response()
    emit = control.observer(response)
    emit(R.SYNTHESIS_STARTED)
    assert control.snapshot().synthesizing
    emit(R.PLAYBACK_STARTED)
    emit(R.GENERATION_COMPLETED)
    assert control.snapshot().generation_complete
    assert control.snapshot().floor.state is S.ASSISTANT_SPEAKING
    emit(R.SYNTHESIS_COMPLETED)
    emit(R.PLAYBACK_COMPLETED)
    assert control.snapshot().floor.state is S.THINKING
    control.finish_response(response)
    assert control.snapshot().floor.state is S.ARMED
    assert control.snapshot().response_id is None


@pytest.mark.parametrize("initial_response", [False, True])
@pytest.mark.parametrize("competing_response", [False, True])
def test_capture_has_one_owner_across_threads(initial_response, competing_response):
    control = controller()
    with control.capture(response=initial_response) as lease:
        def contender():
            with pytest.raises(RealtimeBusyError):
                with control.capture(response=competing_response):
                    pytest.fail("second capture acquired")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(contender).result(timeout=2)
        assert not lease.finished.is_set()
    assert lease.finished.is_set()
    with control.capture(response=competing_response):
        assert control.snapshot().capture_active


def test_capture_failure_clears_pending_and_allows_reuse():
    control = controller()
    with pytest.raises(OSError):
        with control.capture():
            control.observe(result("private-content"))
            raise OSError("synthetic capture failure")
    assert control.transcripts.provisional() is None
    assert not control.snapshot().capture_active
    with control.capture():
        pass


@pytest.mark.parametrize("stage", list(R))
def test_late_events_cannot_change_new_response_or_restart_stopped_floor(stage):
    control = controller()
    old = control.begin_response()
    control.interruption()
    control.final_speech()
    current = control.begin_response()
    before = control.snapshot()
    control.response_event(old, stage)
    control.finish_response(old, failed=True)
    assert control.snapshot() == before
    control.stop()
    stopped = control.snapshot()
    control.response_event(current, stage)
    control.finish_response(current)
    assert control.snapshot() == stopped


@pytest.mark.parametrize("failure", [R.SYNTHESIS_FAILED, R.PLAYBACK_FAILED])
def test_stage_failure_rejects_racing_playback_events_and_recovers(failure):
    control = controller()
    response = control.begin_response()
    control.response_event(response, failure)
    assert control.snapshot().floor.state is S.INTERRUPTED
    control.response_event(response, R.PLAYBACK_STARTED)
    control.response_event(response, R.PLAYBACK_COMPLETED)
    assert control.snapshot().floor.state is S.INTERRUPTED
    control.finish_response(response, failed=True)
    assert control.snapshot().floor.state is S.ARMED
    assert control.begin_response() > response


def test_candidate_survives_response_completion_then_rejects_to_listening():
    control = controller()
    response = control.begin_response()
    control.candidate()
    control.response_event(response, R.PLAYBACK_STARTED)
    assert control.snapshot().floor.candidate_return_state is S.ASSISTANT_SPEAKING
    control.finish_response(response)
    assert control.snapshot().floor.state is S.BARGE_IN_CANDIDATE
    control.candidate(rejected=True)
    assert control.snapshot().floor.state is S.ARMED


def test_stop_cancels_capture_before_releasing_ownership():
    control = controller()
    with control.capture() as lease:
        assert control.stop() is lease
        assert lease.is_set()
        assert not lease.finished.is_set()
        assert lease.on_frame(result(final=True), 0) is A.DISCARD
        assert control.observe(result(final=True)) is None
        with pytest.raises(RealtimeBusyError):
            control.restart()
    with pytest.raises(RealtimeBusyError):
        control.begin_response()
    control.restart()
    with control.capture():
        pass


def test_external_capture_stop_and_dispatch_ownership():
    control = controller()
    external = threading.Event()
    with control.capture(stop=external) as lease:
        with pytest.raises(RealtimeBusyError):
            control.begin_response()
        external.set()
        assert lease.is_set()
    response = control.begin_response()
    with pytest.raises(RealtimeBusyError):
        with control.capture():
            pass
    with pytest.raises(RealtimeBusyError):
        control.begin_response()
    control.finish_response(response)


def test_endpoint_flush_deadline_and_response_idle_capture():
    now = [0.0]
    control = controller(clock=lambda: now[0])
    with control.capture(response=True) as lease:
        now[0] = 100
        assert lease.on_frame(None, 0) is A.CONTINUE
        assert lease.on_frame(result(), 0.1) is A.CONTINUE
        now[0] = 101.3
        assert lease.on_frame(None, 0) is A.REQUEST_FINAL_RESULT
        now[0] = 102.3
        assert lease.on_frame(result(final=True, revision=2), 0) is A.DISCARD
        assert lease.endpoint_ended


@pytest.mark.parametrize("energy", [True, -1, 2, float("inf"), float("nan"), "secret"])
def test_invalid_energy_is_rejected_without_echoing_input(energy):
    with pytest.raises(ValueError) as error:
        RealtimeConversationController(endpointing=TurnEndpointConfig(), activity_energy=energy)
    assert "secret" not in str(error.value)


def test_snapshot_is_immutable_and_content_free():
    control = controller()
    control.observe(result("private-content"))
    snapshot = control.snapshot()
    assert "private-content" not in repr(snapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.capture_active = True
    with pytest.raises(TypeError):
        control.response_event(1, "private-content")
