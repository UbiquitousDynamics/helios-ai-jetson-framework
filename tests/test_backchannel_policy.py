"""Cue admission, bounded supersession and provider-neutral spoken style."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import config
from api.api_client import APIClient
from api.providers.contracts import ContentOrigin, ErrorCategory, ProviderError
from api.routing import Connectivity
from api.streaming import CancellationController
from api.transcripts import TranscriptPromoter, TranscriptBoundaryError
from assistant import AssistantRuntimeError, VoiceAssistant
from audio.backchannel import BackchannelMode, BackchannelSession, suppress_backchannel_for
from recognizer.speech_recognizer import RecognitionResult
from test_api_client import FakeClient, chunk
from test_assistant import FakeAPI, FakeRecognizer, FakeSoundPlayer, FakeTTS, ImmediateExecutor
from test_conversational_pacing import InterruptiblePreloadedTTS
from test_hybrid_api_client import FakeOllamaClient, FakeRemoteProvider, hybrid_settings


def runtime(language="en", *, api=None, tts=None, results=()):
    return VoiceAssistant(
        settings=config.Settings(language=language, backchannel_delay_seconds=0.01),
        api_client=api or FakeAPI(),
        tts=tts or FakeTTS(),
        speech_recognizer=FakeRecognizer(list(results)),
        sound_player=FakeSoundPlayer(),
        sound_executor=ImmediateExecutor(),
    )


@pytest.mark.parametrize(
    ("language", "prompt", "suppressed"),
    [
        ("en", "Take dictation: synthetic words", True),
        ("en", "I will dictate", True),
        ("en", "Transcribe this exactly", True),
        ("en", "I confirm the deletion", True),
        ("en", "Yes, proceed", True),
        ("en", "Go ahead", True),
        ("en", "What is dictation?", False),
        ("en", "Confirmatory research methods", False),
        ("it", "Ti detto un messaggio", True),
        ("it", "Sto dettando", True),
        ("it", "Trascrivi queste parole", True),
        ("it", "Confermo la cancellazione", True),
        ("it", "Sì, procedi", True),
        ("it", "Fallo", True),
        ("it", "Come funziona la dettatura?", False),
        ("it", "Confermare significa cosa?", False),
    ],
)
def test_bilingual_suppression_is_wired_before_model_dispatch(language, prompt, suppressed):
    class API(FakeAPI):
        callback = None

        def talk(self, message, context=None, before_first_speech=None):
            self.callback = before_first_speech
            if before_first_speech:
                before_first_speech()
            return super().talk(message, context)

    class TTS(FakeTTS):
        def speak_preloaded(self, phrase, *, cancellation):
            raise AssertionError("fast response must suppress the cue")

    api = API()
    assistant = runtime(language, api=api, tts=TTS())
    try:
        assert suppress_backchannel_for(prompt, language=language) is suppressed
        assert assistant.process_command("Emilia " + prompt) == "model response"
        assert (api.callback is None) is suppressed
        assert api.messages == [prompt]
    finally:
        assistant.close()


@pytest.mark.parametrize("language", ["it", "en"])
@pytest.mark.parametrize(
    "mode", [BackchannelMode.DICTATION, BackchannelMode.SENSITIVE_CONFIRMATION]
)
def test_explicit_flow_mode_suppresses_cues_without_changing_request_or_floor(language, mode):
    class API(FakeAPI):
        def talk(self, message, context=None, before_first_speech=None):
            assert before_first_speech is None
            return super().talk(message, context)

    assistant = runtime(language, api=API())
    try:
        assistant.set_backchannel_mode(mode)
        assert assistant.process_command("Emilia synthetic request") == "model response"
        assert assistant.api_client.messages == ["synthetic request"]
        assistant.set_backchannel_mode(BackchannelMode.NORMAL)
        response = assistant.realtime.begin_response()
        assert assistant._backchannel_allowed(response)
        assistant._finish_response(response)
    finally:
        assistant.close()


@pytest.mark.parametrize("language", ["it", "en"])
def test_short_user_pause_never_schedules_a_backchannel_or_model_turn(language):
    assistant = runtime(
        language, results=[RecognitionResult("unfinished synthetic words", is_final=False), None]
    )
    try:
        assert not assistant.run_once()
        assert not assistant.run_once()
        assert assistant._last_backchannel_session is None
        assert assistant.api_client.messages == [] and assistant.tts.spoken == []
    finally:
        assistant.close()


@pytest.mark.parametrize(
    "event", ["partial", "candidate", "dictation", "confirmation", "stop", "interrupt"]
)
def test_active_cue_stops_when_user_or_local_control_requires_silence(event):
    tts = InterruptiblePreloadedTTS()
    assistant = runtime(tts=tts)
    response = assistant.realtime.begin_response()
    try:
        cue = assistant._start_backchannel(response_id=response)
        tts.started.get(timeout=2)
        if event == "partial":
            assistant._observe_transcript(RecognitionResult("unfinished", is_final=False))
        elif event == "candidate":
            assistant.realtime.candidate()
        elif event in {"dictation", "confirmation"}:
            assistant.set_backchannel_mode(
                BackchannelMode.DICTATION
                if event == "dictation"
                else BackchannelMode.SENSITIVE_CONFIRMATION
            )
        elif event == "stop":
            assistant.process_command("stop")
        else:
            assistant._interrupt_current_response()
        cue.future.result(timeout=2)
        assert not cue.is_playing
        assert [kind for kind, _phrase in tts.events] == [
            "backchannel_started",
            "interrupt",
            "backchannel_stopped",
        ]
        assistant.set_backchannel_mode(BackchannelMode.NORMAL)
        assert len(tts.events) == 3  # Suppression cannot resurrect the same cue.
    finally:
        assistant._finish_response(response)
        assistant.close()


def test_request_cancellation_stops_cue_even_while_model_has_not_unwound():
    tts = InterruptiblePreloadedTTS()
    cancellation = CancellationController()
    with ThreadPoolExecutor(max_workers=1) as pool:
        cue = BackchannelSession(
            tts=tts,
            phrase="One moment.",
            delay_seconds=0.01,
            executor=pool,
            cancellation=cancellation,
        )
        tts.started.get(timeout=2)
        cancellation.cancel()
        cue.future.result(timeout=2)
    assert not cue.is_playing and tts.events[-1][0] == "backchannel_stopped"


@pytest.mark.parametrize("reason", ["cancelled", "disallowed", "policy_failure"])
def test_queued_cue_checks_admission_before_playback(reason):
    class TTS:
        def speak_preloaded(self, phrase, *, cancellation):
            raise AssertionError("suppressed cue reached playback")

    cancellation = CancellationController()
    if reason == "cancelled":
        cancellation.cancel()

    def allowed():
        if reason == "policy_failure":
            raise RuntimeError("synthetic policy failure")
        return reason != "disallowed"

    with ThreadPoolExecutor(max_workers=1) as pool:
        cue = BackchannelSession(
            tts=TTS(),
            phrase="One moment.",
            delay_seconds=0.01,
            executor=pool,
            allowed=allowed,
            cancellation=cancellation,
        )
        cue.future.result(timeout=2)
    assert not cue.triggered and not cue.played


def test_uncooperative_cue_blocks_real_speech_with_a_bounded_failure(monkeypatch):
    import audio.backchannel as backchannel

    monkeypatch.setattr(backchannel, "BACKCHANNEL_STOP_TIMEOUT_SECONDS", 0.02)
    started, release = threading.Event(), threading.Event()

    class TTS(FakeTTS):
        def speak_preloaded(self, phrase, *, cancellation):
            started.set()
            assert release.wait(timeout=2)
            return True

    tts = TTS()
    api = APIClient(
        client=FakeClient([chunk("Synthetic answer.", done=True)]), tts=tts, retry_wait=0
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            cue = BackchannelSession(
                tts=tts, phrase="One moment.", delay_seconds=0.01, executor=pool
            )
            try:
                assert started.wait(timeout=2)
                with pytest.raises(TimeoutError, match="backchannel playback did not stop"):
                    api.talk("synthetic request", before_first_speech=cue.before_first_speech)
                assert tts.spoken == []
                next_turn = api.conversation.begin_turn("next synthetic request")
                assert [
                    (message.role.value, message.content)
                    for message in api.conversation.history_before(next_turn)
                ] == [("user", "synthetic request")]
                api.conversation.fail_turn(next_turn, interrupted=True)
            finally:
                release.set()
                assert cue.supersede(timeout=2)
    finally:
        api.close()


@pytest.mark.parametrize("value", [0, -1, True, "1", float("nan"), float("inf")])
def test_invalid_delays_never_submit_work(value):
    with pytest.raises(ValueError):
        BackchannelSession(
            tts=object(), phrase="One moment.", delay_seconds=value, executor=object()
        )


def test_assistant_stops_on_cue_timeout_and_owned_worker_cleans_up(monkeypatch):
    import assistant as assistant_module
    import audio.backchannel as backchannel

    monkeypatch.setattr(assistant_module, "BACKCHANNEL_STOP_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(backchannel, "BACKCHANNEL_STOP_TIMEOUT_SECONDS", 0.02)
    entered, release = threading.Event(), threading.Event()

    class TTS(FakeTTS):
        def speak_preloaded(self, phrase, *, cancellation):
            entered.set()
            assert release.wait(timeout=2)
            return True

    class Source:
        def chat(self, **kwargs):
            assert entered.wait(timeout=2)
            yield chunk("Synthetic answer.", done=True)

    tts = TTS()
    api = APIClient(client=Source(), tts=tts, retry_wait=0)
    assistant = runtime(api=api, tts=tts)
    try:
        with pytest.raises(AssistantRuntimeError, match="backchannel playback did not stop"):
            assistant.process_command("Emilia synthetic request")
        assert assistant._stop_requested
        assert assistant.realtime.snapshot().response_id is None
        assert tts.spoken == []
    finally:
        release.set()
        assistant.close()
    assert assistant._last_backchannel_session.future.done()


@pytest.mark.parametrize("timeout", [-1, True, "1", float("nan"), float("inf")])
def test_invalid_supersession_timeouts_are_rejected(timeout):
    with ThreadPoolExecutor(max_workers=1) as pool:
        cue = BackchannelSession(tts=object(), phrase="One moment.", delay_seconds=1, executor=pool)
        try:
            with pytest.raises(ValueError):
                cue.supersede(timeout=timeout)
        finally:
            assert cue.supersede(timeout=2)


def test_provisional_text_cannot_reach_lexical_policy():
    provisional = TranscriptPromoter().observe("unfinalized", is_final=False)
    with pytest.raises(TranscriptBoundaryError):
        suppress_backchannel_for(provisional, language="en")


@pytest.mark.parametrize("language", ["it", "en"])
def test_spoken_style_is_static_per_request_and_never_truncates_or_enters_history(language):
    answer = " ".join(["Synthetic detail."] * 30)
    source = FakeClient([chunk(answer, done=True)])
    tts = FakeTTS()
    api = APIClient(
        client=source, tts=tts, retry_wait=0, language=language, spoken_response_style=True
    )
    try:
        instruction = config.spoken_response_instruction(language)
        assert api.talk("first request") == answer
        assert source.calls[0]["messages"] == [
            {"role": "system", "content": instruction},
            {"role": "user", "content": "first request"},
        ]
        assert " ".join(tts.spoken) == answer
        api.think("silent request", tts=False)
        assert all(message["role"] != "system" for message in source.calls[-1]["messages"])
        api.think("spoken request", tts=True)
        messages = source.calls[-1]["messages"]
        assert sum(message["content"] == instruction for message in messages) == 1
        assert len(messages) == 6  # Style plus two prior pairs plus current request.
        assert api.conversation.snapshot().history_message_count == 6
    finally:
        api.close()


@pytest.mark.parametrize("language", ["it", "en"])
def test_spoken_style_survives_fallback_with_static_provenance(tmp_path, language):
    error = ProviderError(
        ErrorCategory.PROVIDER_UNAVAILABLE,
        "synthetic unavailable",
        provider="remote",
        model="remote-model",
        transmitted=False,
    )
    remote = FakeRemoteProvider([error])
    local = FakeOllamaClient()
    api = APIClient(
        client=local,
        tts=FakeTTS(),
        llm_settings=hybrid_settings(tmp_path),
        providers={"remote": remote},
        connectivity=Connectivity.ONLINE,
        language=language,
        spoken_response_style=True,
        retry_wait=0,
    )
    try:
        assert api.talk("synthetic request") == "Local."
        instruction = config.spoken_response_instruction(language)
        style = [message for message in remote.calls[0].messages if message.content == instruction]
        assert len(style) == 1 and style[0].origin is ContentOrigin.STATIC_INSTRUCTION
        assert [m["content"] for m in local.calls[0]["messages"]].count(instruction) == 1
        assert api.conversation.snapshot().history_message_count == 2
    finally:
        api.close()


def test_default_assistant_enables_spoken_style_and_modes_are_typed():
    assistant = VoiceAssistant(
        settings=config.Settings(language="en"),
        tts=FakeTTS(),
        speech_recognizer=FakeRecognizer([]),
        sound_player=FakeSoundPlayer(),
    )
    try:
        assert assistant.api_client._spoken_response_style is True
        with pytest.raises(TypeError):
            assistant.set_backchannel_mode("dictation")
    finally:
        assistant.close()
