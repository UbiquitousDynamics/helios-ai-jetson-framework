from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

import api.transcripts as transcripts_module
from api.api_client import APIClient
from api.conversation import ConversationSession, ConversationTurnStatus
from api.metrics import MetricEvent, record_safely
from api.providers.contracts import ChatMessage, ContentOrigin, Role
from api.transcripts import (
    AuthoritativeUtterance,
    ProvisionalRevision,
    TranscriptBoundaryError,
    TranscriptCapacityError,
    TranscriptPromoter,
    TranscriptRevisionAggregator,
    TranscriptSegment,
    authoritative_text,
)
from assistant import VoiceAssistant
from document.rag_system import RagSystem


def test_revisions_finalize_only_the_last_recognizer_wording() -> None:
    promoter = TranscriptPromoter()
    first = promoter.observe("synthetic Tuesday", is_final=False, capture_id=1, segment_id=1)
    second = promoter.observe("synthetic Wednesday", is_final=False, capture_id=1, segment_id=1)
    final = promoter.observe(" synthetic final wording ", is_final=True, capture_id=1, segment_id=1)
    assert isinstance(first, ProvisionalRevision)
    assert isinstance(second, ProvisionalRevision)
    assert (first.revision, second.revision, final.revision) == (1, 2, 3)
    assert first.text == "synthetic Tuesday"
    assert authoritative_text(final) == "synthetic final wording"
    assert final.segment == TranscriptSegment(1, 1)


@pytest.mark.parametrize(
    "revisions,final",
    [
        (["Tuesday", "Tuesday actually", "Wednesday"], "Tuesday... actually, Wednesday."),
        (["martedì", "martedì anzi", "mercoledì"], "martedì... anzi, mercoledì."),
        (["very", "very very"], "very very carefully"),
        (["non", "non non"], "non non cambiare questa frase"),
        (["wrong wording", "entirely revised wording"], "new final wording"),
    ],
)
def test_aggregation_replaces_hypotheses_and_preserves_final_wording(revisions, final):
    aggregator = TranscriptRevisionAggregator()
    for number, text in enumerate(revisions, 1):
        value = aggregator.observe(
            text, is_final=False, capture_id=1, segment_id=1, revision=number
        )
        assert aggregator.provisional() is value
        assert value.text == text
        assert aggregator.snapshot().characters == len(text)
        assert aggregator.snapshot().revision == number
    value = aggregator.observe(
        final, is_final=True, capture_id=1, segment_id=1, revision=len(revisions) + 1
    )
    assert authoritative_text(value) == final
    assert aggregator.provisional() is None
    assert not aggregator.snapshot().pending
    assert aggregator.observe(final, is_final=True, capture_id=1, segment_id=1) is None


def test_aggregation_clears_pending_without_replaying_consumed_or_stale_segments():
    aggregator = TranscriptRevisionAggregator()
    aggregator.observe("previous", is_final=True, capture_id=1, segment_id=1)
    pending = aggregator.observe("private-content", is_final=False, capture_id=1, segment_id=2)
    assert aggregator.observe("old", is_final=True, capture_id=1, segment_id=1) is None
    assert aggregator.provisional() is pending
    assert "private-content" not in repr(aggregator.snapshot())
    aggregator.clear_pending()
    assert aggregator.provisional() is None
    assert aggregator.observe("old", is_final=True, capture_id=1, segment_id=1) is None
    assert (
        authoritative_text(aggregator.observe("new", is_final=True, capture_id=2, segment_id=1))
        == "new"
    )


@pytest.mark.parametrize("is_final", [False, True])
def test_aggregation_capacity_rejects_instead_of_truncating(is_final):
    aggregator = TranscriptRevisionAggregator(maximum_characters=4)
    aggregator.observe("1234", is_final=False, capture_id=1, segment_id=1)
    with pytest.raises(TranscriptCapacityError) as error:
        aggregator.observe("private-content", is_final=is_final, capture_id=1, segment_id=1)
    assert "private-content" not in str(error.value)
    assert aggregator.provisional() is None
    assert (
        authoritative_text(aggregator.observe("new", is_final=True, capture_id=2, segment_id=1))
        == "new"
    )


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5, "invalid"])
def test_aggregation_rejects_invalid_capacity(limit):
    with pytest.raises(ValueError):
        TranscriptRevisionAggregator(maximum_characters=limit)


def test_aggregation_empty_final_does_not_promote_last_partial():
    aggregator = TranscriptRevisionAggregator()
    partial = aggregator.observe("partial only", is_final=False)
    assert aggregator.observe("", is_final=True) is None
    assert aggregator.provisional() is partial
    aggregator.clear_pending()
    assert aggregator.provisional() is None


