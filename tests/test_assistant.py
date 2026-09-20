from __future__ import annotations

import threading
import json
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

import assistant as assistant_module
import config
from api.api_client import APIClient
from api.conversation_control import ConversationFloor, ConversationFloorState
from api.metrics import SafeMetricsRecorder
from api.transcripts import AuthoritativeUtterance, TranscriptPromoter
from assistant import (
    AssistantRuntimeError,
    AssistantShutdownTimeout,
    AssistantState,
    VoiceAssistant,
    VoiceConversationState,
    _BargeInCaptureStop,
)
from audio.tts import PiperTTS
from audio.speech_pipeline import SpeechPipelineShutdownTimeout
from recognizer.barge_in_detector import BargeInDetector
from recognizer.speech_recognizer import RecognitionResult, SpeechRecognitionError, SpeechRecognizer


class FakeTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.closed = False

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def close(self) -> None:
        self.closed = True


class FakeAPI:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.think_messages: list[str] = []
        self.closed = False
        self.cancelled = False
        self.prepare_calls = 0
        self.local_prepare_calls = 0

    def talk(self, message: str, context: str | None = None) -> str:
        assert context is None
        self.messages.append(message)
        return "model response"

    def think(
        self,
        message: str,
        context: str | None = None,
        tts: bool = False,
    ) -> str:
        assert context is None
        assert tts is True
        self.think_messages.append(message)
        return "reasoned response"

    def close(self) -> None:
        self.closed = True

    def cancel_current(self) -> None:
        self.cancelled = True

    def prepare_remote_async(self) -> None:
        self.prepare_calls += 1

    def prepare_local_async(self) -> None:
        self.local_prepare_calls += 1


class FakeRecognizer:
    def __init__(self, results: list[RecognitionResult | None]) -> None:
        self.results = iter(results)
        self.closed = False
        self.prepare_calls = 0

    def prepare_async(self) -> None:
        self.prepare_calls += 1

    def listen_once(self, timeout: float) -> RecognitionResult | None:
        assert timeout > 0
        return next(self.results, None)

    def close(self) -> None:
        self.closed = True


def test_native_speech_shutdown_timeout_uses_bounded_terminal_shutdown_path():
    class API(FakeAPI):
        def close(self):
            raise SpeechPipelineShutdownTimeout("synthetic stuck native stage")

    assistant, tts, _api, _sounds, recognizer = make_assistant([])
    assistant.api_client = API()
    with pytest.raises(AssistantShutdownTimeout):
        assistant.close()
    assert assistant._close_complete.is_set()
    assert not tts.closed and not recognizer.closed


def test_modern_barge_capture_resets_stale_decoder_without_reopening_stream():
    response = Future()

    class TTS(FakeTTS):
        is_speaking = False
        active_playback_started_at = None
        active_playback_text = "unrelated response"

    tts = TTS()

    class Recognizer(FakeRecognizer):
        captures = 0

        def listen_events(self, timeout, *, stop_event, keep_open, reset_event, on_frame, on_segment_reset):
            self.captures += 1
            assert keep_open
            tts.is_speaking = True
            tts.active_playback_started_at = 10.0
            yield RecognitionResult("stale ambient words", False, frame_energy=0.2,
                                    capture_id=1, segment_id=1, revision=1, segment_started_at=9.0)
            assert reset_event.is_set()
            reset_event.clear()
            on_segment_reset()
            yield RecognitionResult("Emilia nuova domanda adesso", True, frame_energy=0.2,
                                    capture_id=1, segment_id=2, revision=1, segment_started_at=10.1,
                                    confidence=0.9, speech_duration_seconds=0.5, segment_peak_energy=0.2)

    recognizer = Recognizer([])
    assistant = VoiceAssistant(settings=config.Settings(), api_client=FakeAPI(), tts=tts,
                               speech_recognizer=recognizer, sound_player=FakeSoundPlayer(),
                               sound_executor=ImmediateExecutor(), clock=lambda: 10.3)
    try:
        assert assistant._listen_for_barge_in(response) == "Emilia nuova domanda adesso"
        assert recognizer.captures == 1
    finally:
        response.cancel()
        assistant.close()


def test_primary_capture_is_exclusive_and_close_waits_before_recognizer_teardown():
    entered = threading.Event()
    released = threading.Event()

    class CooperativeRecognizer(FakeRecognizer):
        def listen_once(self, timeout, *, stop_event):
            entered.set()
            for _ in range(200):
                if stop_event.is_set():
                    released.set()
                    return RecognitionResult("Emilia late final", True)
                threading.Event().wait(0.005)
            pytest.fail("primary capture did not receive stop")

        def close(self):
            assert released.is_set(), "recognizer closed before capture release"
            super().close()

    assistant, _tts, api, _sounds, _recognizer = make_assistant([])
    assistant.speech_recognizer = CooperativeRecognizer([])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(assistant.run_once)
        try:
            assert entered.wait(timeout=1)
            with pytest.raises(AssistantRuntimeError, match="already has an owner"):
                assistant.run_once()
            assistant.close()
            assert future.result(timeout=2) is False
            assert not assistant.realtime.snapshot().capture_active
            assert assistant.realtime.snapshot().stopped
            assert api.messages == []
        finally:
            assistant.stop()
            assistant.close()


def test_response_submission_failure_releases_floor_and_allows_next_iteration():
    class FailingOnceExecutor(ImmediateExecutor):
        failed = False

        def submit(self, function, *args):
            if not self.failed:
                self.failed = True
                raise RuntimeError("synthetic submission failure")
            return super().submit(function, *args)

    assistant, _tts, api, _sounds, _recognizer = make_assistant([
        RecognitionResult("Emilia first", True), RecognitionResult("Emilia second", True),
    ])
    original_executor = assistant._conversation_executor
    assistant._conversation_executor = FailingOnceExecutor()
    assistant._owns_conversation_executor = False
    try:
        with pytest.raises(RuntimeError, match="synthetic submission failure"):
            assistant.run_once()
        assert assistant.realtime.snapshot().response_id is None
        assert assistant.run_once()
        assert api.messages == ["second"]
    finally:
        assistant.close()
        original_executor.shutdown(wait=True)


class FakeSoundPlayer:
    def __init__(self) -> None:
        self.files: list[str] = []

    def play_sound(self, sound_file: str) -> None:
        self.files.append(sound_file)


class ImmediateExecutor:
    def submit(self, function: object, *args: object) -> Future[None]:
        future: Future[None] = Future()
        try:
            function(*args)  # type: ignore[operator]
        except Exception as exc:  # pragma: no cover - failure callback path
            future.set_exception(exc)
        else:
            future.set_result(None)
        return future


class FakeRag:
    def __init__(self) -> None:
        self.queries: list[tuple[str, int]] = []
        self.prepare_calls = 0

    def prepare(self) -> bool:
        self.prepare_calls += 1
        return True

    def run(self, query: str, top_k: int) -> str:
        self.queries.append((query, top_k))
        return "La risposta verificata"


def make_assistant(
    results: list[RecognitionResult | None],
    *,
    rag: FakeRag | None = None,
    metrics: SafeMetricsRecorder | None = None,
) -> tuple[VoiceAssistant, FakeTTS, FakeAPI, FakeSoundPlayer, FakeRecognizer]:
    tts = FakeTTS()
    api = FakeAPI()
    sounds = FakeSoundPlayer()
    recognizer = FakeRecognizer(results)
    assistant = VoiceAssistant(
        settings=config.Settings(project_root=config.PROJECT_ROOT, language="it"),
        tts=tts,
        sound_player=sounds,
        api_client=api,
        speech_recognizer=recognizer,
        rag_searcher=rag,
        sound_executor=ImmediateExecutor(),
        metrics=metrics,
    )
    return assistant, tts, api, sounds, recognizer


def test_command_requires_a_whole_wake_word() -> None:
    assistant, _tts, api, _sounds, _recognizer = make_assistant([])

    assert assistant.contains_wake_word("Ciao Emilia, aiutami")
    assert not assistant.contains_wake_word("La parola emiliana non è un richiamo")
    assert assistant.process_command("nessun richiamo") is None
    assert api.messages == []


def test_wake_word_is_removed_but_a_semantic_second_occurrence_is_preserved() -> None:
    assistant, _tts, api, _sounds, _recognizer = make_assistant([])

    assert assistant.process_command("Emilia, dimmi chi è Emilia?") == "model response"

    assert api.messages == ["dimmi chi è Emilia?"]


