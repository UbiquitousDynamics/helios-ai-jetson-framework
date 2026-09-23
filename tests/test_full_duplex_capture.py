from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import config
from api.conversation_control import ConversationFloorState as S
from api.realtime_conversation import ResponseEvent as R
from assistant import VoiceAssistant, _BargeInCaptureStop
from recognizer.speech_recognizer import RecognitionResult
from test_assistant import FakeAPI, FakeRecognizer, FakeSoundPlayer, FakeTTS, ImmediateExecutor


class NeverInterrupt:
    def reset(self):
        pass

    def process_recognition(self, _result, **_kwargs):
        return False


class PhaseRecognizer(FakeRecognizer):
    def __init__(self, phases, acknowledged):
        super().__init__([RecognitionResult("Emilia synthetic request", True)])
        self.phases, self.acknowledged = phases, acknowledged
        self.captures = 0
        self.observed = []
        self.owner = None

    def listen_events(self, timeout, *, stop_event, keep_open, reset_event, on_frame, on_segment_reset):
        assert timeout is None and keep_open
        self.captures += 1
        deadline = time.monotonic() + 3
        while not stop_event.is_set():
            assert time.monotonic() < deadline
            try:
                phase, expected = self.phases.get(timeout=0.01)
            except queue.Empty:
                continue
            assert self.owner.realtime.snapshot().floor.state is expected
            event = RecognitionResult("synthetic hypothesis " + phase, False,
                                      capture_id=2, segment_id=1, revision=len(self.observed) + 1)
            on_frame(event, 0.1)
            yield event
            self.observed.append(phase)
            self.acknowledged.set()


def make_runtime(recognizer, api, tts=None, detector=None):
    runtime = VoiceAssistant(settings=config.Settings(), speech_recognizer=recognizer,
                             api_client=api, tts=tts or FakeTTS(), sound_player=FakeSoundPlayer(),
                             sound_executor=ImmediateExecutor(), barge_in_detector=detector or NeverInterrupt())
    recognizer.owner = runtime
    return runtime


@pytest.mark.parametrize("entry", ["voice", "public_command"])
def test_one_capture_processes_input_during_all_response_phases(entry):
    phases, acknowledged = queue.Queue(maxsize=1), threading.Event()
    recognizer = PhaseRecognizer(phases, acknowledged)

    class API(FakeAPI):
        def talk(self, message, context=None, *, on_lifecycle, **_kwargs):
            self.messages.append(message)
            for phase, events, expected in [
                ("generation", (), S.THINKING),
                ("synthesis", (R.SYNTHESIS_STARTED,), S.THINKING),
                ("playback", (R.SYNTHESIS_COMPLETED, R.PLAYBACK_STARTED), S.ASSISTANT_SPEAKING),
                ("gap", (R.PLAYBACK_COMPLETED,), S.THINKING),
                ("provider_eof", (R.GENERATION_COMPLETED,), S.THINKING),
                ("last_audio", (R.PLAYBACK_STARTED,), S.ASSISTANT_SPEAKING),
            ]:
                for event in events:
                    on_lifecycle(event)
                acknowledged.clear()
                phases.put((phase, expected), timeout=1)
                assert acknowledged.wait(timeout=1)
            on_lifecycle(R.PLAYBACK_COMPLETED)
            return "answer"

    api = API()
    runtime = make_runtime(recognizer, api)
    try:
        if entry == "voice":
            assert runtime.run_once()
        else:
            assert runtime.process_command("Emilia synthetic request") == "answer"
        assert api.messages == ["synthetic request"]
        assert recognizer.captures == 1
        assert recognizer.observed == ["generation", "synthesis", "playback", "gap", "provider_eof", "last_audio"]
        assert not runtime.realtime.snapshot().capture_active
    finally:
        runtime.close()


def test_rag_retrieval_and_speech_share_response_time_capture():
    phases, acknowledged = queue.Queue(maxsize=1), threading.Event()
    recognizer = PhaseRecognizer(phases, acknowledged)

    def phase(name, state):
        acknowledged.clear()
        phases.put((name, state), timeout=1)
        assert acknowledged.wait(timeout=1)

    class Search:
        def run(self, **_kwargs):
            phase("retrieval", S.THINKING)
            return "synthetic retrieval result"

    class TTS(FakeTTS):
        def speak(self, text):
            phase("playback", S.ASSISTANT_SPEAKING)
            super().speak(text)

    runtime = make_runtime(recognizer, FakeAPI(), TTS())
    try:
        assert runtime.process_rag_command("synthetic query", Search()) == "synthetic retrieval result"
        assert recognizer.captures == 1
        assert recognizer.observed == ["retrieval", "playback"]
        assert not runtime.realtime.snapshot().capture_active
    finally:
        runtime.close()


