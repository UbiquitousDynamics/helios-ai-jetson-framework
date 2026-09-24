from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from audio.speech_pipeline import (
    SpeechPipeline,
    SpeechPipelineShutdownTimeout,
    SpeechPipelineTimeout,
)
from api.realtime_conversation import ResponseEvent


@pytest.mark.parametrize(
    "argument,value",
    [
        ("max_pending", True),
        ("max_pending", 1.5),
        ("max_pending", 0),
        ("operation_timeout", 0),
        ("operation_timeout", float("inf")),
        ("shutdown_timeout", -1),
        ("shutdown_timeout", float("nan")),
    ],
)
def test_pipeline_rejects_invalid_wait_and_queue_bounds(argument, value):
    with pytest.raises(ValueError):
        SpeechPipeline(
            synthesize=lambda text: text, play=lambda _fragment: None, **{argument: value}
        )


@pytest.mark.parametrize("waiting", ["dispatch", "drain"])
def test_native_stall_has_bounded_wait_and_explicit_bounded_shutdown(waiting):
    started, release = threading.Event(), threading.Event()

    def synthesize(text):
        started.set()
        assert release.wait(timeout=2)
        return text

    pipeline = SpeechPipeline(
        synthesize=synthesize,
        play=lambda _fragment: None,
        max_pending=1,
        operation_timeout=0.05,
        shutdown_timeout=0.05,
    )
    try:
        pipeline("first")
        assert started.wait(timeout=1)
        if waiting == "dispatch":
            pipeline("queued")
        began = time.monotonic()
        with pytest.raises(SpeechPipelineTimeout):
            pipeline("blocked") if waiting == "dispatch" else pipeline.flush()
        assert time.monotonic() - began < 0.5
        with pytest.raises(SpeechPipelineShutdownTimeout):
            pipeline.close()
        assert pipeline._pending == 1
    finally:
        release.set()
        for thread in pipeline._threads:
            thread.join(timeout=1)
        pipeline.close()
    assert pipeline._pending == 0
    assert not any(thread.is_alive() for thread in pipeline._threads)


def test_cancel_wakes_blocked_producer_and_old_failure_cannot_poison_next_response():
    started, release = threading.Event(), threading.Event()
    producer_entered = threading.Event()
    played = []

    def synthesize(text):
        if text == "old":
            started.set()
            assert release.wait(timeout=2)
            raise RuntimeError("late old-generation failure")
        return text

    pipeline = SpeechPipeline(synthesize=synthesize, play=played.append, max_pending=1)
    put = pipeline._put

    def observed_put(target, item, **kwargs):
        if item[1] == "old blocked":
            producer_entered.set()
        return put(target, item, **kwargs)

    pipeline._put = observed_put
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            pipeline("old")
            assert started.wait(timeout=1)
            pipeline("old queued")
            blocked = pool.submit(pipeline, "old blocked")
            assert producer_entered.wait(timeout=1)
            # The first two jobs occupy the worker and the queue.
            assert not blocked.done()
            pipeline.cancel()
            blocked.result(timeout=1)
            pipeline("new")
            release.set()
            pipeline.flush()
            assert played == ["new"]
        finally:
            release.set()
            pipeline.close()
    assert pipeline._pending == 0