@pytest.mark.parametrize("trigger", ["pensa", "ragiona"])
def test_think_prefix_selects_think_mode_and_is_not_sent_to_the_model(trigger: str) -> None:
    assistant, _tts, api, _sounds, _recognizer = make_assistant([])

    assert (
        assistant.process_command(f"Emilia, {trigger}: confronta due strategie")
        == "reasoned response"
    )

    assert api.messages == []
    assert api.think_messages == ["confronta due strategie"]


def test_empty_wake_and_think_commands_are_ignored() -> None:
    assistant, _tts, api, _sounds, _recognizer = make_assistant([])

    assert assistant.process_command("Emilia") is None
    assert assistant.process_command("Emilia, pensa") is None

    assert api.messages == []
    assert api.think_messages == []


def test_wake_only_utterance_activates_and_acknowledges_the_conversation() -> None:
    assistant, tts, api, _sounds, _recognizer = make_assistant(
        [
            RecognitionResult("Emilia", is_final=True),
            RecognitionResult("dimmi qualcosa", is_final=True),
        ]
    )

    assert assistant.run_once() is True
    assert assistant.conversation_state.name == "LISTENING"
    assert tts.spoken == ["Certo."]
    assert api.messages == []

    assert assistant.run_once() is True
    assert api.messages == ["dimmi qualcosa"]
    assistant.close()


def test_first_wake_command_is_acknowledged_before_model_processing() -> None:
    class PreloadedTTS(FakeTTS):
        def speak_preloaded(self, phrase: str, *, cancellation: object | None = None) -> bool:
            del cancellation
            self.spoken.append(phrase)
            return True

    tts = PreloadedTTS()
    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(project_root=config.PROJECT_ROOT, language="it"),
        tts=tts,
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=FakeRecognizer(
            [RecognitionResult("Emilia, dimmi qualcosa", is_final=True)]
        ),
        sound_executor=ImmediateExecutor(),
    )

    assert assistant.run_once() is True
    assert tts.spoken == ["Certo."]
    assert api.messages == ["dimmi qualcosa"]
    assistant.close()


def test_partial_recognition_is_not_executed() -> None:
    assistant, _tts, api, _sounds, _recognizer = make_assistant(
        [RecognitionResult("emilia dimmi qualcosa", is_final=False)]
    )

    assert assistant.run_once() is False
    assert api.messages == []


def test_identified_revisions_dispatch_once_and_reject_stale_captures() -> None:
    def event(text, final, capture, segment, revision):
        return RecognitionResult(
            text, final, capture_id=capture, segment_id=segment, revision=revision
        )

    assistant, _tts, api, _sounds, _recognizer = make_assistant([
        event("Emilia synthetic partial", False, 1, 1, 1),
        event("Emilia synthetic revised", False, 1, 1, 2),
        event("Emilia synthetic final", True, 1, 1, 3),
        event("Emilia duplicate", True, 1, 1, 4),
        event("Emilia second segment", False, 1, 3, 1),
        event("Emilia stale segment", True, 1, 2, 2),
        event("Emilia new capture", True, 2, 1, 1),
        event("Emilia stale capture", True, 1, 4, 1),
    ])
    try:
        assert [assistant.run_once() for _ in range(8)] == [
            False, False, True, False, False, False, True, False
        ]
        assert api.messages == ["synthetic final", "new capture"]
    finally:
        assistant.close()


def test_rag_receives_only_one_authoritative_revision() -> None:
    rag = FakeRag()
    assistant, _tts, api, _sounds, _recognizer = make_assistant([
        RecognitionResult("provisional", False, capture_id=1, segment_id=1, revision=1),
        RecognitionResult("final query", True, capture_id=1, segment_id=1, revision=2),
        RecognitionResult("duplicate", True, capture_id=1, segment_id=1, revision=3),
    ], rag=rag)
    assistant.state = AssistantState.RAG
    try:
        assert assistant.run_once() is False
        assert assistant.run_once() is True
        assert assistant.run_once() is False
        assert rag.queries == [("final query", assistant.settings.top_k)]
        assert api.messages == []
    finally:
        assistant.close()


def test_legacy_text_iterator_exhaustion_cannot_promote_partial() -> None:
    class LegacyRecognizer:
        def listen(self, timeout):
            yield "Emilia incomplete"
            yield "Emilia revised incomplete"

        def close(self):
            pass

    assistant, _tts, api, _sounds, _recognizer = make_assistant([])
    assistant.speech_recognizer = LegacyRecognizer()
    try:
        assert assistant.run_once() is False
        assert api.messages == []
    finally:
        assistant.close()


def test_primary_adapter_preserves_all_recognition_metadata() -> None:
    result = RecognitionResult(
        "final", True, frame_energy=0.5, segment_id=2, segment_started_at=1.0,
        energy_reemit=True, confidence=0.9, speech_duration_seconds=0.8,
        segment_peak_energy=0.7, word_confidences=(0.9,), word_timings=((0.0, 0.8),),
        capture_id=3, revision=4,
    )
    assistant, *_ = make_assistant([result])
    try:
        assert assistant._recognize_once_unobserved() == result
    finally:
        assistant.close()


@pytest.mark.parametrize("language,partials,final", [
    ("en", ["Emilia Tuesday", "Emilia Tuesday actually"], "Emilia Tuesday... actually, Wednesday."),
    ("it", ["Emilia martedì", "Emilia martedì anzi"], "Emilia martedì... anzi, mercoledì."),
    ("en", ["Emilia very", "Emilia very very"], "Emilia very very carefully"),
    ("it", ["Emilia non", "Emilia non non"], "Emilia non non cambiare"),
    ("en", ["Emilia old words", "Emilia all revised"], "Emilia entirely new final wording"),
])
def test_native_revisions_deliver_one_exact_final_prompt(language, partials, final):
    class Stream:
        closed = False
        stopped = False

        def start_stream(self):
            pass

        def read(self, frames, **_kwargs):
            return bytes(frames * 2)

        def stop_stream(self):
            self.stopped = True

        def close(self):
            self.closed = True

    class Audio:
        stream = Stream()

        def open(self, **_kwargs):
            return self.stream

    class Native:
        def __init__(self, *_args):
            self.index = -1

        def AcceptWaveform(self, _data):
            self.index += 1
            return self.index == len(partials)

        def PartialResult(self):
            return json.dumps({"partial":partials[self.index]})

        def Result(self):
            return json.dumps({"text":final})

    audio = Audio()
    recognizer = SpeechRecognizer(model=object(), audio_interface=audio, recognizer_factory=Native)
    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(language=language, barge_in_enabled=False),
        speech_recognizer=recognizer, api_client=api, tts=FakeTTS(),
        sound_player=FakeSoundPlayer(), sound_executor=ImmediateExecutor(),
    )
    observed = []
    original = assistant._observe_transcript

    def observe(result):
        value = original(result)
        if not result.is_final:
            assert api.messages == []
            assert assistant._transcript_revisions.provisional().text == result.text
            observed.append(result.text)
        return value

    assistant._observe_transcript = observe
    try:
        assert assistant.run_once() is True
        assert observed == partials
        assert api.messages == [final.removeprefix("Emilia ")]
        assert audio.stream.closed and audio.stream.stopped
        assert not assistant._transcript_revisions.snapshot().pending
    finally:
        assistant.close()


def test_primary_provisional_observer_failure_releases_pending_text():
    class BrokenRecognizer(FakeRecognizer):
        def listen_once(self, timeout, *, on_provisional):
            on_provisional(RecognitionResult("private pending", False))
            raise SpeechRecognitionError("synthetic capture failure")

    assistant, _tts, api, _sounds, _recognizer = make_assistant([])
    assistant.speech_recognizer = BrokenRecognizer([])
    try:
        with pytest.raises(SpeechRecognitionError, match="synthetic capture failure"):
            assistant.run_once()
        assert assistant._transcript_revisions.provisional() is None
        assert api.messages == []
    finally:
        assistant.close()


@pytest.mark.parametrize("malformed", ["yes", 1, None])
def test_recognition_without_explicit_boolean_finality_cannot_dispatch(malformed) -> None:
    result = RecognitionResult("Emilia synthetic request", is_final=malformed)
    assistant, _tts, api, _sounds, _recognizer = make_assistant([result])
    try:
        assert assistant.run_once() is False
        assert api.messages == []
    finally:
        assistant.close()


