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
import queue
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_STAGE_SHUTDOWN_TIMEOUT_SECONDS = 5.0
# Bounded so a fast model cannot render an unbounded amount of audio ahead of
# playback. Two in flight is enough to hide synthesis behind playback.
_DEFAULT_MAX_PENDING = 2


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
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be at least one")
        self._synthesize = synthesize
        self._play = play
        self._synthesis_queue: queue.Queue[Any] = queue.Queue(maxsize=max_pending)
        self._playback_queue: queue.Queue[Any] = queue.Queue(maxsize=max_pending)
        self._lock = threading.Lock()
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
            if self._closed:
                return
            self._closed = True
            threads = self._threads
            self._generation += 1
        self._drain(self._synthesis_queue)
        self._drain(self._playback_queue)
        self._synthesis_queue.put(None)
        self._playback_queue.put(None)
        for thread in threads:
            thread.join(timeout=_STAGE_SHUTDOWN_TIMEOUT_SECONDS)
            if thread.is_alive():
                # Daemon threads, so a stuck native synthesis call cannot keep
                # the process alive. Report it rather than blocking shutdown.
                logger.warning("Speech pipeline stage did not stop: %s", thread.name)

    def __enter__(self) -> SpeechPipeline:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- dispatch ----------------------------------------------------------

    def __call__(self, text: str) -> None:
        """Dispatch one fragment. Returns before the audio is played."""

        self._raise_pending_error()
        self._ensure_threads()
        with self._lock:
            generation = self._generation
        self._synthesis_queue.put((generation, text))
        # Surface a failure that happened while this dispatch was blocked on
        # backpressure, so an error cannot be delayed until flush.
        self._raise_pending_error()

    def flush(self) -> tuple[Any, ...]:
        """Wait for dispatched audio to finish and return its timings."""

        with self._lock:
            # ``_lock`` is not reentrant, so decide here and collect below.
            started = bool(self._threads)
        if started:
            self._synthesis_queue.join()
            self._playback_queue.join()
            self._raise_pending_error()
        return self._take_timings()

    def cancel(self) -> None:
        """Discard queued fragments and stop attributing their timings.

        In-flight native synthesis cannot be preempted, so its result is
        dropped by generation check instead.
        """

        with self._lock:
            self._generation += 1
            self._timings = []
            self._error = None
        self._drain(self._synthesis_queue)
        self._drain(self._playback_queue)

    # -- internals ---------------------------------------------------------

    def _take_timings(self) -> tuple[Any, ...]:
        with self._lock:
            timings = tuple(self._timings)
            self._timings = []
        return timings

    def _raise_pending_error(self) -> None:
        with self._lock:
            error = self._error
            self._error = None
        if error is not None:
            raise error

    def _record_error(self, error: BaseException) -> None:
        with self._lock:
            if self._error is None:
                self._error = error

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and self._error is None

    @staticmethod
    def _drain(target: queue.Queue[Any]) -> None:
        while True:
            try:
                item = target.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                target.task_done()
            else:
                # Preserve a shutdown sentinel for the worker.
                target.put(None)
                target.task_done()
                return

    def _synthesis_worker(self) -> None:
        while True:
            item = self._synthesis_queue.get()
            if item is None:
                self._synthesis_queue.task_done()
                return
            try:
                generation, text = item
                if not self._is_current(generation):
                    continue
                fragment = self._synthesize(text)
                if fragment is None:
                    continue
                if not self._is_current(generation):
                    continue
                self._playback_queue.put((generation, fragment))
            except BaseException as error:  # noqa: BLE001 - reported to caller
                self._record_error(error)
            finally:
                self._synthesis_queue.task_done()

    def _playback_worker(self) -> None:
        while True:
            item = self._playback_queue.get()
            if item is None:
                self._playback_queue.task_done()
                return
            try:
                generation, fragment = item
                if not self._is_current(generation):
                    continue
                timing = self._play(fragment)
                if timing is None:
                    continue
                with self._lock:
                    if generation == self._generation:
                        self._timings.append(timing)
            except BaseException as error:  # noqa: BLE001 - reported to caller
                self._record_error(error)
            finally:
                self._playback_queue.task_done()
