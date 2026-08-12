from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

import config
from api.api_client import APIClient
from api.metrics import SafeMetricsRecorder
from assistant import AssistantState, VoiceAssistant, _BargeInCaptureStop
from audio.tts import PiperTTS
from recognizer.barge_in_detector import BargeInDetector
from recognizer.speech_recognizer import RecognitionResult


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


def test_partial_recognition_is_not_executed() -> None:
    assistant, _tts, api, _sounds, _recognizer = make_assistant(
        [RecognitionResult("emilia dimmi qualcosa", is_final=False)]
    )

    assert assistant.run_once() is False
    assert api.messages == []


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


def test_stop_cancels_the_active_model_stream() -> None:
    assistant, _tts, api, _sounds, _recognizer = make_assistant([])

    assistant.stop()

    assert not assistant._running
    assert api.cancelled


def test_run_prepares_remote_while_startup_greeting_is_spoken() -> None:
    assistant, tts, api, _sounds, recognizer = make_assistant([])

    assistant.run(max_iterations=0)

    assert api.prepare_calls == 1
    assert recognizer.prepare_calls == 1
    assert tts.spoken == [
        assistant.profile.welcome_message.format(
            wake_word=assistant.profile.wake_word,
        )
    ]


def test_run_finishes_backchannel_preload_before_first_listen() -> None:
    events: list[str] = []

    class PreloadingTTS(FakeTTS):
        def speak(self, text: str) -> None:
            del text
            events.append("welcome")

        def preload_phrases(self, phrases: tuple[str, ...]) -> None:
            assert phrases == ("Certo.", "Un momento.", "Vediamo.")
            events.append("preload")

    class OrderingRecognizer(FakeRecognizer):
        def listen_once(self, timeout: float) -> RecognitionResult | None:
            events.append("listen")
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

    assert events == ["welcome", "preload", "listen"]


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
            yield RecognitionResult("correzione", is_final=True, frame_energy=0.5)

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

    assert assistant._listen_for_barge_in(response) == "correzione"
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
            "Le stelle brillavano, un eco di possibilità infinite, "
            "mentre il mio algoritmo."
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
        active_playback_text = (
            "Marte, un deserto di rosso, con tracce di vita passata."
        )

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
                "no cambia",
                is_final=False,
                frame_energy=0.2,
                segment_id=5,
                segment_started_at=9.5,
            )
            self.tts.is_speaking = True
            self.tts.active_playback_started_at = 10.0
            yield RecognitionResult(
                "no cambia argomento",
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

    assert assistant._listen_for_barge_in(response) == "no cambia argomento"
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
                "no cambia",
                is_final=False,
                frame_energy=0.2,
                segment_id=7,
                segment_started_at=10.2,
            )
            self.tts.active_playback_started_at = 11.0
            self.tts.active_playback_text = "Secondo frammento ancora non correlato."
            yield RecognitionResult(
                "no cambia argomento",
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

    assert assistant._listen_for_barge_in(response) == "no cambia argomento"
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