def test_legacy_listen_once_string_contract_remains_final() -> None:
    assistant, _tts, api, _sounds, _recognizer = make_assistant(["Emilia synthetic request"])
    try:
        assert assistant.run_once() is True
        assert api.messages == ["synthetic request"]
    finally:
        assistant.close()


def test_public_command_accepts_promoted_utterance() -> None:
    assistant, _tts, api, _sounds, _recognizer = make_assistant([])
    final = TranscriptPromoter().observe("Emilia synthetic request", is_final=True)
    assert isinstance(final, AuthoritativeUtterance)
    try:
        assert assistant.process_command(final) == "model response"
        assert api.messages == ["synthetic request"]
    finally:
        assistant.close()


@pytest.mark.parametrize("stale", [False, True])
def test_barge_in_promotion_rejects_duplicate_and_stale_final_before_cancel(stale) -> None:
    class Detecting:
        def reset(self):
            pass

        def process_recognition(self, *_args, **_kwargs):
            return True

    final = RecognitionResult(
        "synthetic finalized follow up", True, frame_energy=0.5,
        capture_id=1, segment_id=1, revision=2,
    )

    class EventRecognizer(FakeRecognizer):
        def listen_events(self, timeout, *, stop_event):
            yield final

    assistant, _tts, api, _sounds, _recognizer = make_assistant([])
    assistant._barge_in_detector = Detecting()
    assistant.speech_recognizer = EventRecognizer([final])
    response: Future[str] = Future()
    try:
        if stale:
            assistant._observe_transcript(RecognitionResult(
                "new partial", False, capture_id=1, segment_id=2, revision=1
            ))
        else:
            assert assistant._listen_for_barge_in(response) == final.text
            assert api.cancelled
            api.cancelled = False
        assert assistant._listen_for_barge_in(response) is None
        assert api.cancelled is False
        assert assistant.run_once() is False
        assert api.messages == []
    finally:
        response.cancel()
        assistant.close()


def test_active_voice_conversation_accepts_follow_up_without_wake_word() -> None:
    tts = FakeTTS()
    api = FakeAPI()
    recognizer = FakeRecognizer(
        [
            RecognitionResult("Emilia, name three planets", is_final=True),
            RecognitionResult("only discuss the second one", is_final=True),
        ]
    )
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            language="it",
            barge_in_enabled=True,
        ),
        tts=tts,
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=recognizer,
        barge_in_detector=object(),
        sound_executor=ImmediateExecutor(),
    )

    assert assistant.run_once()
    assert assistant.run_once()
    assert api.messages == ["name three planets", "only discuss the second one"]
    assistant.close()


def test_voice_conversation_requires_wake_word_again_after_idle_timeout() -> None:
    now = [0.0]
    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            language="it",
            barge_in_enabled=True,
            llm=config.LLMSettings(context_idle_timeout_seconds=5),
        ),
        tts=FakeTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=FakeRecognizer(
            [
                RecognitionResult("Emilia, first question", is_final=True),
                RecognitionResult("late follow-up", is_final=True),
            ]
        ),
        barge_in_detector=object(),
        sound_executor=ImmediateExecutor(),
        clock=lambda: now[0],
    )

    assert assistant.run_once()
    now[0] = 5.0
    assert assistant.run_once() is False
    assert api.messages == ["first question"]
    assistant.close()


def test_rag_state_transition_has_no_startup_warmup_query() -> None:
    rag = FakeRag()
    assistant, tts, api, sounds, _recognizer = make_assistant(
        [
            RecognitionResult("regolamento", is_final=True),
            RecognitionResult("quanta acqua posso usare", is_final=True),
        ],
        rag=rag,
    )

    assert rag.queries == []
    assert assistant.run_once()
    assert assistant.state is AssistantState.RAG
    assert rag.queries == []
    assert rag.prepare_calls == 1
    assert api.messages == []

    assert assistant.run_once()
    assert assistant.state is AssistantState.COMMAND
    assert rag.queries == [("quanta acqua posso usare", assistant.settings.top_k)]
    assert api.messages == []
    assert tts.spoken == ["Ecco cosa ho trovato: La risposta verificata"]
    assert sounds.files == [
        str(assistant.settings.wake_sound),
        str(assistant.settings.stop_sound),
    ]


def test_successful_rag_retrieval_is_not_reclassified_when_tts_fails() -> None:
    class FailingTTS(FakeTTS):
        def speak(self, text: str) -> None:
            del text
            raise RuntimeError("audio unavailable")

    metrics = SafeMetricsRecorder()
    assistant, _tts, _api, _sounds, _recognizer = make_assistant(
        [],
        rag=FakeRag(),
        metrics=metrics,
    )
    assistant.tts = FailingTTS()

    with pytest.raises(Exception, match="present the RAG result"):
        assistant.process_rag_command("query")

    names = [event.event for event in metrics.snapshot()]
    assert names.count("rag_completed") == 1
    assert "rag_failed" not in names
    assert names.count("tts_failed") == 1


def test_recognized_command_is_counted_once_without_inventing_stt_latency() -> None:
    metrics = SafeMetricsRecorder()
    assistant, _tts, _api, _sounds, _recognizer = make_assistant(
        [RecognitionResult("Emilia dimmi qualcosa", is_final=True)],
        metrics=metrics,
    )

    assert assistant.run_once() is True

    events = metrics.snapshot()
    listen = next(event for event in events if event.event == "voice_listen_completed")
    command = next(event for event in events if event.event == "voice_command_completed")
    assert listen.listening_ms is not None
    assert listen.stt_ms is None
    assert listen.recognized_count == 0
    assert command.recognized_count == 1
    assert sum(event.recognized_count for event in events) == 1


def test_close_releases_services_once() -> None:
    assistant, tts, api, _sounds, recognizer = make_assistant([])

    assistant.close()
    assistant.close()

    assert tts.closed
    assert api.closed
    assert recognizer.closed


@pytest.mark.parametrize(
    "legacy,expected",
    [
        (VoiceConversationState.IDLE, ConversationFloorState.IDLE),
        (VoiceConversationState.LISTENING, ConversationFloorState.ARMED),
        (VoiceConversationState.USER_TURN_FINALIZED, ConversationFloorState.FINALIZING),
        (VoiceConversationState.GENERATING, ConversationFloorState.THINKING),
        (VoiceConversationState.SPEAKING, ConversationFloorState.ASSISTANT_SPEAKING),
        (VoiceConversationState.BARGE_IN_DETECTED, ConversationFloorState.INTERRUPTED),
        (VoiceConversationState.CANCELLING, ConversationFloorState.INTERRUPTED),
        (VoiceConversationState.CAPTURING_FOLLOW_UP, ConversationFloorState.USER_SPEAKING),
        (VoiceConversationState.FOLLOW_UP_FINALIZED, ConversationFloorState.FINALIZING),
    ],
)
def test_legacy_state_has_a_read_only_floor_adapter(legacy, expected) -> None:
    assert legacy.to_floor_state() is expected
    assert list(VoiceConversationState)[legacy.value - 1] is legacy


def test_floor_controller_drives_existing_voice_dispatch(monkeypatch) -> None:
    events = []
    original = ConversationFloor.apply

    def control_dispatch(floor, event):
        events.append(event.kind.value)
        return original(floor, event)

    monkeypatch.setattr(ConversationFloor, "apply", control_dispatch)
    assistant, _tts, api, _sounds, _recognizer = make_assistant([
        RecognitionResult("Emilia", is_final=True),
        RecognitionResult("synthetic provisional", is_final=False),
        RecognitionResult("synthetic follow up", is_final=True),
    ])
    try:
        assert assistant.conversation_state.to_floor_state() is ConversationFloorState.IDLE
        assert assistant.run_once()
        assert assistant.conversation_state.to_floor_state() is ConversationFloorState.ARMED
        assert not assistant.run_once()
        assert api.messages == []
        assert assistant.run_once()
        assert api.messages == ["synthetic follow up"]
        assert assistant.conversation_state is VoiceConversationState.LISTENING
        assert "provisional_speech" in events
        assert "final_speech" in events
        assert events.count("generation_started") == 1
        assert events.count("response_finished") == 1
    finally:
        assistant.close()


def test_stop_cancels_the_active_model_stream() -> None:
    assistant, _tts, api, _sounds, _recognizer = make_assistant([])

    assistant.stop()

    assert not assistant._running
    assert api.cancelled


