"""Local transcript authority and bounded, transient revision aggregation.

Plain strings remain trusted input for legacy callers. Realtime callers must
use the promoter; extracting a provisional object's text manually is not an
authorized conversion. These Python types are an integration guard, not a
security boundary against callers deliberately bypassing the contract.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


class TranscriptBoundaryError(TypeError):
    """An input lacks the authority required by a downstream text boundary."""


def _positive_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True, order=True)
class TranscriptSegment:
    """Monotonic capture/segment identity within one recognizer lifetime."""

    capture_id: int
    segment_id: int

    def __post_init__(self) -> None:
        _positive_integer(self.capture_id, "capture_id")
        _positive_integer(self.segment_id, "segment_id")


@dataclass(frozen=True, slots=True)
class ProvisionalRevision:
    text: str = field(repr=False)
    segment: TranscriptSegment | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("transcript text must be a string")
        if self.segment is not None and not isinstance(self.segment, TranscriptSegment):
            raise TypeError("segment must be a TranscriptSegment")
        _positive_integer(self.revision, "revision")


@dataclass(frozen=True, slots=True, init=False)
class AuthoritativeUtterance:
    text: str = field(repr=False)
    segment: TranscriptSegment | None
    revision: int

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TranscriptBoundaryError("use TranscriptPromoter to finalize speech")


def authoritative_text(value: str | AuthoritativeUtterance) -> str:
    """Accept finalized speech or caller-owned legacy text; never stringify it."""

    if isinstance(value, AuthoritativeUtterance):
        return value.text
    if isinstance(value, str):
        return value
    raise TranscriptBoundaryError("finalized speech or a legacy string is required")


class TranscriptPromoter:
    """One atomic promotion per identified segment, without text deduplication.

    Captures and segments must increase for this recognizer. A new capture may
    restart segment numbering. Older revisions/segments/captures are ignored.
    Only one watermark is retained, so storage is constant and content-free.
    Legacy events without both identity fields cannot be reliably deduplicated;
    repeated wording may be an intentional new utterance and remains valid.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._segment: TranscriptSegment | None = None
        self._revision = 0
        self._finalized = False

    def observe(
        self,
        text: str,
        *,
        is_final: bool,
        capture_id: int | None = None,
        segment_id: int | None = None,
        revision: int | None = None,
    ) -> ProvisionalRevision | AuthoritativeUtterance | None:
        if not isinstance(text, str):
            raise TypeError("transcript text must be a string")
        if not isinstance(is_final, bool):
            raise TypeError("is_final must be a boolean")
        for value, name in (
            (capture_id, "capture_id"), (segment_id, "segment_id"), (revision, "revision")
        ):
            if value is not None:
                _positive_integer(value, name)
        segment = (
            TranscriptSegment(capture_id, segment_id)
            if capture_id is not None and segment_id is not None else None
        )
        text = text.strip()
        if not text:
            return None
        with self._lock:
            same_segment = segment is not None and segment == self._segment
            if segment is not None and self._segment is not None:
                if segment < self._segment or (same_segment and self._finalized):
                    return None
            previous_revision = self._revision if same_segment else 0
            next_revision = revision if revision is not None else previous_revision + 1
            if next_revision <= previous_revision:
                return None
            if is_final:
                result = object.__new__(AuthoritativeUtterance)
                object.__setattr__(result, "text", text)
                object.__setattr__(result, "segment", segment)
                object.__setattr__(result, "revision", next_revision)
            else:
                result = ProvisionalRevision(text, segment, next_revision)
            # Publish only after construction succeeds. Anonymous legacy events
            # cannot reset the watermark protecting fully identified captures.
            if segment is not None:
                self._segment = segment
                self._revision = next_revision
                self._finalized = is_final
            return result


class TranscriptCapacityError(ValueError):
    """A revision exceeds the bounded realtime buffer; never truncate speech."""


@dataclass(frozen=True, slots=True)
class TranscriptRevisionSnapshot:
    pending: bool
    characters: int
    segment: TranscriptSegment | None
    revision: int | None


class TranscriptRevisionAggregator:
    """Replace provisional hypotheses and use only the final recognizer text.

    Revisions are complete hypotheses, not text deltas. Concatenating them
    would duplicate words or retain a retracted request. This single-slot
    realtime buffer never edits wording, merges finalized segments, or keeps
    revision history. The existing promoter owns all authority/deduplication.
    """

    def __init__(self, *, maximum_characters: int = 32_768) -> None:
        _positive_integer(maximum_characters, "maximum_characters")
        self.maximum_characters = maximum_characters
        self._lock = threading.Lock()
        self._promoter = TranscriptPromoter()
        self._pending: ProvisionalRevision | None = None

    def observe(
        self,
        text: str,
        *,
        is_final: bool,
        capture_id: int | None = None,
        segment_id: int | None = None,
        revision: int | None = None,
    ) -> ProvisionalRevision | AuthoritativeUtterance | None:
        if not isinstance(text, str):
            raise TypeError("transcript text must be a string")
        with self._lock:
            if len(text) > self.maximum_characters:
                self._pending = None
                raise TranscriptCapacityError("transcript revision exceeds the character limit")
            result = self._promoter.observe(
                text, is_final=is_final, capture_id=capture_id,
                segment_id=segment_id, revision=revision,
            )
            if isinstance(result, ProvisionalRevision):
                self._pending = result
            elif isinstance(result, AuthoritativeUtterance):
                self._pending = None
            return result

    def provisional(self) -> ProvisionalRevision | None:
        """Return only the current, immutable local hypothesis."""
        with self._lock:
            return self._pending

    def clear_pending(self) -> None:
        """Release text on capture exit without making consumed finals replayable."""
        with self._lock:
            self._pending = None

    def snapshot(self) -> TranscriptRevisionSnapshot:
        with self._lock:
            pending = self._pending
            return TranscriptRevisionSnapshot(
                pending is not None, len(pending.text) if pending else 0,
                pending.segment if pending else None, pending.revision if pending else None,
            )
