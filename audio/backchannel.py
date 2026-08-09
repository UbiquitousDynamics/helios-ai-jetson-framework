"""Low-latency playback of already-synthesized conversational acknowledgments."""

from __future__ import annotations

import logging
import inspect
import threading
import time
from concurrent.futures import CancelledError, Future
from typing import Any, Callable

logger = logging.getLogger(__name__)


class BackchannelSession:
    """Schedule one cached acknowledgment and supersede it before real speech.

    The supplied executor owns all concurrent work. The session never asks the
    TTS engine to synthesize at trigger time: it only calls ``speak_preloaded``.
    """

    def __init__(
        self,
        *,
        tts: Any,
        phrase: str,
        delay_seconds: float,
        executor: Any,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not phrase.strip():
            raise ValueError("backchannel phrase cannot be empty")
        if delay_seconds <= 0:
            raise ValueError("backchannel delay must be positive")
        self.tts = tts
        self.phrase = phrase
        self.delay_seconds = float(delay_seconds)
        self._clock = clock
        self._deadline = clock() + self.delay_seconds
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._triggered = False
        self._played = False
        self._playing = False
        self._future: Future[Any] = executor.submit(self._run)

    @property
    def future(self) -> Future[Any]:
        return self._future

    @property
    def triggered(self) -> bool:
        with self._lock:
            return self._triggered

    @property
    def played(self) -> bool:
        with self._lock:
            return self._played

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._playing

    def _run(self) -> None:
        remaining = max(0.0, self._deadline - self._clock())
        if self._cancelled.wait(remaining):
            return
        player = getattr(self.tts, "speak_preloaded", None)
        if not callable(player):
            logger.debug("TTS backend has no preloaded backchannel support")
            return
        try:
            parameters = inspect.signature(player).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        supports_cancellation = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            or (
                parameter.name == "cancellation"
                and parameter.kind
                in {
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                }
            )
            for parameter in parameters
        )
        if not supports_cancellation:
            # A global interrupt can stop an unrelated buffer while this cue is
            # merely queued, then still let the cue start afterward. Skip legacy
            # players rather than violate the no-overlap guarantee.
            logger.debug("TTS backend lacks scoped backchannel cancellation")
            return
        with self._lock:
            if self._cancelled.is_set():
                return
            self._triggered = True
            self._playing = True
        try:
            result = player(self.phrase, cancellation=self._cancelled)
            with self._lock:
                self._played = result is not False
        except Exception:
            # A filler cue must never turn a valid model response into a failure.
            logger.warning("Unable to play preloaded backchannel", exc_info=True)
        finally:
            with self._lock:
                self._playing = False

    def supersede(self) -> None:
        """Prevent or interrupt the cue and wait until its playback has stopped."""

        self._cancelled.set()
        if self._future.cancel():
            return
        try:
            self._future.result()
        except CancelledError:
            return
        except Exception:
            # ``_run`` contains its own safety boundary; retain this guard for
            # unusual executor implementations.
            logger.warning("Backchannel worker failed", exc_info=True)


__all__ = ["BackchannelSession"]