def test_run_prepares_models_before_startup_greeting() -> None:
    assistant, tts, api, _sounds, recognizer = make_assistant([])

    assistant.run(max_iterations=0)

    assert api.prepare_calls == 1
    assert api.local_prepare_calls == 1
    assert recognizer.prepare_calls == 1
    assert tts.spoken == [
        assistant.profile.welcome_message.format(
            wake_word=assistant.profile.wake_word,
        )
    ]


def test_run_waits_for_startup_tts_preload_before_first_listen() -> None:
    events: list[str] = []
    listening = threading.Event()

    class PreloadingTTS(FakeTTS):
        def speak(self, text: str) -> None:
            assert text == assistant.profile.welcome_message.format(
                wake_word=assistant.profile.wake_word,
            )
            events.append("welcome")

        def preload_phrases(self, phrases: tuple[str, ...]) -> None:
            assert phrases[0] == assistant.profile.welcome_message.format(
                wake_word=assistant.profile.wake_word,
            )
            assert phrases[1:4] == ("Certo.", "Un momento.", "Vediamo.")
            events.append("preload_started")
            events.append("preload_finished")

    class OrderingRecognizer(FakeRecognizer):
        def listen_once(self, timeout: float) -> RecognitionResult | None:
            events.append("listen")
            listening.set()
            return super().listen_once(timeout)

    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            language="it",
            barge_in_enabled=True,
        ),
        tts=PreloadingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=FakeAPI(),
        speech_recognizer=OrderingRecognizer([None]),
        barge_in_detector=object(),
        sound_executor=ImmediateExecutor(),
    )

    assistant.run(max_iterations=1)

    assert events.index("preload_finished") < events.index("welcome")
    assert events.index("welcome") < events.index("listen")


def test_run_preloads_startup_speech_on_main_thread_with_shutdown_token() -> None:
    calling_thread = threading.get_ident()
    observations: list[tuple[int, bool]] = []

    class PreloadingTTS(FakeTTS):
        def preload_phrases(
            self,
            phrases: tuple[str, ...],
            *,
            stop_event: threading.Event,
        ) -> tuple[str, ...]:
            observations.append((threading.get_ident(), stop_event.is_set()))
            return phrases

        def has_preloaded_phrase(self, _phrase: str) -> bool:
            return True

    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            language="it",
            barge_in_enabled=True,
        ),
        tts=PreloadingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=FakeAPI(),
        speech_recognizer=FakeRecognizer([]),
        sound_executor=ImmediateExecutor(),
    )

    assistant.run(max_iterations=0)

    assert len(observations) == 1
    assert observations[0][0] == calling_thread
    assert observations[0][1] is False


def test_close_cancels_in_flight_response_before_joining_owned_executor() -> None:
    class BlockingAPI(FakeAPI):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def talk(self, message: str, context: str | None = None) -> str:
            assert message == "domanda"
            assert context is None
            self.started.set()
            assert self.release.wait(timeout=2)
            return "cancelled response"

        def cancel_current(self) -> None:
            super().cancel_current()
            self.release.set()

    api = BlockingAPI()
    conversation_executor = ThreadPoolExecutor(max_workers=2)
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            language="it",
            barge_in_enabled=True,
        ),
        tts=FakeTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=FakeRecognizer([RecognitionResult("Emilia, domanda", is_final=True)]),
        barge_in_detector=object(),
        conversation_executor=conversation_executor,
    )
    runner = threading.Thread(target=assistant.run_once)
    runner.start()
    assert api.started.wait(timeout=1)

    assistant.close()
    runner.join(timeout=1)

    assert not runner.is_alive()
    assert api.cancelled is True
    assert api.closed is True
    assert conversation_executor.submit(lambda: "caller-owned").result() == "caller-owned"
    conversation_executor.shutdown(wait=True)


def test_close_retires_provider_before_waiting_for_model_worker() -> None:
    events: list[str] = []
    started = threading.Event()
    release = threading.Event()

    class CloseUnblocksAPI(FakeAPI):
        def cancel_current(self) -> None:
            events.append("cancel")

        def close(self) -> None:
            events.append("provider_close")
            release.set()
            super().close()

    assistant = VoiceAssistant(
        settings=config.Settings(project_root=config.PROJECT_ROOT),
        tts=FakeTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=CloseUnblocksAPI(),
        speech_recognizer=FakeRecognizer([]),
    )

    def blocked_model_worker() -> None:
        started.set()
        assert release.wait(timeout=1)
        events.append("worker_done")

    assistant._submit_task(assistant._conversation_executor, blocked_model_worker)
    assert started.wait(timeout=1)

    assistant.close()

    assert events.index("provider_close") < events.index("worker_done")


def test_close_is_retryable_if_teardown_is_interrupted(monkeypatch) -> None:
    assistant = VoiceAssistant(
        settings=config.Settings(project_root=config.PROJECT_ROOT),
        tts=FakeTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=FakeAPI(),
        speech_recognizer=FakeRecognizer([]),
    )
    real_wait = assistant_module.wait
    calls = 0

    def interrupted_wait(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt
        return real_wait(*args, **kwargs)

    monkeypatch.setattr(assistant_module, "wait", interrupted_wait)

    with pytest.raises(KeyboardInterrupt):
        assistant.close()
    assert assistant._close_complete.is_set() is False
    assert assistant._closing is False

    assistant.close()
    assert assistant._close_complete.is_set() is True


def test_owned_worker_shutdown_timeout_is_explicit(monkeypatch) -> None:
    monkeypatch.setattr(assistant_module, "_TASK_SHUTDOWN_TIMEOUT_SECONDS", 0.01)
    release = threading.Event()
    started = threading.Event()
    assistant = VoiceAssistant(
        settings=config.Settings(project_root=config.PROJECT_ROOT),
        tts=FakeTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=FakeAPI(),
        speech_recognizer=FakeRecognizer([]),
    )

    def stuck_worker() -> None:
        started.set()
        release.wait(timeout=1)

    future = assistant._submit_task(assistant._conversation_executor, stuck_worker)
    assert started.wait(timeout=1)

    with pytest.raises(AssistantShutdownTimeout):
        assistant.close()

    release.set()
    future.result(timeout=1)


def test_echo_epoch_tracks_active_playback_and_only_a_short_completed_tail() -> None:
    tts = FakeTTS()
    tts.active_playback_started_at = 10.0
    tts.last_playback_window = (10.0, None)
    assistant = VoiceAssistant(
        settings=config.Settings(project_root=config.PROJECT_ROOT),
        tts=tts,
        sound_player=FakeSoundPlayer(),
        api_client=FakeAPI(),
        speech_recognizer=FakeRecognizer([]),
        sound_executor=ImmediateExecutor(),
    )

    assert assistant._tts_echo_epoch_start(10.5) == 10.0

    tts.active_playback_started_at = None
    tts.last_playback_window = (10.0, 10.5)
    assert assistant._tts_echo_epoch_start(10.7) == 10.0
    assert assistant._tts_echo_epoch_start(10.76) is None
    assistant.close()


def test_final_follow_up_before_playback_requires_detector_acceptance() -> None:
    class IdleTTS(FakeTTS):
        is_speaking = False
        active_playback_started_at = None
        last_playback_window = None

    class EventRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            assert getattr(stop_event, "is_set")() is False
            yield RecognitionResult(
                "questa è una correzione",
                is_final=True,
                frame_energy=0.5,
            )

    class CountingDetector:
        def __init__(self) -> None:
            self.calls = 0

        def reset(self) -> None:
            pass

        def process_recognition(self, *_args: object, **_kwargs: object) -> bool:
            self.calls += 1
            return True

    detector = CountingDetector()
    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=IdleTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=EventRecognizer([]),
        barge_in_detector=detector,
        sound_executor=ImmediateExecutor(),
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) == "questa è una correzione"
    assert detector.calls == 1
    assert api.cancelled is True
    response.cancel()
    assistant.close()


def test_low_energy_final_before_playback_does_not_cancel_generation() -> None:
    class IdleTTS(FakeTTS):
        is_speaking = False
        active_playback_started_at = None
        last_playback_window = None

    class EventRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult("certo un momento", is_final=True, frame_energy=None)

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=IdleTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=EventRecognizer([]),
        sound_executor=ImmediateExecutor(),
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) is None
    assert api.cancelled is False
    response.cancel()
    assistant.close()