def test_aggregation_concurrent_finals_produce_one_authoritative_value():
    aggregator = TranscriptRevisionAggregator()
    aggregator.observe("old wording", is_final=False, capture_id=1, segment_id=1)
    barrier = threading.Barrier(8)

    def finalize():
        barrier.wait(timeout=2)
        return aggregator.observe("corrected final", is_final=True, capture_id=1, segment_id=1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [future.result(timeout=3) for future in [pool.submit(finalize) for _ in range(8)]]
    assert sum(isinstance(value, AuthoritativeUtterance) for value in results) == 1
    assert results.count(None) == 7
    assert aggregator.provisional() is None


def test_aggregation_invalid_revision_keeps_pending_and_releases_lock():
    aggregator = TranscriptRevisionAggregator()
    pending = aggregator.observe("pending", is_final=False, capture_id=1, segment_id=1)
    with pytest.raises(ValueError):
        aggregator.observe("bad", is_final=True, capture_id=1, segment_id=1, revision=-1)
    assert aggregator.provisional() is pending
    assert (
        authoritative_text(aggregator.observe("final", is_final=True, capture_id=1, segment_id=1))
        == "final"
    )


@pytest.mark.parametrize("is_final", [False, True])
@pytest.mark.parametrize("revision", [1, 2, 100, None])
def test_finalized_segment_rejects_every_later_observation(is_final, revision) -> None:
    promoter = TranscriptPromoter()
    assert promoter.observe("final", is_final=True, capture_id=2, segment_id=3, revision=2)
    assert (
        promoter.observe(
            "replayed", is_final=is_final, capture_id=2, segment_id=3, revision=revision
        )
        is None
    )


@pytest.mark.parametrize(
    "capture,segment,revision", [(1, 99, 100), (2, 2, 100), (2, 3, 1), (2, 3, 2)]
)
@pytest.mark.parametrize("is_final", [False, True])
def test_stale_captures_segments_and_revisions_are_rejected(capture, segment, revision, is_final):
    promoter = TranscriptPromoter()
    promoter.observe("new partial", is_final=False, capture_id=2, segment_id=3, revision=2)
    assert (
        promoter.observe(
            "stale", is_final=is_final, capture_id=capture, segment_id=segment, revision=revision
        )
        is None
    )
    assert promoter.observe("current", is_final=True, capture_id=2, segment_id=3, revision=3)


def test_capture_restart_and_legacy_repetition_do_not_deduplicate_by_text() -> None:
    promoter = TranscriptPromoter()
    for capture, segment in [(1, 3), (1, 4), (2, 1), (None, None), (None, 1), (None, None)]:
        assert isinstance(
            promoter.observe(
                "repeat intentionally", is_final=True, capture_id=capture, segment_id=segment
            ),
            AuthoritativeUtterance,
        )
    assert promoter.observe("stale", is_final=True, capture_id=1, segment_id=8) is None


@pytest.mark.parametrize("field", ["capture_id", "segment_id", "revision"])
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "secret-invalid-metadata"])
def test_invalid_identity_cannot_mutate_promotion_state(field, value) -> None:
    promoter = TranscriptPromoter()
    kwargs = dict(capture_id=1, segment_id=1, revision=1)
    kwargs[field] = value
    with pytest.raises(ValueError) as error:
        promoter.observe("private-content", is_final=True, **kwargs)
    assert "private-content" not in str(error.value)
    assert "secret-invalid-metadata" not in str(error.value)
    assert promoter.observe("valid", is_final=True, capture_id=1, segment_id=1, revision=1)


@pytest.mark.parametrize("text,final", [(object(), True), ("private-content", "yes"), ("x", 1)])
def test_malformed_observation_is_rejected(text, final) -> None:
    with pytest.raises(TypeError):
        TranscriptPromoter().observe(text, is_final=final)


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"text": object()}, TypeError),
        ({"text": "x", "segment": (1, 1)}, TypeError),
        ({"text": "x", "revision": 0}, ValueError),
        ({"text": "x", "revision": True}, ValueError),
    ],
)
def test_provisional_values_validate_without_stringifying(kwargs, error) -> None:
    with pytest.raises(error):
        ProvisionalRevision(**kwargs)


def test_empty_final_does_not_promote_or_consume_a_partial() -> None:
    promoter = TranscriptPromoter()
    partial = promoter.observe("partial only", is_final=False, capture_id=1, segment_id=1)
    assert isinstance(partial, ProvisionalRevision)
    assert promoter.observe("  ", is_final=True, capture_id=1, segment_id=1) is None
    assert promoter.observe("actual final", is_final=True, capture_id=1, segment_id=1).revision == 2