@pytest.mark.parametrize("phase", ["generation", "synthesis", "playback", "gap", "provider_eof"])
def test_stop_unwinds_capture_and_response_at_each_phase(phase):
    phases, acknowledged = queue.Queue(maxsize=1), threading.Event()
    recognizer = PhaseRecognizer(phases, acknowledged)
    ready = threading.Event()

    class API(FakeAPI):
        def talk(self, message, context=None, *, cancellation, on_lifecycle, **_kwargs):
            if phase == "synthesis":
                on_lifecycle(R.SYNTHESIS_STARTED)
            if phase in {"playback", "gap"}:
                on_lifecycle(R.PLAYBACK_STARTED)
            if phase == "gap":
                on_lifecycle(R.PLAYBACK_COMPLETED)
            if phase == "provider_eof":
                on_lifecycle(R.GENERATION_COMPLETED)
            phases.put((phase, S.ASSISTANT_SPEAKING if phase == "playback" else S.THINKING), timeout=1)
            assert acknowledged.wait(timeout=1)
            ready.set()
            deadline = time.monotonic() + 2
            while not cancellation.cancelled:
                assert time.monotonic() < deadline
                threading.Event().wait(0.005)
            raise RuntimeError("synthetic cancellation")

    runtime = make_runtime(recognizer, API())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.run_once)
        try:
            assert ready.wait(timeout=2)
            runtime.stop()
            with pytest.raises(RuntimeError):
                future.result(timeout=2)
            assert recognizer.observed == [phase]
            assert not runtime.realtime.snapshot().capture_active
        finally:
            runtime.stop()
            runtime.close()
    assert runtime._close_complete.is_set()


def test_candidate_can_finalize_after_response_eof_but_has_bounded_inactivity():
    now = [0.0]
    stop = _BargeInCaptureStop(clock=lambda: now[0], follow_up_timeout_seconds=5)
    stop.candidate_activity()
    stop.response_finished()
    assert not stop.is_set()
    now[0] = 1.5
    assert stop.is_set()
    assert stop.consume_candidate_timeout()


def test_stop_during_rag_retrieval_suppresses_speech_without_cancelling_retrieval():
    retrieving, release = threading.Event(), threading.Event()

    class Search:
        def run(self, **_kwargs):
            retrieving.set()
            assert release.wait(timeout=2)
            return "synthetic result after cancellation"

    class TTS(FakeTTS):
        def interrupt(self):
            release.set()

    class Recognizer(FakeRecognizer):
        def listen_events(self, timeout, *, stop_event):
            assert retrieving.wait(timeout=1)
            yield RecognitionResult("stop", True, frame_energy=0.5, segment_peak_energy=0.5,
                                    confidence=0.9, speech_duration_seconds=0.5)

    # Real interruption admission must still validate this explicit final.
    tts, api, recognizer = TTS(), FakeAPI(), Recognizer([])
    runtime = VoiceAssistant(settings=config.Settings(), speech_recognizer=recognizer,
                             api_client=api, tts=tts, sound_player=FakeSoundPlayer(),
                             sound_executor=ImmediateExecutor())
    try:
        assert runtime.process_rag_command("synthetic query", Search()) == "synthetic result after cancellation"
        assert tts.spoken == [] and api.messages == []
        assert not api.cancelled
        assert not runtime.realtime.snapshot().capture_active
    finally:
        release.set()
        runtime.close()


def test_synthesis_failure_unwinds_capture_and_allows_next_response():
    phases, acknowledged = queue.Queue(maxsize=1), threading.Event()
    recognizer = PhaseRecognizer(phases, acknowledged)

    class API(FakeAPI):
        def talk(self, message, context=None, *, on_lifecycle, **_kwargs):
            self.messages.append(message)
            if len(self.messages) == 1:
                on_lifecycle(R.SYNTHESIS_STARTED)
                phases.put(("synthesis", S.THINKING), timeout=1)
                assert acknowledged.wait(timeout=1)
                on_lifecycle(R.SYNTHESIS_FAILED)
                raise RuntimeError("synthetic synthesis failure")
            return "recovered"

    api = API()
    runtime = make_runtime(recognizer, api)
    try:
        with pytest.raises(RuntimeError, match="synthetic synthesis failure"):
            runtime.run_once()
        assert not runtime.realtime.snapshot().capture_active
        assert runtime.realtime.snapshot().response_id is None
        assert runtime.process_command("Emilia another request") == "recovered"
        assert api.messages == ["synthetic request", "another request"]
    finally:
        runtime.close()