def test_recognized_tts_text_during_playback_does_not_trigger_barge_in() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Ci sono otto pianeti del sistema solare."

    class EchoRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult("ci sono otto", is_final=False, frame_energy=0.2)
            yield RecognitionResult(
                "ci sono otto pianeti del sistema solare",
                is_final=True,
                frame_energy=0.2,
            )

    class CountingDetector:
        def __init__(self) -> None:
            self.calls = 0

        def reset(self) -> None:
            pass

        def process_recognition(self, *_args: object, **_kwargs: object) -> bool:
            self.calls += 1
            return True

    detector = CountingDetector()
    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=EchoRecognizer([]),
        barge_in_detector=detector,
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.8,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) is None
    assert detector.calls == 0
    assert api.cancelled is False
    response.cancel()
    assistant.close()


def test_tts_echo_segment_stays_suppressed_when_vosk_revision_diverges() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Nel sistema solare ci sono otto pianeti."

    class RevisingEchoRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "nel sistema solare ci sono otto",
                is_final=False,
                frame_energy=0.2,
            )
            yield RecognitionResult(
                "nel sistema suonare ci sono molto",
                is_final=False,
                frame_energy=0.2,
            )
            yield RecognitionResult(
                "nel sistema suonare ci sono molti pianeti",
                is_final=True,
                frame_energy=0.2,
            )

    class CountingDetector:
        def __init__(self) -> None:
            self.calls = 0

        def reset(self) -> None:
            pass

        def process_recognition(self, *_args: object, **_kwargs: object) -> bool:
            self.calls += 1
            return True

    detector = CountingDetector()
    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=RevisingEchoRecognizer([]),
        barge_in_detector=detector,
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.8,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) is None
    assert detector.calls == 0
    assert api.cancelled is False
    response.cancel()
    assistant.close()


def test_first_mistranscribed_tts_candidate_is_suppressed_by_word_similarity() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Nel sistema solare ci sono otto pianeti."

    class MistranscribingRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "nel sistema suonare ci sono molti pianeti",
                is_final=True,
                frame_energy=0.2,
            )

    class CountingDetector:
        def __init__(self) -> None:
            self.calls = 0

        def reset(self) -> None:
            pass

        def process_recognition(self, *_args: object, **_kwargs: object) -> bool:
            self.calls += 1
            return True

    detector = CountingDetector()
    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=MistranscribingRecognizer([]),
        barge_in_detector=detector,
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.8,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) is None
    assert detector.calls == 0
    assert api.cancelled is False
    response.cancel()
    assistant.close()


def test_phonetically_similar_echo_with_changed_word_boundaries_is_suppressed() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = (
            "Le stelle brillavano, un eco di possibilità infinite, mentre il mio algoritmo."
        )

    assistant = VoiceAssistant(
        settings=config.Settings(project_root=config.PROJECT_ROOT),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=FakeAPI(),
        speech_recognizer=FakeRecognizer([]),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.8,
    )

    assert assistant._matches_current_tts_echo(
        "le stalle brillava eco possibilità finite mentre mio algoritmo",
        10.8,
    )
    assert not assistant._matches_current_tts_echo(
        "no parlami soltanto di marte",
        10.8,
    )
    assistant.close()


def test_short_mistranscribed_echo_is_suppressed_before_partial_confirmation() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Marte, un deserto di rosso, con tracce di vita passata."

    class ShortEchoRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "parte un",
                is_final=False,
                frame_energy=0.2,
                segment_id=1,
                segment_started_at=10.1,
            )
            yield RecognitionResult(
                "parte un deserto",
                is_final=False,
                frame_energy=0.2,
                segment_id=1,
                segment_started_at=10.1,
            )
            yield RecognitionResult(
                "parte un deserto di rosso",
                is_final=True,
                frame_energy=0.2,
                segment_id=1,
                segment_started_at=10.1,
            )

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=ShortEchoRecognizer([]),
        barge_in_detector=BargeInDetector(minimum_active_seconds=0.0),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.8,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) is None
    assert api.cancelled is False
    response.cancel()
    assistant.close()


def test_energy_only_reemit_crossing_playback_onset_suppresses_whole_segment() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Una frase diversa riprodotta dall'altoparlante."

    class StaleRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "vecchia ipotesi incompleta",
                is_final=False,
                frame_energy=0.3,
                segment_id=4,
                segment_started_at=9.5,
                energy_reemit=True,
            )
            yield RecognitionResult(
                "vecchia ipotesi diventata testo eco",
                is_final=True,
                frame_energy=0.3,
                segment_id=4,
                segment_started_at=9.5,
            )

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=StaleRecognizer([]),
        barge_in_detector=BargeInDetector(minimum_active_seconds=0.0),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.1,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) is None
    assert api.cancelled is False
    response.cancel()
    assistant.close()


def test_low_energy_pre_playback_partial_cannot_arm_post_playback_echo() -> None:
    class StartingTTS(FakeTTS):
        is_speaking = False
        active_playback_started_at = None
        active_playback_text = "Una frase pronunciata dall'altoparlante."

    class BoundaryRecognizer(FakeRecognizer):
        def __init__(self, tts: StartingTTS) -> None:
            super().__init__([])
            self.tts = tts

        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "ipotesi ambientale vecchia",
                is_final=False,
                frame_energy=0.05,
                segment_id=3,
                segment_started_at=9.5,
            )
            self.tts.is_speaking = True
            self.tts.active_playback_started_at = 10.0
            yield RecognitionResult(
                "ipotesi ambientale diventata eco",
                is_final=False,
                frame_energy=0.5,
                segment_id=3,
                segment_started_at=9.5,
            )
            yield RecognitionResult(
                "ipotesi ambientale diventata eco finale",
                is_final=True,
                frame_energy=0.5,
                segment_id=3,
                segment_started_at=9.5,
            )

    tts = StartingTTS()
    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=tts,
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=BoundaryRecognizer(tts),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.2,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) is None
    assert api.cancelled is False
    response.cancel()
    assistant.close()


def test_high_energy_user_speech_can_cross_from_synthesis_into_playback() -> None:
    class StartingTTS(FakeTTS):
        is_speaking = False
        active_playback_started_at = None
        active_playback_text = "Una risposta non correlata."

    class CrossOnsetUserRecognizer(FakeRecognizer):
        def __init__(self, tts: StartingTTS) -> None:
            super().__init__([])
            self.tts = tts

        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "no cambia questo",
                is_final=False,
                frame_energy=0.2,
                segment_id=5,
                segment_started_at=9.5,
            )
            self.tts.is_speaking = True
            self.tts.active_playback_started_at = 10.0
            yield RecognitionResult(
                "no cambia questo argomento",
                is_final=True,
                frame_energy=0.01,
                segment_id=5,
                segment_started_at=9.5,
            )

    tts = StartingTTS()
    api = FakeAPI()
    observed = iter([9.8, 10.2, 10.2])
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=tts,
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=CrossOnsetUserRecognizer(tts),
        sound_executor=ImmediateExecutor(),
        clock=lambda: next(observed),
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) == "no cambia questo argomento"
    assert api.cancelled is True
    response.cancel()
    assistant.close()


def test_pending_user_speech_survives_playback_gap_without_early_detection() -> None:
    class FragmentedTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Un frammento non correlato."

    class GapRecognizer(FakeRecognizer):
        def __init__(self, tts: FragmentedTTS) -> None:
            super().__init__([])
            self.tts = tts

        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "no cambia",
                is_final=False,
                frame_energy=0.2,
                segment_id=6,
                segment_started_at=10.2,
            )
            self.tts.is_speaking = False
            self.tts.active_playback_started_at = None
            yield RecognitionResult(
                "no cambia argomento",
                is_final=False,
                frame_energy=0.2,
                segment_id=6,
                segment_started_at=10.2,
            )
            self.tts.is_speaking = True
            self.tts.active_playback_started_at = 11.0
            yield RecognitionResult(
                "no cambia argomento adesso",
                is_final=True,
                frame_energy=0.01,
                segment_id=6,
                segment_started_at=10.2,
            )

    tts = FragmentedTTS()
    api = FakeAPI()
    observed = iter([10.3, 10.8, 11.2, 11.2])
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=tts,
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=GapRecognizer(tts),
        sound_executor=ImmediateExecutor(),
        clock=lambda: next(observed),
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) == "no cambia argomento adesso"
    assert api.cancelled is True
    response.cancel()
    assistant.close()