def test_immutable_content_safe_values_and_single_creation_path(caplog) -> None:
    promoter = TranscriptPromoter()
    values = [
        promoter.observe("private-content", is_final=False),
        promoter.observe("private-content", is_final=True),
    ]
    for value in values:
        assert "private-content" not in repr(value)
        assert "private-content" not in str(value)
        with pytest.raises(FrozenInstanceError):
            value.text = "changed"
    with pytest.raises(TranscriptBoundaryError):
        AuthoritativeUtterance("private-content", None, 1)
    with pytest.raises(TranscriptBoundaryError):
        authoritative_text(values[0])
    assert "private-content" not in caplog.text
    assert "private-content" not in repr(vars(promoter))


def test_concurrent_final_delivery_promotes_exactly_once() -> None:
    promoter = TranscriptPromoter()
    barrier = threading.Barrier(8)

    def deliver():
        barrier.wait(timeout=2)
        return promoter.observe("final", is_final=True, capture_id=1, segment_id=1, revision=1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(deliver) for _ in range(8)]
        results = [future.result(timeout=3) for future in futures]
    assert sum(isinstance(value, AuthoritativeUtterance) for value in results) == 1
    assert results.count(None) == 7


def test_construction_failure_releases_lock_and_keeps_previous_watermark(monkeypatch) -> None:
    promoter = TranscriptPromoter()
    promoter.observe("first partial", is_final=False, capture_id=1, segment_id=1, revision=1)

    def fail_construction(*_args):
        raise RuntimeError("synthetic failure")

    with monkeypatch.context() as patch:
        patch.setattr(transcripts_module, "ProvisionalRevision", fail_construction)
        with pytest.raises(RuntimeError, match="synthetic failure"):
            promoter.observe("new partial", is_final=False, capture_id=2, segment_id=1, revision=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            promoter.observe,
            "actual final",
            is_final=True,
            capture_id=1,
            segment_id=1,
            revision=2,
        ).result(timeout=2)
    assert authoritative_text(result) == "actual final"


@pytest.mark.parametrize("method", ["talk", "think"])
@pytest.mark.parametrize("slot", ["message", "context"])
def test_api_rejects_provisional_before_history_provider_or_speech(method, slot) -> None:
    # Deliberately uninitialized: a rejection must precede any resource access.
    client = object.__new__(APIClient)
    arguments = {"message": "trusted", "context": None}
    arguments[slot] = ProvisionalRevision("private-content")
    with pytest.raises(TranscriptBoundaryError):
        getattr(client, method)(**arguments)


@pytest.mark.parametrize("method", ["search", "retrieve", "run"])
def test_rag_rejects_provisional_before_loading_or_querying(method) -> None:
    rag = object.__new__(RagSystem)
    with pytest.raises(TranscriptBoundaryError):
        getattr(rag, method)(ProvisionalRevision("private-content"))


@pytest.mark.parametrize(
    "method", ["process_command", "_process_model_prompt", "process_rag_command"]
)
def test_assistant_boundaries_reject_provisional_before_side_effects(method) -> None:
    assistant = object.__new__(VoiceAssistant)
    with pytest.raises(TranscriptBoundaryError):
        getattr(assistant, method)(ProvisionalRevision("private-content"))


@pytest.mark.parametrize("origin", list(ContentOrigin))
def test_provider_history_document_and_tool_context_reject_provisional(origin) -> None:
    with pytest.raises(TranscriptBoundaryError):
        ChatMessage(Role.USER, ProvisionalRevision("private-content"), origin=origin)
    final = TranscriptPromoter().observe("final", is_final=True)
    assert ChatMessage(Role.USER, final, origin=origin).content == "final"


def test_history_guard_is_atomic_and_accepts_finalized_text() -> None:
    session = ConversationSession()
    partial = ProvisionalRevision("private-content")
    with pytest.raises(TranscriptBoundaryError):
        session.begin_turn(partial)
    final = TranscriptPromoter().observe("final", is_final=True)
    turn = session.begin_turn(final)
    assert turn.number == 1
    assert turn.user.content == "final"
    with pytest.raises(TranscriptBoundaryError):
        session.complete_turn(turn, partial)
    assert turn.status is ConversationTurnStatus.PENDING
    assert turn.assistant is None
    session.complete_turn(turn, "answer")
    assert turn.status is ConversationTurnStatus.COMPLETED


@pytest.mark.parametrize("field", ["event", "model", "outcome", "latency_ms", "recognized_count"])
@pytest.mark.parametrize("final", [False, True])
def test_metrics_reject_all_transcript_values_before_sink(field, final) -> None:
    value = TranscriptPromoter().observe("private-content", is_final=final)
    arguments = {"event": "voice_listen_completed", field: value}
    with pytest.raises((TypeError, ValueError)):
        MetricEvent(**arguments)

    class Sink:
        def record(self, _event):
            pytest.fail("transcript must not reach a metrics sink")

    event = arguments.pop("event")
    assert record_safely(Sink(), event, **arguments) is None
