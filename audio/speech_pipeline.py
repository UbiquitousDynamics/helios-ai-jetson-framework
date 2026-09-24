"""Overlapped speech dispatch for streamed model responses.

The synchronous path (``PiperTTS.speak_with_timing``) serializes three things
that do not need to be serialized:

1. reading provider events,
2. synthesizing a fragment,
3. playing it.

Because the streaming coordinator called ``speak()`` inline, nothing was read
from the provider while audio played, and no audio was rendered while audio
played. The audible result was a gap between sentences equal to the synthesis
time of the next one, and provider read timeouts ticked during playback.

This module keeps the coordinator's contract -- fragments are spoken in order,
failures surface to the caller, cancellation is prompt -- while running
synthesis and playback on their own threads. Only playback order is actually
constrained, so only playback is serialized.
"""

from __future__ import annotations

import logging
import inspect
import math
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from api.realtime_conversation import ResponseEvent

logger = logging.getLogger(__name__)

_STAGE_SHUTDOWN_TIMEOUT_SECONDS = 5.0
# Bounded so a fast model cannot render an unbounded amount of audio ahead of
# playback. Two in flight is enough to hide synthesis behind playback.
_DEFAULT_MAX_PENDING = 2
_OPERATION_TIMEOUT_SECONDS = 30.0


class SpeechPipelineTimeout(RuntimeError):
    """A bounded speech-stage wait expired."""


class SpeechPipelineShutdownTimeout(SpeechPipelineTimeout):
    """A native speech call still owns resources after the close deadline."""