def test_first_event_short_final_during_playback_does_not_interrupt() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Certo."

    class ShortEchoRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "cerco",
                is_final=True,
                frame_energy=0.5,
                segment_id=1,
                segment_started_at=10.1,
            )

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=ShortEchoRecognizer([]),
        barge_in_detector=BargeInDetector(minimum_active_seconds=0.0),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.2,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) is None
    assert api.cancelled is False
    response.cancel()
    assistant.close()


@pytest.mark.parametrize("command", ("stop", "basta", "fermati"))
def test_high_energy_explicit_short_final_interrupts_playback(command: str) -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Una risposta non correlata continua."

    class CommandRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                command,
                is_final=True,
                frame_energy=0.2,
                segment_id=1,
                segment_started_at=10.1,
                confidence=0.9,
                speech_duration_seconds=0.2,
                segment_peak_energy=0.2,
            )

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=CommandRecognizer([]),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.2,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) == command
    assert api.cancelled is True
    response.cancel()
    assistant.close()


def test_high_confidence_wake_word_interrupts_playback() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Una risposta non correlata continua."

    class WakeRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "Emilia",
                is_final=True,
                frame_energy=0.2,
                segment_id=1,
                segment_started_at=10.1,
                confidence=0.9,
                speech_duration_seconds=0.25,
                segment_peak_energy=0.2,
            )

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=WakeRecognizer([]),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.2,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) == "Emilia"
    assert api.cancelled is True
    response.cancel()
    assistant.close()


def test_stale_segment_at_playback_start_reopens_capture_for_user_speech() -> None:
    class StartingTTS(FakeTTS):
        is_speaking = False
        active_playback_started_at = None
        active_playback_text = "Una risposta non correlata continua."

    class RestartingRecognizer(FakeRecognizer):
        def __init__(self, tts: StartingTTS) -> None:
            super().__init__([])
            self.tts = tts
            self.calls = 0

        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            self.calls += 1
            if self.calls == 1:
                self.tts.is_speaking = True
                self.tts.active_playback_started_at = 10.0
                yield RecognitionResult(
                    "vecchia ipotesi ambientale",
                    is_final=False,
                    frame_energy=0.3,
                    segment_id=1,
                    segment_started_at=9.5,
                    energy_reemit=True,
                )
                pytest.fail("the stale capture session should have been closed")
            else:
                common = {
                    "frame_energy": 0.2,
                    "segment_id": 1,
                    "segment_started_at": 10.2,
                    "confidence": 0.9,
                    "speech_duration_seconds": 0.5,
                    "segment_peak_energy": 0.2,
                }
                yield RecognitionResult(
                    "Emilia nuova domanda",
                    is_final=False,
                    **common,
                )
                yield RecognitionResult(
                    "Emilia nuova domanda adesso",
                    is_final=True,
                    **common,
                )

    tts = StartingTTS()
    recognizer = RestartingRecognizer(tts)
    api = FakeAPI()
    observed = iter((10.1, 10.3, 10.6))
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=tts,
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=recognizer,
        barge_in_detector=BargeInDetector(minimum_active_seconds=0.0),
        sound_executor=ImmediateExecutor(),
        clock=lambda: next(observed),
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) == "Emilia nuova domanda adesso"
    assert recognizer.calls == 2
    assert api.cancelled is True
    response.cancel()
    assistant.close()


def test_strong_final_can_interrupt_after_vosk_revises_the_partial() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Una risposta non correlata continua."

    class RevisedRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "ipotesi ambientale precedente",
                is_final=False,
                frame_energy=0.2,
                segment_id=1,
                segment_started_at=10.1,
            )
            yield RecognitionResult(
                "accendi la luce del soggiorno",
                is_final=True,
                frame_energy=0.2,
                segment_id=1,
                segment_started_at=10.1,
                confidence=0.9,
                speech_duration_seconds=0.8,
                segment_peak_energy=0.2,
            )

    tts = SpeakingTTS()
    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=tts,
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=RevisedRecognizer([]),
        barge_in_detector=BargeInDetector(minimum_active_seconds=0.0),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.2,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) == "accendi la luce del soggiorno"
    assert api.cancelled is True
    response.cancel()
    assistant.close()


def test_stale_segment_is_suppressed_across_a_new_tts_buffer() -> None:
    class FragmentedTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Primo frammento pronunciato."

    class FragmentBoundaryRecognizer(FakeRecognizer):
        def __init__(self, tts: FragmentedTTS) -> None:
            super().__init__([])
            self.tts = tts

        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "ipotesi non confermata qui",
                is_final=False,
                frame_energy=0.5,
                segment_id=9,
                segment_started_at=9.5,
            )
            self.tts.active_playback_started_at = 11.0
            self.tts.active_playback_text = "Secondo frammento pronunciato."
            yield RecognitionResult(
                "ipotesi non confermata diventata eco",
                is_final=False,
                frame_energy=0.5,
                segment_id=9,
                segment_started_at=9.5,
            )
            yield RecognitionResult(
                "ipotesi non confermata diventata eco finale",
                is_final=True,
                frame_energy=0.5,
                segment_id=9,
                segment_started_at=9.5,
            )

    tts = FragmentedTTS()
    api = FakeAPI()
    observed = iter([10.2, 11.2, 11.3])
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=tts,
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=FragmentBoundaryRecognizer(tts),
        barge_in_detector=BargeInDetector(minimum_active_seconds=0.0),
        sound_executor=ImmediateExecutor(),
        clock=lambda: next(observed),
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) is None
    assert api.cancelled is False
    response.cancel()
    assistant.close()


def test_real_barge_in_preserves_pending_peak_across_tts_buffers() -> None:
    class FragmentedTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Primo frammento senza parole correlate."

    class UserAcrossBoundaryRecognizer(FakeRecognizer):
        def __init__(self, tts: FragmentedTTS) -> None:
            super().__init__([])
            self.tts = tts

        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "no cambia questo",
                is_final=False,
                frame_energy=0.2,
                segment_id=7,
                segment_started_at=10.2,
            )
            self.tts.active_playback_started_at = 11.0
            self.tts.active_playback_text = "Secondo frammento ancora non correlato."
            yield RecognitionResult(
                "no cambia questo argomento",
                is_final=True,
                frame_energy=0.01,
                segment_id=7,
                segment_started_at=10.2,
            )

    class ThresholdPolicy:
        def should_suppress(
            self,
            frame_energy: float,
            elapsed_since_tts_start: float,
        ) -> bool:
            del elapsed_since_tts_start
            return frame_energy < 0.1

    tts = FragmentedTTS()
    api = FakeAPI()
    observed = iter([10.3, 11.2, 11.2])
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=tts,
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=UserAcrossBoundaryRecognizer(tts),
        barge_in_detector=BargeInDetector(
            minimum_active_seconds=0.0,
            suppression_policy=ThresholdPolicy(),
        ),
        sound_executor=ImmediateExecutor(),
        clock=lambda: next(observed),
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) == "no cambia questo argomento"
    assert api.cancelled is True
    response.cancel()
    assistant.close()


def test_echo_suppression_does_not_leak_into_the_next_vosk_segment() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Questa frase arriva dall'altoparlante."

    class EchoThenUserRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "questa frase arriva",
                is_final=False,
                frame_energy=0.2,
                segment_id=1,
                segment_started_at=10.1,
            )
            # Vosk may close segment 1 with an empty final, which is not yielded.
            yield RecognitionResult(
                "no parlami della",
                is_final=False,
                frame_energy=0.2,
                segment_id=2,
                segment_started_at=10.5,
            )
            yield RecognitionResult(
                "no parlami della luna",
                is_final=True,
                frame_energy=0.2,
                segment_id=2,
                segment_started_at=10.5,
            )

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=EchoThenUserRecognizer([]),
        barge_in_detector=BargeInDetector(minimum_active_seconds=0.0),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.8,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) == "no parlami della luna"
    assert api.cancelled is True
    response.cancel()
    assistant.close()


def test_tts_echo_prefix_is_removed_before_follow_up_is_returned() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Nel sistema solare ci sono otto pianeti."

    class EchoThenCorrectionRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            common = {
                "frame_energy": 0.2,
                "segment_id": 4,
                "segment_started_at": 10.1,
                "confidence": 0.9,
                "speech_duration_seconds": 0.8,
                "segment_peak_energy": 0.2,
            }
            yield RecognitionResult(
                "nel sistema solare ci sono otto pianeti",
                is_final=False,
                **common,
            )
            yield RecognitionResult(
                "nel sistema solare ci sono otto pianeti no parlami della luna",
                is_final=False,
                **common,
            )
            yield RecognitionResult(
                "nel sistema solare ci sono otto pianeti no parlami della luna",
                is_final=True,
                **common,
            )

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=EchoThenCorrectionRecognizer([]),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.8,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) == "no parlami della luna"
    assert api.cancelled is True
    response.cancel()
    assistant.close()


