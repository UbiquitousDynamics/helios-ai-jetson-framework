"""Incremental, provider-independent segmentation of streamed text for speech."""

from __future__ import annotations

import re
from collections.abc import Iterator

_SPEECH_MARKUP = re.compile(r"[*$#@]")
_SENTENCE_BOUNDARY = re.compile(r"[.!?;:](?=\s|$)")


class SpeechChunker:
    """Turn text deltas into clean sentence or soft-boundary speech fragments."""

    __slots__ = (
        "first_speech_min_chars",
        "speech_chunk_max_chars",
        "_buffer",
        "_generated_chars",
        "_speech_committed",
    )

    def __init__(
        self,
        first_speech_min_chars: int = 0,
        speech_chunk_max_chars: int = 0,
    ) -> None:
        if first_speech_min_chars < 0:
            raise ValueError("first_speech_min_chars cannot be negative")
        if speech_chunk_max_chars < 0:
            raise ValueError("speech_chunk_max_chars cannot be negative")
        self.first_speech_min_chars = first_speech_min_chars
        self.speech_chunk_max_chars = speech_chunk_max_chars
        self._buffer = ""
        self._generated_chars = 0
        self._speech_committed = False

    @property
    def speech_committed(self) -> bool:
        """Whether at least one speakable fragment has been emitted."""

        return self._speech_committed

    def push(self, text: str) -> Iterator[str]:
        """Append one provider delta and yield every fragment now ready."""

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
            sentence = _SPEECH_MARKUP.sub("", fragment).strip()
            if sentence and any(character.isalnum() for character in sentence):
                self._speech_committed = True
                yield sentence
            if force:
                return


__all__ = ["SpeechChunker"]
