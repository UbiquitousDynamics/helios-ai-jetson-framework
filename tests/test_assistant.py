from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path

import pytest

import config
from api.api_client import APIClient
from assistant import AssistantState, VoiceAssistant
from audio.tts import PiperTTS
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