def test_short_token_is_not_removed_as_substring_of_a_tts_word() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Ora posso stoppare la riproduzione."

    assistant = VoiceAssistant(
        settings=config.Settings(project_root=config.PROJECT_ROOT),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=FakeAPI(),
        speech_recognizer=FakeRecognizer([]),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.5,
    )

    assert assistant._remove_current_tts_echo("stop", 10.5) == ("stop", ())
    assistant.close()


def test_strong_final_after_short_cleaned_echo_residual_interrupts() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Nel sistema solare ci sono otto pianeti."

    class JumpingRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            common = {
                "frame_energy": 0.2,
                "segment_id": 15,
                "segment_started_at": 10.1,
                "confidence": 0.9,
                "speech_duration_seconds": 0.8,
                "segment_peak_energy": 0.2,
            }
            yield RecognitionResult(
                "nel sistema solare ci sono otto pianeti",
                is_final=False,
                **common,
            )
            yield RecognitionResult(
                "nel sistema solare ci sono otto pianeti no parlami",
                is_final=False,
                **common,
            )
            yield RecognitionResult(
                "nel sistema solare ci sono otto pianeti no parlami della luna",
                is_final=True,
                **common,
            )

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=JumpingRecognizer([]),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.8,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) == "no parlami della luna"
    assert api.cancelled is True
    response.cancel()
    assistant.close()


def test_low_confidence_residual_cannot_borrow_tts_word_evidence() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "risposta vecchia assistente"

    class ContaminatedRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            text = "risposta vecchia assistente parole casuali qui"
            common = {
                "frame_energy": 0.2,
                "segment_id": 17,
                "segment_started_at": 10.1,
                "confidence": 0.525,
                "speech_duration_seconds": 0.8,
                "segment_peak_energy": 0.2,
                "word_confidences": (0.95, 0.95, 0.95, 0.1, 0.1, 0.1),
                "word_timings": (
                    (0.0, 0.1),
                    (0.1, 0.2),
                    (0.2, 0.3),
                    (0.3, 0.4),
                    (0.4, 0.5),
                    (0.5, 0.6),
                ),
            }
            yield RecognitionResult(text, is_final=False, **common)
            yield RecognitionResult(text, is_final=True, **common)

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=ContaminatedRecognizer([]),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.8,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) is None
    assert api.cancelled is False
    response.cancel()
    assistant.close()


def test_multiple_tts_fragments_are_all_removed_from_follow_up() -> None:
    assistant = VoiceAssistant(
        settings=config.Settings(project_root=config.PROJECT_ROOT),
        tts=FakeTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=FakeAPI(),
        speech_recognizer=FakeRecognizer([]),
        sound_executor=ImmediateExecutor(),
    )

    residual, removed = assistant._remove_current_tts_echo(
        "prima frase assistente seconda frase assistente nuova domanda sulla luna",
        10.0,
        references=("prima frase assistente", "seconda frase assistente"),
    )

    assert residual == "nuova domanda sulla luna"
    assert removed == (0, 1, 2, 3, 4, 5)
    assistant.close()


def test_tts_reference_quoted_inside_follow_up_is_not_removed() -> None:
    assistant = VoiceAssistant(
        settings=config.Settings(project_root=config.PROJECT_ROOT),
        tts=FakeTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=FakeAPI(),
        speech_recognizer=FakeRecognizer([]),
        sound_executor=ImmediateExecutor(),
    )

    residual, removed = assistant._remove_current_tts_echo(
        "voglio citare prima frase assistente nella domanda",
        10.0,
        references=("prima frase assistente",),
    )

    assert residual == "voglio citare prima frase assistente nella domanda"
    assert removed == ()
    assistant.close()


def test_exact_echo_prefix_wins_over_an_earlier_fuzzy_reference() -> None:
    assistant = VoiceAssistant(
        settings=config.Settings(project_root=config.PROJECT_ROOT),
        tts=FakeTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=FakeAPI(),
        speech_recognizer=FakeRecognizer([]),
        sound_executor=ImmediateExecutor(),
    )

    residual, removed = assistant._remove_current_tts_echo(
        "seconda frase assistente nuova domanda sulla luna",
        10.0,
        references=(
            "prima frase assistente nuova domanda sulla luna",
            "seconda frase assistente",
        ),
    )

    assert residual == "nuova domanda sulla luna"
    assert removed == (0, 1, 2)
    assistant.close()


@pytest.mark.parametrize("during_playback", (False, True))
def test_low_confidence_vosk_hallucination_never_interrupts(
    during_playback: bool,
) -> None:
    class DynamicTTS(FakeTTS):
        is_speaking = during_playback
        active_playback_started_at = 10.0 if during_playback else None
        active_playback_text = "Una risposta non correlata."

    class HallucinationRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            common = {
                "frame_energy": 0.4,
                "segment_id": 12,
                "segment_started_at": 10.1,
                "confidence": 0.2,
                "speech_duration_seconds": 0.6,
                "segment_peak_energy": 0.4,
            }
            yield RecognitionResult(
                "ipotesi vocale casuale",
                is_final=False,
                **common,
            )
            yield RecognitionResult(
                "ipotesi vocale casuale",
                is_final=True,
                **common,
            )

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=DynamicTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=HallucinationRecognizer([]),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.5,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) is None
    assert api.cancelled is False
    response.cancel()
    assistant.close()


def test_response_completion_after_armed_partial_does_not_lose_final_question() -> None:
    response: Future[str] = Future()

    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Una risposta non correlata."

    class CompletionBetweenEventsRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "questa nuova domanda",
                is_final=False,
                frame_energy=0.2,
                segment_id=22,
                segment_started_at=10.2,
                confidence=0.9,
                speech_duration_seconds=0.5,
                segment_peak_energy=0.2,
            )
            response.set_result("old response completed")
            yield RecognitionResult(
                "questa nuova domanda completa",
                is_final=True,
                frame_energy=0.01,
                segment_id=22,
                segment_started_at=10.2,
                confidence=0.9,
                speech_duration_seconds=0.7,
                segment_peak_energy=0.2,
            )

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=CompletionBetweenEventsRecognizer([]),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.8,
    )

    assert assistant._listen_for_barge_in(response) == "questa nuova domanda completa"
    assert api.cancelled is True
    assistant.close()


def test_response_completion_before_strong_final_does_not_consume_question() -> None:
    response: Future[str] = Future()

    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Una risposta non correlata."

    class FinalAfterCompletionRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            response.set_result("old response completed")
            yield RecognitionResult(
                "questa nuova domanda completa",
                is_final=True,
                frame_energy=0.2,
                segment_id=30,
                segment_started_at=10.2,
                confidence=0.9,
                speech_duration_seconds=0.7,
                segment_peak_energy=0.2,
            )

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=FinalAfterCompletionRecognizer([]),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.8,
    )

    assert assistant._listen_for_barge_in(response) == "questa nuova domanda completa"
    assert api.cancelled is True
    assistant.close()


def test_distinct_user_correction_during_playback_remains_interruptible() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Nel sistema solare ci sono otto pianeti."

    class CorrectionRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "no parlami soltanto di marte",
                is_final=True,
                frame_energy=0.2,
            )

    class AcceptingDetector:
        def reset(self) -> None:
            pass

        def process_recognition(self, *_args: object, **_kwargs: object) -> bool:
            return True

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=CorrectionRecognizer([]),
        barge_in_detector=AcceptingDetector(),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.8,
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) == "no parlami soltanto di marte"
    assert api.cancelled is True
    response.cancel()
    assistant.close()


def test_partial_only_barge_in_timeout_never_executes_partial_as_follow_up() -> None:
    class SpeakingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 0.0

    class PartialOnlyRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult("incomplete phrase", is_final=False, frame_energy=0.5)

    class Detecting:
        def reset(self) -> None:
            pass

        def process_recognition(self, *_args: object, **_kwargs: object) -> bool:
            return True

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=SpeakingTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=PartialOnlyRecognizer([]),
        barge_in_detector=Detecting(),
        sound_executor=ImmediateExecutor(),
    )
    response: Future[str] = Future()

    assert assistant._listen_for_barge_in(response) is None
    assert api.cancelled is False
    response.cancel()
    assistant.close()