class SpeechPipeline:
    """Speak fragments in order, overlapping synthesis with playback.

    Call the instance to dispatch a fragment; it returns immediately. Call
    :meth:`flush` to wait for queued audio to finish and collect the timing
    objects the synchronous path would have returned inline. The first failure
    in either stage is re-raised from a later dispatch or from ``flush``, so the
    caller still sees speech errors at a point where it can act on them.
    """

    def __init__(
        self,
        *,
        synthesize: Callable[[str], Any],
        play: Callable[[Any], Any],
        max_pending: int = _DEFAULT_MAX_PENDING,
        operation_timeout: float = _OPERATION_TIMEOUT_SECONDS,
        shutdown_timeout: float = _STAGE_SHUTDOWN_TIMEOUT_SECONDS,
        interrupt: Callable[[], Any] | None = None,
    ) -> None:
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending < 1:
            raise ValueError("max_pending must be at least one")
        for value in (operation_timeout, shutdown_timeout):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("speech wait bounds must be finite and positive")
        self._synthesize = synthesize
        self._play = play
        try:
            self._play_accepts_cancellation = (
                "cancellation_event" in inspect.signature(play).parameters
            )
        except (TypeError, ValueError):
            self._play_accepts_cancellation = False
        self._synthesis_queue: queue.Queue[Any] = queue.Queue(maxsize=max_pending)
        self._playback_queue: queue.Queue[Any] = queue.Queue(maxsize=max_pending)
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._stopping = threading.Event()
        self._generation_stop = threading.Event()
        self._pending = 0
        self._operation_timeout = operation_timeout
        self._shutdown_timeout = shutdown_timeout
        self._interrupt = interrupt
        self._timings: list[Any] = []
        self._error: BaseException | None = None
        self._generation = 0
        self._threads: tuple[threading.Thread, ...] = ()
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def _ensure_threads(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Speech pipeline is closed")
            if self._threads:
                return
            self._threads = (
                threading.Thread(
                    target=self._synthesis_worker,
                    name="speech-synthesis",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._playback_worker,
                    name="speech-playback",
                    daemon=True,
                ),
            )
            for thread in self._threads:
                thread.start()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stopping.set()
            self._generation_stop.set()
            threads = self._threads
            self._generation += 1
            self._drain(self._synthesis_queue)
            self._drain(self._playback_queue)
            self._changed.notify_all()
        self._interrupt_playback()
        deadline = time.monotonic() + self._shutdown_timeout
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise SpeechPipelineShutdownTimeout(
                "speech stage did not stop before the close deadline"
            )

    def __enter__(self) -> SpeechPipeline:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- dispatch ----------------------------------------------------------

    def __call__(self, text: str) -> None:
        """Dispatch one fragment. Returns before the audio is played."""

        self._dispatch(text, None)

    def with_observer(self, observer: Callable[[ResponseEvent], None]) -> Any:
        """Bind an immutable response callback to each queued fragment."""
        pipeline = self

        class ObservedSpeech:
            def __call__(self, text: str) -> None:
                pipeline._dispatch(text, observer)

            def flush(self) -> tuple[Any, ...]:
                return pipeline.flush()

            def cancel(self) -> None:
                pipeline.cancel()

        return ObservedSpeech()

    def _dispatch(self, text: str, observer: Callable[[ResponseEvent], None] | None) -> None:

        self._raise_pending_error()
        self._ensure_threads()
        with self._lock:
            generation = self._generation
            generation_stop = self._generation_stop
        enqueued = self._put(
            self._synthesis_queue, (generation, text, observer, generation_stop), initial=True
        )
        if not enqueued and self._closed:
            raise RuntimeError("Speech pipeline is closed")
        # Surface a failure that happened while this dispatch was blocked on
        # backpressure, so an error cannot be delayed until flush.
        self._raise_pending_error()

    def flush(self) -> tuple[Any, ...]:
        """Wait for dispatched audio to finish and return its timings."""

        deadline = time.monotonic() + self._operation_timeout
        with self._changed:
            while self._pending:
                self._raise_pending_error()
                if self._closed:
                    raise RuntimeError("Speech pipeline is closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error = SpeechPipelineTimeout("speech drain deadline expired")
                    self._record_error(error, self._generation)
                    raise error
                self._changed.wait(remaining)
            self._raise_pending_error()
            return self._take_timings()

    def cancel(self) -> None:
        """Discard queued fragments and stop attributing their timings.

        In-flight native synthesis cannot be preempted, so its result is
        dropped by generation check instead.
        """

        with self._lock:
            self._generation += 1
            self._generation_stop.set()
            self._generation_stop = threading.Event()
            self._timings = []
            self._error = None
            self._drain(self._synthesis_queue)
            self._drain(self._playback_queue)
            self._changed.notify_all()
        self._interrupt_playback()

    def _interrupt_playback(self) -> None:
        if callable(self._interrupt):
            try:
                self._interrupt()
            except Exception:
                logger.warning("Unable to interrupt speech playback")

    def _put(self, target: queue.Queue[Any], item: Any, *, initial: bool = False) -> bool:
        deadline = time.monotonic() + self._operation_timeout
        with self._changed:
            while self._is_current(item[0]):
                try:
                    target.put_nowait(item)
                except queue.Full:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        error = SpeechPipelineTimeout("speech queue deadline expired")
                        self._record_error(error, item[0])
                        raise error
                    self._changed.wait(remaining)
                else:
                    if initial:
                        self._pending += 1
                    return True
            return False

    def _complete(self) -> None:
        with self._changed:
            self._pending -= 1
            self._changed.notify_all()

    # -- internals ---------------------------------------------------------

    def _take_timings(self) -> tuple[Any, ...]:
        with self._lock:
            timings = tuple(self._timings)
            self._timings = []
        return timings

    def _raise_pending_error(self) -> None:
        with self._lock:
            error = self._error
        if error is not None:
            raise error

    def _record_error(self, error: BaseException, generation: int) -> None:
        with self._lock:
            if generation == self._generation and self._error is None and not self._closed:
                self._error = error
                self._generation_stop.set()
                self._drain(self._synthesis_queue)
                self._drain(self._playback_queue)
                self._changed.notify_all()

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and self._error is None and not self._closed

    def _drain(self, target: queue.Queue[Any]) -> None:
        while True:
            try:
                target.get_nowait()
            except queue.Empty:
                return
            target.task_done()
            self._complete()

    def _synthesis_worker(self) -> None:
        while not self._stopping.is_set():
            try:
                item = self._synthesis_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            handed_off = False
            generation, text, observer, generation_stop = item
            with self._changed:
                self._changed.notify_all()
            try:
                if not self._is_current(generation):
                    continue
                if observer:
                    observer(ResponseEvent.SYNTHESIS_STARTED)
                try:
                    fragment = self._synthesize(text)
                except BaseException:
                    if observer:
                        observer(ResponseEvent.SYNTHESIS_FAILED)
                    raise
                if observer:
                    observer(ResponseEvent.SYNTHESIS_COMPLETED)
                if fragment is None:
                    continue
                if not self._is_current(generation):
                    continue
                handed_off = self._put(
                    self._playback_queue, (generation, fragment, observer, generation_stop)
                )
            except BaseException as error:  # noqa: BLE001 - reported to caller
                self._record_error(error, generation)
            finally:
                if not handed_off:
                    self._complete()
                self._synthesis_queue.task_done()

    def _playback_worker(self) -> None:
        while not self._stopping.is_set():
            try:
                item = self._playback_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            generation, fragment, observer, generation_stop = item
            with self._changed:
                self._changed.notify_all()
            try:
                if not self._is_current(generation):
                    continue
                if observer:
                    observer(ResponseEvent.PLAYBACK_STARTED)
                try:
                    if self._play_accepts_cancellation:
                        timing = self._play(fragment, cancellation_event=generation_stop)
                    else:
                        timing = self._play(fragment)
                except BaseException:
                    if observer:
                        observer(ResponseEvent.PLAYBACK_FAILED)
                    raise
                if observer:
                    observer(ResponseEvent.PLAYBACK_COMPLETED)
                if timing is None:
                    continue
                with self._lock:
                    if generation == self._generation:
                        self._timings.append(timing)
            except BaseException as error:  # noqa: BLE001 - reported to caller
                self._record_error(error, generation)
            finally:
                self._complete()
                self._playback_queue.task_done()
