"""Incremental, provider-independent segmentation of streamed text for speech."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterator

_SPEECH_MARKUP = re.compile(r"[*$#@]")
_SENTENCE_BOUNDARY = re.compile(r"[.!?;:](?=\s|$)")


class SpeechChunker:
    """Turn text deltas into clean sentence or soft-boundary speech fragments."""

    __slots__ = (
        "first_speech_min_chars",
        "speech_chunk_max_chars",
        "speech_chunk_max_delay_seconds",
        "_clock",
        "_buffer",
        "_buffer_started_at",
        "_generated_chars",
        "_speech_committed",
    )

    def __init__(
        self,
        first_speech_min_chars: int = 0,
        speech_chunk_max_chars: int = 0,
        speech_chunk_max_delay_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if first_speech_min_chars < 0:
            raise ValueError("first_speech_min_chars cannot be negative")
        if speech_chunk_max_chars < 0:
            raise ValueError("speech_chunk_max_chars cannot be negative")
        if (
            isinstance(speech_chunk_max_delay_seconds, bool)
            or not isinstance(speech_chunk_max_delay_seconds, (int, float))
            or not math.isfinite(float(speech_chunk_max_delay_seconds))
            or speech_chunk_max_delay_seconds < 0
        ):
            raise ValueError("speech_chunk_max_delay_seconds must be finite and non-negative")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.first_speech_min_chars = first_speech_min_chars
        self.speech_chunk_max_chars = speech_chunk_max_chars
        self.speech_chunk_max_delay_seconds = float(speech_chunk_max_delay_seconds)
        self._clock = clock
        self._buffer = ""
        self._buffer_started_at: float | None = None
        self._generated_chars = 0
        self._speech_committed = False

    @property
    def speech_committed(self) -> bool:
        """Whether at least one speakable fragment has been emitted."""

        return self._speech_committed

    def push(self, text: str) -> Iterator[str]:
        """Append one provider delta and yield every fragment now ready."""

        if text and not self._buffer:
            self._buffer_started_at = self._clock()
        self._buffer += text
        self._generated_chars += len(text)
        return self._drain(force=False)

    def finish(self) -> Iterator[str]:
        """Yield the remaining speakable text as one final fragment."""

        return self._drain(force=True)

    def _next_end(self, *, force: bool) -> int | None:
        if not self._buffer:
            return None
        if force:
            return len(self._buffer)
        if not self._speech_committed and self._generated_chars < self.first_speech_min_chars:
            return None

        boundary = _SENTENCE_BOUNDARY.search(self._buffer)
        if boundary is not None and (
            self.speech_chunk_max_chars == 0 or boundary.end() <= self.speech_chunk_max_chars
        ):
            return boundary.end()

        if self.speech_chunk_max_chars > 0 and len(self._buffer) >= self.speech_chunk_max_chars:
            window = self._buffer[: self.speech_chunk_max_chars + 1]
            whitespace = tuple(re.finditer(r"\s+", window))
            if whitespace and whitespace[-1].start() > 0:
                return whitespace[-1].start()

        if (
            self.speech_chunk_max_delay_seconds > 0
            and self._buffer_started_at is not None
            and self._clock() - self._buffer_started_at >= self.speech_chunk_max_delay_seconds
        ):
            # A slow provider may stream a few words without punctuation. Do
            # not leave those words silent until the terminal completion event.
            # Only split at whitespace, preserving whole words.
            whitespace = tuple(re.finditer(r"\s+", self._buffer))
            if whitespace and whitespace[-1].start() > 0:
                return whitespace[-1].start()

        # Never split a word merely to meet the soft size objective. Late
        # punctuation is still a safe boundary when no whitespace is usable.
        return boundary.end() if boundary is not None else None

    def _drain(self, *, force: bool) -> Iterator[str]:
        while self._buffer:
            end = self._next_end(force=force)
            if end is None:
                return
            fragment = self._buffer[:end]
            self._buffer = self._buffer[end:].lstrip()
            self._buffer_started_at = self._clock() if self._buffer else None
            sentence = _SPEECH_MARKUP.sub("", fragment).strip()
            if sentence and any(character.isalnum() for character in sentence):
                self._speech_committed = True
                yield sentence
            if force:
                return


__all__ = ["SpeechChunker"]
