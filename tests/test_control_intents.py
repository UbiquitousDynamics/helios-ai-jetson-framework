from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import config
from api.api_client import APIClient
from api.control_intents import ControlIntent as I, SpeechOutputControl, parse_control
from api.conversation_control import ConversationFloorState as S
from api.streaming import CancellationController
from api.transcripts import ProvisionalRevision
from assistant import VoiceAssistant, RagCommandError
from audio.tts import PiperTTS
from recognizer.speech_recognizer import RecognitionResult
from test_assistant import FakeAPI, FakeRecognizer, FakeSoundPlayer, FakeTTS, ImmediateExecutor
from test_api_client import FakeClient, chunk
from test_tts_interrupt import LongVoice, PauseAwareRecordingBackend


@pytest.mark.parametrize("language,text,intent", [
    ("it", "Emilia, basta!", I.STOP_SPEAKING), ("it", "silenzio", I.STOP_SPEAKING),
    ("en", "Emilia, stop speaking.", I.STOP_SPEAKING), ("en", "stop", I.STOP_SPEAKING),
    ("it", "disattiva audio", I.MUTE), ("en", "mute audio", I.MUTE),
    ("it", "riattiva audio", I.UNMUTE), ("en", "unmute", I.UNMUTE),
    ("it", "sospendi conversazione", I.SUSPEND_SESSION), ("en", "pause session", I.SUSPEND_SESSION),
    ("it", "riprendi sessione", I.RESUME_SESSION), ("en", "resume conversation", I.RESUME_SESSION),
    ("it", "termina conversazione", I.END_SESSION), ("en", "end session", I.END_SESSION),
    ("it", "annulla attività", I.CANCEL_TASK), ("en", "cancel the task", I.CANCEL_TASK),
])
def test_local_control_grammar(language, text, intent):
    assert parse_control(text, language=language, wake_words=("emilia",)) is intent


@pytest.mark.parametrize("text", [
    "", "please explain stop signs", "do not stop", "say mute", "what does end session mean",
    "cancel the task tomorrow", "stop and tell me a story", "unstoppable", "stopwatch",
    "spiega la parola silenzio", "non disattiva audio", "hello world stop",
])
def test_control_false_matches_are_regular_content(text):
    assert parse_control(text, language="en", wake_words=("emilia", "hello")) is None
    assert parse_control(text, language="it", wake_words=("emilia", "hello")) is None


def test_control_parser_rejects_provisional_authority():
    with pytest.raises(TypeError):
        parse_control(ProvisionalRevision("stop", capture_id=1, segment_id=1, revision=1), language="en")
    with pytest.raises(ValueError):
        parse_control("stop", language="unknown")


def assistant_for(results=(), *, language="en", api=None, tts=None):
    return VoiceAssistant(
        settings=config.Settings(language=language, barge_in_enabled=False),
        tts=tts or FakeTTS(), api_client=api or FakeAPI(),
        speech_recognizer=FakeRecognizer([RecognitionResult(text, is_final=True) for text in results]),
        sound_player=FakeSoundPlayer(), sound_executor=ImmediateExecutor(), clock=lambda: 10,
    )


@pytest.mark.parametrize("entry", ["process_command", "process_rag_command", "_process_model_prompt"])
@pytest.mark.parametrize("command", ["stop", "mute", "unmute", "pause session", "resume session", "end session", "cancel task"])
def test_all_command_boundaries_consume_controls_without_model_or_rag(entry, command):
    assistant = assistant_for()
    try:
        getattr(assistant, entry)(command)
        assert assistant.api_client.messages == []
        assert assistant.api_client.think_messages == []
        assert assistant._rag_searcher is None
        assert assistant.last_control_result[0] is parse_control(command, language="en")
        if command == "cancel task":
            assert assistant.last_control_result == (I.CANCEL_TASK, False)
            assert not assistant.api_client.cancelled
            assert not assistant.realtime.speech_output.is_set()
    finally:
        assistant.close()


def test_end_session_requires_new_activation_and_resume_cannot_bypass_it():
    assistant = assistant_for(["Emilia", "end session", "resume session", "a new question", "Emilia a new question"])
    try:
        assert assistant.run_once()
        assert assistant._voice_conversation_active
        assert assistant.run_once()
        assert not assistant._voice_conversation_active
        assert assistant.realtime.snapshot().floor.state is S.IDLE
        assert assistant.run_once()
        assert assistant.last_control_result == (I.RESUME_SESSION, False)
        assert not assistant.run_once()
        assert assistant.api_client.messages == []
        assert assistant.run_once()
        assert assistant.api_client.messages == ["a new question"]
    finally:
        assistant.close()


