"""Issue #21: exercise worker unwinding through the real voice-turn boundary."""

import logging
import threading

import pytest

from api.api_client import APIClientError
from api.metrics import SafeMetricsRecorder
from recognizer.speech_recognizer import RecognitionResult
from test_assistant import FakeAPI, make_assistant


@pytest.mark.parametrize("confirmed_interruption", [True, False])
def test_cancelled_worker_and_provider_failure_have_distinct_logs_and_kpis(
    confirmed_interruption, monkeypatch, caplog
):
    metrics = SafeMetricsRecorder()
    runtime, *_ = make_assistant(
        [RecognitionResult("Emilia raccontami del sole", True)], metrics=metrics
    )
    started = threading.Event()
    release = threading.Event()

    class FailingAPI(FakeAPI):
        def talk(self, message, context=None):
            self.messages.append(message)
            if len(self.messages) > 1:
                return "follow-up response"
            started.set()
            assert release.wait(2), "test did not release the response worker"
            category = "cancelled" if self.cancelled else "server_error"
            raise APIClientError(f"Unable to complete request ({category})")

        def cancel_current(self):
            super().cancel_current()
            release.set()

    api = FailingAPI()
    runtime.api_client = api
    captures = 0

    def capture(response, *, cancellation, response_deadline):
        nonlocal captures
        captures += 1
        if captures == 1:
            assert started.wait(2)
            if confirmed_interruption:
                # The detector's confirmed output; cancellation itself is real.
                runtime._interrupt_current_response(cancellation)
                return "Emilia raccontami della luna"
            release.set()
        # Wait without consuming the exception: the voice-turn boundary must do that.
        response.exception(timeout=2)
        return None

    monkeypatch.setattr(runtime, "_listen_for_barge_in", capture)
    caplog.set_level(logging.DEBUG, logger="assistant")
    try:
        if confirmed_interruption:
            assert runtime.run_once() is True
        else:
            with pytest.raises(APIClientError, match="server_error"):
                runtime.run_once()
        events = metrics.snapshot()
        interruptions = [e for e in events if e.event == "voice_response_interrupted"]
        failures = [e for e in events if e.event == "voice_command_failed"]
        completed = [e for e in events if e.event == "voice_command_completed"]
        assert len(interruptions) == int(confirmed_interruption)
        assert len(failures) == int(not confirmed_interruption)
        assert len(completed) == int(confirmed_interruption)
        if interruptions:
            assert interruptions[0].outcome == "cancelled"
            assert interruptions[0].success is False
        if failures:
            assert failures[0].outcome == "failed"
            assert failures[0].success is False
        unwind = [r for r in caplog.records if "event=interrupted_response_unwound" in r.message]
        assert len(unwind) == int(confirmed_interruption)
        if unwind:
            assert "outcome=APIClientError" in unwind[0].message
        assert len(api.messages) == (2 if confirmed_interruption else 1)
    finally:
        release.set()
        runtime.close()