def test_close_wakes_blocked_producer_without_accepting_new_audio():
    started, release = threading.Event(), threading.Event()

    def synthesize(text):
        started.set()
        assert release.wait(timeout=2)
        return text

    pipeline = SpeechPipeline(
        synthesize=synthesize, play=lambda _fragment: None, max_pending=1, interrupt=release.set
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        pipeline("active")
        assert started.wait(timeout=1)
        pipeline("queued")
        blocked = pool.submit(pipeline, "blocked")
        pipeline.close()
        with pytest.raises(RuntimeError, match="closed"):
            blocked.result(timeout=1)
    assert pipeline._pending == 0
    assert not any(thread.is_alive() for thread in pipeline._threads)


def test_cancellation_signal_is_latched_before_playback_backend_registration():
    entered, release = threading.Event(), threading.Event()
    played = []

    def play(fragment, *, cancellation_event):
        entered.set()
        assert release.wait(timeout=2)
        if not cancellation_event.is_set():
            played.append(fragment)

    with SpeechPipeline(synthesize=lambda text: text, play=play) as pipeline:
        pipeline("old")
        try:
            assert entered.wait(timeout=1)
            pipeline.cancel()
        finally:
            release.set()
        pipeline.flush()
        pipeline("new")
        pipeline.flush()
    assert played == ["new"]


def test_pipeline_retains_original_response_observer_across_cancellation():
    started = threading.Event()
    release = threading.Event()
    observed = [[], []]
    calls = []

    def synthesize(text):
        calls.append(text)
        if len(calls) == 1:
            started.set()
            assert release.wait(timeout=2)
        return text

    with SpeechPipeline(synthesize=synthesize, play=lambda _fragment: None) as pipeline:
        old = pipeline.with_observer(observed[0].append)
        new = pipeline.with_observer(observed[1].append)
        old("first")
        try:
            assert started.wait(timeout=1)
            old.cancel()
            new("second")
        finally:
            release.set()
        new.flush()
    assert observed[0] == [ResponseEvent.SYNTHESIS_STARTED, ResponseEvent.SYNTHESIS_COMPLETED]
    assert observed[1] == [
        ResponseEvent.SYNTHESIS_STARTED,
        ResponseEvent.SYNTHESIS_COMPLETED,
        ResponseEvent.PLAYBACK_STARTED,
        ResponseEvent.PLAYBACK_COMPLETED,
    ]


@pytest.mark.parametrize("stage", ["synthesis", "playback"])
def test_bound_pipeline_observer_receives_stage_failure(stage):
    events = []

    def synthesize(text):
        if stage == "synthesis":
            raise RuntimeError("synthetic failure")
        return text

    def play(_fragment):
        raise RuntimeError("synthetic failure")

    with SpeechPipeline(synthesize=synthesize, play=play) as pipeline:
        speak = pipeline.with_observer(events.append)
        with pytest.raises(RuntimeError, match="synthetic failure"):
            speak("synthetic")
            speak.flush()
    assert events[-1] is (
        ResponseEvent.SYNTHESIS_FAILED if stage == "synthesis" else ResponseEvent.PLAYBACK_FAILED
    )


class _Recorder:
    """Records the real interleaving of synthesis and playback."""

    def __init__(self, synthesis_seconds: float = 0.02, playback_seconds: float = 0.05) -> None:
        self.synthesis_seconds = synthesis_seconds
        self.playback_seconds = playback_seconds
        self.events: list[tuple[str, str]] = []
        self.played: list[str] = []
        self._lock = threading.Lock()

    def _record(self, stage: str, text: str) -> None:
        with self._lock:
            self.events.append((stage, text))

    def synthesize(self, text: str) -> str:
        self._record("synth_start", text)
        time.sleep(self.synthesis_seconds)
        self._record("synth_end", text)
        return text

    def play(self, fragment: str) -> object:
        self._record("play_start", fragment)
        time.sleep(self.playback_seconds)
        self._record("play_end", fragment)
        with self._lock:
            self.played.append(fragment)
        return object()


def _pipeline(recorder: _Recorder, **kwargs: object) -> SpeechPipeline:
    return SpeechPipeline(
        synthesize=recorder.synthesize,
        play=recorder.play,
        **kwargs,  # type: ignore[arg-type]
    )


def test_playback_order_is_preserved() -> None:
    recorder = _Recorder()
    fragments = [f"frase {index}" for index in range(6)]

    with _pipeline(recorder) as pipeline:
        for fragment in fragments:
            pipeline(fragment)
        pipeline.flush()

    assert recorder.played == fragments


def test_synthesis_overlaps_playback() -> None:
    """The next fragment must be rendered while the current one is audible."""

    recorder = _Recorder(synthesis_seconds=0.05, playback_seconds=0.10)

    with _pipeline(recorder) as pipeline:
        for index in range(4):
            pipeline(f"frase {index}")
        pipeline.flush()

    # Find the window during which the first fragment was playing and assert
    # some later fragment finished synthesizing inside it. Serialized speech
    # cannot produce this interleaving.
    first_play_start = recorder.events.index(("play_start", "frase 0"))
    first_play_end = recorder.events.index(("play_end", "frase 0"))
    during_first_playback = recorder.events[first_play_start:first_play_end]
    assert any(
        stage == "synth_end" and text != "frase 0" for stage, text in during_first_playback
    ), f"no synthesis overlapped playback: {recorder.events}"


def test_flush_returns_timings_in_playback_order() -> None:
    recorder = _Recorder(synthesis_seconds=0.0, playback_seconds=0.0)
    recorder.play = lambda fragment: f"timing:{fragment}"  # type: ignore[assignment]

    with _pipeline(recorder) as pipeline:
        pipeline("uno")
        pipeline("due")
        timings = pipeline.flush()

    assert timings == ("timing:uno", "timing:due")


def test_flush_without_dispatch_returns_no_timings() -> None:
    recorder = _Recorder()

    with _pipeline(recorder) as pipeline:
        assert pipeline.flush() == ()


def test_synthesis_failure_is_reraised_to_the_caller() -> None:
    recorder = _Recorder(synthesis_seconds=0.0, playback_seconds=0.0)

    def failing(text: str) -> str:
        raise RuntimeError("piper non disponibile")

    pipeline = SpeechPipeline(synthesize=failing, play=recorder.play)
    try:
        pipeline("frase")
        with pytest.raises(RuntimeError, match="piper non disponibile"):
            pipeline.flush()
    finally:
        pipeline.close()

    assert recorder.played == []


def test_playback_failure_is_reraised_to_the_caller() -> None:
    recorder = _Recorder(synthesis_seconds=0.0, playback_seconds=0.0)

    def failing(fragment: str) -> object:
        raise RuntimeError("uscita audio assente")

    pipeline = SpeechPipeline(synthesize=recorder.synthesize, play=failing)
    try:
        pipeline("frase")
        with pytest.raises(RuntimeError, match="uscita audio assente"):
            pipeline.flush()
    finally:
        pipeline.close()


def test_a_failed_fragment_stops_later_fragments() -> None:
    """A speech failure must not let subsequent fragments play out of context."""

    recorder = _Recorder(synthesis_seconds=0.0, playback_seconds=0.0)
    calls: list[str] = []

    def sometimes_failing(text: str) -> str:
        calls.append(text)
        if text == "due":
            raise RuntimeError("sintesi fallita")
        return text

    pipeline = SpeechPipeline(synthesize=sometimes_failing, play=recorder.play)
    try:
        pipeline("uno")
        pipeline("due")
        # Let the failure land before dispatching more.
        time.sleep(0.05)
        with pytest.raises(RuntimeError, match="sintesi fallita"):
            pipeline("tre")
    finally:
        pipeline.close()

    assert "tre" not in recorder.played


def test_cancel_discards_queued_fragments() -> None:
    recorder = _Recorder(synthesis_seconds=0.0, playback_seconds=0.05)
    started = threading.Event()

    def blocking_play(fragment: str) -> object:
        started.set()
        return recorder.play(fragment)

    pipeline = SpeechPipeline(
        synthesize=recorder.synthesize,
        play=blocking_play,
        max_pending=8,
    )
    try:
        for index in range(8):
            pipeline(f"frase {index}")
        assert started.wait(timeout=2.0)
        pipeline.cancel()
        pipeline.flush()
    finally:
        pipeline.close()

    # Cancellation cannot unplay audio already started, but it must stop the
    # queue well short of everything dispatched.
    assert len(recorder.played) < 8


def test_cancel_clears_timings_from_the_cancelled_response() -> None:
    recorder = _Recorder(synthesis_seconds=0.0, playback_seconds=0.0)

    with _pipeline(recorder) as pipeline:
        pipeline("uno")
        pipeline.flush()
        pipeline("due")
        pipeline.cancel()
        assert pipeline.flush() == ()


def test_dispatch_after_close_is_rejected() -> None:
    recorder = _Recorder()
    pipeline = _pipeline(recorder)
    pipeline.close()

    with pytest.raises(RuntimeError, match="closed"):
        pipeline("frase")


def test_none_from_synthesis_is_skipped_without_playback() -> None:
    """PiperTTS returns None for punctuation-only fragments."""

    recorder = _Recorder(synthesis_seconds=0.0, playback_seconds=0.0)

    def skipping(text: str) -> str | None:
        return None if text == "..." else text

    with SpeechPipeline(synthesize=skipping, play=recorder.play) as pipeline:
        pipeline("...")
        pipeline("frase")
        pipeline.flush()

    assert recorder.played == ["frase"]