def test_partial_arms_tts_duck_before_final_model_cancellation() -> None:
    response: Future[str] = Future()
    events: list[str] = []

    class DuckingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Una risposta non correlata."

        def __init__(self) -> None:
            super().__init__()
            self.paused = False
            self.resume_calls = 0

        def duck(self) -> bool:
            events.append("tts_duck")
            self.paused = True
            return True

        def resume(self) -> bool:
            self.resume_calls += 1
            was_paused = self.paused
            self.paused = False
            if was_paused:
                events.append("tts_resume")
            return was_paused

        def interrupt(self) -> bool:
            events.append("tts_interrupt")
            self.paused = False
            return True

    class RecordingAPI(FakeAPI):
        def cancel_current(self) -> None:
            events.append("model_cancel")
            super().cancel_current()

    class OrderedRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            yield RecognitionResult(
                "questa nuova domanda",
                is_final=False,
                frame_energy=0.2,
                segment_id=40,
                segment_started_at=10.2,
            )
            assert events == ["tts_duck"]
            yield RecognitionResult(
                "questa nuova domanda completa",
                is_final=True,
                frame_energy=0.01,
                segment_id=40,
                segment_started_at=10.2,
            )

    api = RecordingAPI()
    tts = DuckingTTS()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=tts,
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=OrderedRecognizer([]),
        sound_executor=ImmediateExecutor(),
        clock=lambda: 10.8,
    )

    assert assistant._listen_for_barge_in(response) == "questa nuova domanda completa"
    assert events == ["tts_duck", "tts_interrupt", "model_cancel"]
    assert tts.resume_calls >= 1
    response.cancel()
    assistant.close()


def test_provisional_timeout_resumes_and_restarts_capture_for_later_barge_in() -> None:
    response: Future[str] = Future()
    now = [10.2]

    class DuckingTTS(FakeTTS):
        is_speaking = True
        active_playback_started_at = 10.0
        active_playback_text = "Una risposta non correlata."

        def __init__(self) -> None:
            super().__init__()
            self.paused = False
            self.duck_calls = 0
            self.resume_calls = 0
            self.interrupt_calls = 0

        def duck(self) -> bool:
            self.duck_calls += 1
            self.paused = True
            return True

        def resume(self) -> bool:
            was_paused = self.paused
            self.paused = False
            if was_paused:
                self.resume_calls += 1
            return was_paused

        def interrupt(self) -> bool:
            self.interrupt_calls += 1
            self.paused = False
            return True

    class RestartingRecognizer(FakeRecognizer):
        def __init__(self) -> None:
            super().__init__([])
            self.sessions = 0

        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            self.sessions += 1
            if self.sessions == 1:
                yield RecognitionResult(
                    "rumore casuale parole",
                    is_final=False,
                    frame_energy=0.2,
                    segment_id=1,
                    segment_started_at=10.2,
                )
                now[0] = 11.8
                assert getattr(stop_event, "is_set")() is True
                return

            now[0] = 12.0
            yield RecognitionResult(
                "questa nuova domanda",
                is_final=False,
                frame_energy=0.2,
                segment_id=1,
                segment_started_at=12.0,
                confidence=0.9,
            )
            now[0] = 12.3
            yield RecognitionResult(
                "questa nuova domanda completa",
                is_final=True,
                frame_energy=0.01,
                segment_id=1,
                segment_started_at=12.0,
                confidence=0.9,
                speech_duration_seconds=0.5,
                segment_peak_energy=0.2,
            )

    tts = DuckingTTS()
    recognizer = RestartingRecognizer()
    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(project_root=config.PROJECT_ROOT, barge_in_enabled=True),
        tts=tts,
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=recognizer,
        sound_executor=ImmediateExecutor(),
        clock=lambda: now[0],
    )

    assert assistant._listen_for_barge_in(response) == "questa nuova domanda completa"
    assert recognizer.sessions == 2
    assert tts.duck_calls == 2
    assert tts.resume_calls == 1
    assert tts.interrupt_calls == 1
    assert api.cancelled is True
    response.cancel()
    assistant.close()


def test_response_completion_wins_over_late_recognizer_finalization() -> None:
    response: Future[str] = Future()

    class ResettableDetector:
        def reset(self) -> None:
            pass

    class CompletionRaceRecognizer(FakeRecognizer):
        def listen_events(self, timeout: float | None, *, stop_event: object):
            assert timeout is None
            response.set_result("done")
            yield RecognitionResult("late flush", is_final=True, frame_energy=0.5)

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            barge_in_enabled=True,
        ),
        tts=FakeTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=CompletionRaceRecognizer([]),
        barge_in_detector=ResettableDetector(),
        sound_executor=ImmediateExecutor(),
    )

    assert assistant._listen_for_barge_in(response) is None
    assert api.cancelled is False
    assistant.close()


def test_tts_interrupt_failure_does_not_skip_model_cancellation() -> None:
    class FailingInterruptTTS(FakeTTS):
        def interrupt(self) -> None:
            raise RuntimeError("audio backend failed")

    api = FakeAPI()
    assistant = VoiceAssistant(
        settings=config.Settings(project_root=config.PROJECT_ROOT),
        tts=FailingInterruptTTS(),
        sound_player=FakeSoundPlayer(),
        api_client=api,
        speech_recognizer=FakeRecognizer([]),
        sound_executor=ImmediateExecutor(),
    )

    assistant._interrupt_current_response()

    assert api.cancelled is True
    assistant.close()


def test_stop_forces_active_follow_up_capture_to_finish() -> None:
    assistant, _tts, api, _sounds, _recognizer = make_assistant([])
    capture_stop = _BargeInCaptureStop(
        clock=lambda: 0.0,
        follow_up_timeout_seconds=30,
    )
    capture_stop.barge_in_detected()
    with assistant._capture_lock:
        assistant._active_capture_stop = capture_stop

    assistant.stop()

    assert capture_stop.is_set()
    assert api.cancelled is True
    capture_stop.capture_finished()
    with assistant._capture_lock:
        assistant._active_capture_stop = None
    assistant.close()


def test_post_barge_deadline_refreshes_on_recognition_activity() -> None:
    now = [0.0]
    capture_stop = _BargeInCaptureStop(
        clock=lambda: now[0],
        follow_up_timeout_seconds=6.5,
    )

    capture_stop.barge_in_detected()
    now[0] = 6.0
    capture_stop.recognition_activity()
    now[0] = 12.0
    assert capture_stop.is_set() is False
    now[0] = 12.6
    assert capture_stop.is_set() is True


def test_provisional_candidate_inactivity_ends_capture_and_can_be_cleared() -> None:
    now = [0.0]
    capture_stop = _BargeInCaptureStop(
        clock=lambda: now[0],
        follow_up_timeout_seconds=30,
    )

    capture_stop.candidate_activity()
    now[0] = 1.4
    assert capture_stop.is_set() is False
    now[0] = 1.6
    assert capture_stop.is_set() is True

    capture_stop.candidate_finished()
    assert capture_stop.is_set() is False


def test_known_single_worker_conversation_executor_is_rejected() -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(ValueError, match="at least two workers"):
            VoiceAssistant(
                settings=config.Settings(
                    project_root=config.PROJECT_ROOT,
                    barge_in_enabled=True,
                ),
                conversation_executor=executor,
            )


def test_settings_validate_language_and_root_derived_paths(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigurationError, match="Unsupported language"):
        config.Settings(language="fr")

    settings = config.Settings(project_root=tmp_path, language="en")
    assert settings.profile.tts_model == tmp_path / "audio/models/en_GB-alba-medium.onnx"
    assert settings.upload_folder == tmp_path / "uploads"
    assert settings.ollama_host == "http://localhost:11434"


def test_injected_api_client_shares_the_profile_specific_tts() -> None:
    settings = config.Settings(project_root=config.PROJECT_ROOT, language="en")
    api = APIClient(client=object())
    assistant = VoiceAssistant(
        settings=settings,
        api_client=api,
        sound_player=FakeSoundPlayer(),
        speech_recognizer=FakeRecognizer([]),
        sound_executor=ImmediateExecutor(),
    )

    assert isinstance(assistant.tts, PiperTTS)
    assert assistant.tts.voice_model == settings.profile.tts_model
    assert api.tts is assistant.tts
