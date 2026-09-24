"""Task 02 local transcript-authority gate; synthetic events open no real audio."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
from api.api_client import APIClient
from api.control_intents import parse_control
from api.conversation import ConversationSession
from api.conversation_control import ConversationFloorState
from api.providers.contracts import ChatMessage, ContentOrigin, Role
from api.realtime_conversation import RealtimeConversationController
from api.transcripts import (
    AuthoritativeUtterance,
    ProvisionalRevision,
    TranscriptBoundaryError,
    TranscriptCapacityError,
    TranscriptRevisionAggregator,
    authoritative_text,
)
from assistant import VoiceAssistant
from document.rag_system import RagSystem
from recognizer.speech_recognizer import RecognitionResult, SpeechRecognitionError, SpeechRecognizer
from recognizer.turn_endpoint_detector import TurnEndpointConfig


CONTRACT = Path(__file__).parent / "voice_suite" / "task02-contract.json"


def load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def controller() -> RealtimeConversationController:
    return RealtimeConversationController(
        endpointing=TurnEndpointConfig(),
        activity_energy=0.08,
        clock=lambda: 0.0,
    )


def test_task02_contract_is_versioned_and_has_independent_identity_cases() -> None:
    contract = load_contract()
    assert contract["schema_version"] == 1
    assert contract["contract_version"] == "1.0.0"
    assert contract["test_task"] == "02"
    scenarios = contract["scenarios"]
    assert len(scenarios) == 10
    assert len({case["id"] for case in scenarios}) == len(scenarios)
    assert {case["language"] for case in scenarios} == {"it", "en"}
    totals = {"provisional": 0, "final": 0, "ignored": 0}
    for case in scenarios:
        assert case["id"] and case["events"]
        assert case["expected_final_count"] == sum(
            event["expected"] == "final" for event in case["events"]
        )
        for event in case["events"]:
            assert set(event) == {
                "text",
                "is_final",
                "capture_id",
                "segment_id",
                "revision",
                "expected",
            }
            assert type(event["is_final"]) is bool
            assert all(
                type(event[key]) is int and event[key] > 0
                for key in (
                    "capture_id",
                    "segment_id",
                    "revision",
                )
            )
            assert isinstance(event["text"], str)
            assert event["expected"] in totals
            totals[event["expected"]] += 1
    assert totals == {"provisional": 10, "final": 14, "ignored": 12}


@pytest.mark.parametrize(
    "scenario",
    load_contract()["scenarios"],
    ids=lambda case: case["id"],
)
def test_observed_revisions_dispatch_only_declared_authoritative_segments(scenario: dict) -> None:
    realtime = controller()
    session = ConversationSession(id_factory=lambda: "task02-local-session")
    dispatched: list[str] = []
    try:
        for event in scenario["events"]:
            before = realtime.snapshot()
            pending_before = realtime.transcripts.provisional()
            result = realtime.observe(
                RecognitionResult(
                    event["text"],
                    event["is_final"],
                    capture_id=event["capture_id"],
                    segment_id=event["segment_id"],
                    revision=event["revision"],
                )
            )
            if event["expected"] == "ignored":
                assert result is None
                assert realtime.snapshot() == before
                assert realtime.transcripts.provisional() is pending_before
            elif event["expected"] == "provisional":
                assert isinstance(result, ProvisionalRevision)
                assert result.text == event["text"].strip()
                assert realtime.transcripts.provisional() is result
                assert realtime.snapshot().floor.state is ConversationFloorState.USER_SPEAKING
            else:
                assert isinstance(result, AuthoritativeUtterance)
                assert authoritative_text(result) == event["text"].strip()
                assert realtime.transcripts.provisional() is None
                assert realtime.snapshot().floor.state is ConversationFloorState.FINALIZING
                dispatched.append(authoritative_text(result))
                # Canonical history accepts this exact final only after promotion.
                turn = session.begin_turn(result)
                assert turn.user.content == event["text"].strip()
                session.complete_turn(turn, "Synthetic answer")
            assert len(dispatched) == session.snapshot().turn_count
        assert len(dispatched) == scenario["expected_final_count"]
        assert not realtime.transcripts.snapshot().pending or scenario["expected_final_count"] == 0
    finally:
        realtime.transcripts.clear_pending()
        realtime.stop()
    assert not realtime.transcripts.snapshot().pending
    assert not realtime.snapshot().capture_active


def test_provisional_value_is_rejected_at_all_available_downstream_text_boundaries() -> None:
    realtime = controller()
    provisional = realtime.observe(
        RecognitionResult(
            "Emilia private provisional",
            False,
            capture_id=50,
            segment_id=1,
            revision=1,
        )
    )
    assert isinstance(provisional, ProvisionalRevision)
    session = ConversationSession(id_factory=lambda: "task02-boundary-session")
    original = session.snapshot()

    # Uninitialized facades prove the authority check precedes provider, RAG,
    # assistant, or tool-side resource access. The session is the canonical
    # history store in this repository; there is no separate memory service.
    for facade, method in (
        (object.__new__(APIClient), "talk"),
        (object.__new__(APIClient), "think"),
        (object.__new__(RagSystem), "search"),
        (object.__new__(RagSystem), "retrieve"),
        (object.__new__(RagSystem), "run"),
        (object.__new__(VoiceAssistant), "process_command"),
        (object.__new__(VoiceAssistant), "_process_model_prompt"),
        (object.__new__(VoiceAssistant), "process_rag_command"),
    ):
        with pytest.raises(TranscriptBoundaryError):
            getattr(facade, method)(provisional)
    with pytest.raises(TranscriptBoundaryError):
        parse_control(provisional, language="en")
    with pytest.raises(TranscriptBoundaryError):
        session.begin_turn(provisional)
    for origin in (
        ContentOrigin.RAW_TRANSCRIPT,
        ContentOrigin.CONVERSATION_HISTORY,
        ContentOrigin.TOOL_RESULT,
        ContentOrigin.LOCAL_DOCUMENT,
    ):
        with pytest.raises(TranscriptBoundaryError):
            ChatMessage(Role.USER, provisional, origin=origin)
    assert session.snapshot() == original
    assert "private provisional" not in repr(realtime.snapshot())
    realtime.stop()


def test_concurrent_duplicate_final_has_one_history_admission() -> None:
    realtime = controller()
    barrier = threading.Barrier(6)
    final = RecognitionResult(
        "Emilia one final",
        True,
        capture_id=70,
        segment_id=1,
        revision=1,
    )

    def submit() -> AuthoritativeUtterance | None:
        barrier.wait(timeout=3)
        return realtime.observe(final)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(submit) for _ in range(6)]
        results = [future.result(timeout=5) for future in futures]
    admitted = [value for value in results if isinstance(value, AuthoritativeUtterance)]
    assert len(admitted) == 1
    assert results.count(None) == 5
    session = ConversationSession(id_factory=lambda: "task02-concurrent-session")
    for value in admitted:
        turn = session.begin_turn(value)
        session.complete_turn(turn, "Synthetic answer")
    assert session.snapshot().turn_count == 1
    realtime.stop()


def test_overflow_and_invalid_identity_do_not_create_authority_or_leak_content() -> None:
    aggregator = TranscriptRevisionAggregator(maximum_characters=16)
    pending = aggregator.observe(
        "pending",
        is_final=False,
        capture_id=1,
        segment_id=1,
        revision=1,
    )
    with pytest.raises(ValueError) as metadata_error:
        aggregator.observe("private text", is_final=True, capture_id=1, segment_id=1, revision=0)
    assert "private text" not in str(metadata_error.value)
    assert aggregator.provisional() is pending
    with pytest.raises(TranscriptCapacityError) as capacity_error:
        aggregator.observe(
            "private text exceeds limit", is_final=True, capture_id=1, segment_id=1, revision=2
        )
    assert "private text" not in str(capacity_error.value)
    assert aggregator.provisional() is None
    assert aggregator.snapshot().characters == 0
    assert (
        authoritative_text(
            aggregator.observe(
                "valid",
                is_final=True,
                capture_id=1,
                segment_id=1,
                revision=2,
            )
        )
        == "valid"
    )


def test_native_partial_revisions_do_not_call_provider_or_write_history() -> None:
    partials = ["Emilia open the door", "Emilia open my messages"]
    final = "Emilia open the calendar tomorrow"

    class Stream:
        stopped = False
        closed = False

        def start_stream(self) -> None:
            pass

        def read(self, frames: int, **_kwargs: object) -> bytes:
            return bytes(frames * 2)

        def stop_stream(self) -> None:
            self.stopped = True

        def close(self) -> None:
            self.closed = True

    class Audio:
        def __init__(self) -> None:
            self.stream = Stream()

        def open(self, **_kwargs: object) -> Stream:
            return self.stream

    class Native:
        def __init__(self, *_args: object) -> None:
            self.index = -1

        def AcceptWaveform(self, _data: bytes) -> bool:
            self.index += 1
            return self.index == len(partials)

        def PartialResult(self) -> str:
            return json.dumps({"partial": partials[self.index]})

        def Result(self) -> str:
            return json.dumps({"text": final})

    class Provider:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def chat(self, **kwargs: object):
            self.calls.append(kwargs)
            return iter(
                [
                    SimpleNamespace(
                        message=SimpleNamespace(content="Synthetic answer."),
                        done=True,
                        done_reason="stop",
                    )
                ]
            )

    class TTS:
        def __init__(self) -> None:
            self.spoken: list[str] = []

        def speak(self, text: str) -> None:
            self.spoken.append(text)

    audio = Audio()
    recognizer = SpeechRecognizer(
        model=object(),
        audio_interface=audio,
        recognizer_factory=Native,
    )
    provider, tts = Provider(), TTS()
    client = APIClient(client=provider, tts=tts, retry_wait=0, language="en")
    assistant = VoiceAssistant(
        settings=config.Settings(language="en", barge_in_enabled=False),
        speech_recognizer=recognizer,
        api_client=client,
        tts=tts,
    )
    observed: list[tuple[int, int, int]] = []
    original_observer = assistant._observe_transcript

    def observe(result: RecognitionResult):
        value = original_observer(result)
        if not result.is_final:
            assert not provider.calls
            assert client.conversation.snapshot().turn_count == 0
            assert not tts.spoken
            assert isinstance(value, ProvisionalRevision)
            observed.append((result.capture_id, result.segment_id, result.revision))
        return value

    assistant._observe_transcript = observe
    try:
        assert assistant.run_once() is True
        assert observed == [(1, 1, 1), (1, 1, 2)]
        assert len(provider.calls) == 1
        assert provider.calls[0]["messages"][-1]["content"] == "open the calendar tomorrow"
        assert client.conversation.snapshot().turn_count == 1
        assert not assistant.realtime.transcripts.snapshot().pending
        assert audio.stream.stopped and audio.stream.closed
    finally:
        assistant.close()


def test_capture_failure_after_partial_cleans_pending_without_dispatch() -> None:
    class FailingRecognizer:
        def listen_once(self, timeout: float, *, on_provisional, **_kwargs):
            assert timeout > 0
            on_provisional(
                RecognitionResult(
                    "Emilia private partial",
                    False,
                    capture_id=1,
                    segment_id=1,
                    revision=1,
                )
            )
            raise SpeechRecognitionError("synthetic decoder failure")

        def close(self) -> None:
            pass

    class NeverCalledAPI:
        def __init__(self) -> None:
            self.calls = 0

        def talk(self, *_args, **_kwargs):
            self.calls += 1
            pytest.fail("provisional transcript reached the provider")

        def close(self) -> None:
            pass

    class SilentTTS:
        def speak(self, _text: str) -> None:
            pytest.fail("provisional transcript reached speech output")

    api = NeverCalledAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(barge_in_enabled=False),
        speech_recognizer=FailingRecognizer(),
        api_client=api,
        tts=SilentTTS(),
    )
    try:
        with pytest.raises(SpeechRecognitionError, match="synthetic decoder failure"):
            assistant.run_once()
        assert api.calls == 0
        assert assistant.realtime.transcripts.provisional() is None
        assert not assistant.realtime.snapshot().capture_active
    finally:
        assistant.close()