def test_suspended_session_still_processes_local_resume_and_end():
    assistant = assistant_for(["pause session", "Emilia a question", "resume session", "Emilia a question", "pause session", "end session"])
    try:
        assert assistant.run_once()
        assert assistant.realtime.snapshot().floor.state is S.SUSPENDED
        assert not assistant.run_once()
        assert assistant.api_client.messages == []
        assert assistant.run_once()
        assert assistant.realtime.snapshot().floor.state is S.ARMED
        assert assistant.run_once()
        assert assistant.api_client.messages == ["a question"]
        assert assistant.run_once()
        assert assistant.run_once()
        assert assistant.realtime.snapshot().floor.state is S.IDLE
    finally:
        assistant.close()


def test_mute_persists_across_response_boundaries_and_unmute_restores_output():
    tts = FakeTTS()
    api = APIClient(client=FakeClient([chunk("A synthetic answer.", done=True)]), tts=tts, retry_wait=0)
    assistant = assistant_for(api=api, tts=tts)
    try:
        assistant.process_command("mute")
        assert assistant.process_command("Emilia first question") == "A synthetic answer."
        assert assistant.process_command("Emilia second question") == "A synthetic answer."
        assert tts.spoken == []
        assistant.process_command("unmute")
        assert assistant.process_command("Emilia third question") == "A synthetic answer."
        assert tts.spoken == ["A synthetic answer."]
    finally:
        assistant.close()


def test_stop_speaking_during_generation_preserves_generation_and_excludes_control_from_history():
    first_spoken, resume_provider = threading.Event(), threading.Event()

    class TTS(FakeTTS):
        def speak(self, text):
            super().speak(text)
            first_spoken.set()

    class Provider:
        def __init__(self):
            self.messages = []

        def chat(self, **kwargs):
            self.messages.append(kwargs["messages"])
            yield chunk("First answer. ")
            assert resume_provider.wait(timeout=2)
            yield chunk("The rest of the answer.", done=True)

    tts, provider, token = TTS(), Provider(), CancellationController()
    api = APIClient(client=provider, tts=tts, retry_wait=0)
    assistant = assistant_for(api=api, tts=tts)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(assistant.process_command, "Emilia a question", cancellation=token)
            try:
                assert first_spoken.wait(timeout=2)
                assistant.process_command("stop")
                assert not token.cancelled and not future.done()
            finally:
                resume_provider.set()
            assert future.result(timeout=2) == "First answer. The rest of the answer."
        assert tts.spoken == ["First answer."]
        assert len(provider.messages) == 1
        assert provider.messages[0][-1]["content"] == "a question"
        assert api.conversation.snapshot().turn_count == 1
        assert api.conversation.snapshot().history_message_count == 2
    finally:
        resume_provider.set()
        assistant.close()


def test_output_control_blocks_piper_even_before_playback_registers_and_covers_cached_audio():
    control, backend = SpeechOutputControl(), PauseAwareRecordingBackend()
    tts = PiperTTS("unused.onnx", voice=LongVoice(), audio_backend=backend)
    tts.set_output_control(control)
    try:
        tts.preload_phrases(("synthetic phrase",))
        control.set_muted(True)
        assert tts.speak_preloaded("synthetic phrase")
        tts.speak("synthetic phrase")
        assert backend.frames_written == []
        control.begin_response()
        assert control.is_set()
        control.set_muted(False)
        tts.speak("synthetic phrase")
        assert backend.frames_written == [16000]
        control.stop_speaking()
        tts.speak("synthetic phrase")
        assert backend.frames_written == [16000]
        control.begin_response()
        tts.speak("synthetic phrase")
        assert backend.frames_written == [16000, 16000]
    finally:
        tts.close()


def test_actual_rag_cancellation_still_prevents_late_speech_and_model_dispatch():
    retrieving, release = threading.Event(), threading.Event()
    token = CancellationController()

    class Search:
        def run(self, **kwargs):
            retrieving.set()
            assert release.wait(timeout=2)
            return "synthetic result after cancellation"

    assistant = assistant_for()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(assistant._process_rag_response, "synthetic query", Search(), cancellation=token)
            try:
                assert retrieving.wait(timeout=2)
                token.cancel()
            finally:
                release.set()
            with pytest.raises(RagCommandError):
                future.result(timeout=2)
        assert assistant.tts.spoken == [] and assistant.api_client.messages == []
        assert assistant.realtime.snapshot().response_id is None
    finally:
        release.set()
        assistant.close()
