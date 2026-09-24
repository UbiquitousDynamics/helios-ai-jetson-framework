"""Activation admission and coordinated canonical-history reset."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import config
from api.api_client import APIClient, APIClientError
from api.conversation import ConversationSession, ConversationSessionError
from api.conversation_control import ConversationFloorState as S
from api.providers.contracts import ErrorCategory, ProviderError
from assistant import AssistantRuntimeError, VoiceAssistant
from recognizer.speech_recognizer import RecognitionResult
from test_api_client import FakeClient, chunk
from test_assistant import FakeAPI, FakeRecognizer, FakeSoundPlayer, FakeTTS, ImmediateExecutor
from test_hybrid_api_client import FakeRemoteProvider, make_client


def runtime(results, now, *, api=None, tts=None, **settings):
    return VoiceAssistant(
        settings=config.Settings(language="en", activation_timeout_seconds=5, **settings),
        api_client=api or FakeAPI(),
        tts=tts or FakeTTS(),
        speech_recognizer=FakeRecognizer(
            [
                RecognitionResult(item, is_final=True) if isinstance(item, str) else item
                for item in results
            ]
        ),
        sound_player=FakeSoundPlayer(),
        sound_executor=ImmediateExecutor(),
        clock=lambda: now[0],
    )


@pytest.mark.parametrize("barge_in", [False, True])
def test_one_wake_ten_followups_survive_stt_timeouts_and_keep_canonical_history(barge_in):
    now = [0.0]
    source = FakeClient([chunk("Synthetic answer.", done=True)])
    session = ConversationSession(clock=lambda: now[0])
    tts = FakeTTS()
    api = APIClient(client=source, tts=tts, conversation_session=session, retry_wait=0)
    results = ["Emilia"]
    for number in range(10):
        results.extend([None, f"question {number}"])
    assistant = runtime(
        results, now, api=api, tts=tts, listen_timeout=0.01, barge_in_enabled=barge_in
    )
    original = session.session_id
    try:
        assert assistant.run_once()
        assert source.calls == []  # Wake alone does not create a model turn.
        for number in range(10):
            now[0] += 2
            assert not assistant.run_once()
            now[0] += 2
            assert assistant.run_once()
            assert source.calls[-1]["messages"][-1]["content"] == f"question {number}"
        assert len(source.calls) == session.snapshot().turn_count == 10
        assert session.session_id == original
        assert len(source.calls[-1]["messages"]) == 19
    finally:
        assistant.close()


def test_silence_partials_empty_finals_and_mute_do_not_extend_activation():
    now = [0.0]
    assistant = runtime(
        ["Emilia", None, RecognitionResult("noise words", is_final=False), "", "mute", None], now
    )
    try:
        assert assistant.run_once()
        for timestamp in (1, 2, 3):
            now[0] = timestamp
            assert not assistant.run_once()
            assert assistant._voice_conversation_last_activity_at == 0
        now[0] = 4
        assert assistant.run_once()
        assert assistant._voice_conversation_last_activity_at == 0
        now[0] = 5
        assert not assistant.run_once()
        assert not assistant._voice_conversation_active
        assert assistant.realtime.snapshot().floor.state is S.IDLE
        assert assistant.api_client.messages == []
    finally:
        assistant.close()


def test_public_command_entry_point_uses_the_same_activation_window():
    now = [0.0]
    assistant = runtime([], now, barge_in_enabled=False)
    try:
        assert assistant.process_command("not activated") is None
        assert assistant.process_command("Emilia") is None
        assert assistant.api_client.messages == []
        for number in range(10):
            now[0] += 4
            assert assistant.process_command(f"question {number}") == "model response"
        assert len(assistant.api_client.messages) == 10
        now[0] += 5
        assert assistant.process_command("expired question") is None
        assert len(assistant.api_client.messages) == 10
    finally:
        assistant.close()


@pytest.mark.parametrize("command", ["", "   ", "\t\n"])
def test_blank_public_commands_do_not_refresh_activation_or_acknowledge(command):
    now = [0.0]
    assistant = runtime([], now, barge_in_enabled=False)
    try:
        assistant.process_command("Emilia")
        acknowledgements = list(assistant.tts.spoken)
        now[0] = 4
        assert assistant.process_command(command) is None
        assert assistant._voice_conversation_last_activity_at == 0
        assert assistant.tts.spoken == acknowledgements
        assert assistant.api_client.messages == []
        now[0] = 5
        assert assistant.process_command("expired question") is None
        assert not assistant._voice_conversation_active
    finally:
        assistant.close()


def test_canonical_retention_timeout_remains_independent_of_voice_activation():
    now = [0.0]
    session = ConversationSession(idle_timeout_seconds=1, clock=lambda: now[0])
    source = FakeClient([chunk("Synthetic answer.", done=True)])
    api = APIClient(client=source, tts=FakeTTS(), conversation_session=session, retry_wait=0)
    assistant = runtime(["Emilia first question", "followup"], now, api=api, barge_in_enabled=False)
    try:
        assert assistant.run_once()
        original = session.session_id
        now[0] = 1
        assert assistant.run_once()
        assert assistant._voice_conversation_active
        assert session.session_id != original
        assert source.calls[-1]["messages"] == [{"role": "user", "content": "followup"}]
    finally:
        assistant.close()


def test_expiry_during_capture_rejects_a_final_exactly_at_the_deadline():
    now = [0.0]
    assistant = runtime(["Emilia"], now)
    try:
        assert assistant.run_once()

        class FinishingAtDeadline(FakeRecognizer):
            def listen_once(self, timeout):
                now[0] = 5
                return RecognitionResult("late question", is_final=True)

        assistant.speech_recognizer = FinishingAtDeadline([])
        assert not assistant.run_once()
        assert assistant.api_client.messages == []
        assert assistant._session_requires_activation
    finally:
        assistant.close()


def test_final_just_before_deadline_and_successful_response_refresh_activation():
    now = [0.0]

    class SlowAPI(FakeAPI):
        def talk(self, message, context=None):
            now[0] += 20
            assert assistant._voice_conversation_is_active()
            return super().talk(message, context)

    api = SlowAPI()
    assistant = runtime(
        ["Emilia", "first question", "second question"], now, api=api, barge_in_enabled=False
    )
    try:
        assert assistant.run_once()
        now[0] = 4.999
        assert assistant.run_once()
        assert assistant._voice_conversation_last_activity_at == 24.999
        now[0] += 4.999
        assert assistant.run_once()
        assert api.messages == ["first question", "second question"]
    finally:
        assistant.close()


@pytest.mark.parametrize("ending", ["expiry", "voice_end", "software_reset"])
def test_termination_clears_history_provider_bindings_and_requires_activation(ending):
    now = [0.0]
    source = FakeClient([chunk("Synthetic answer.", done=True)])
    session = ConversationSession(clock=lambda: now[0])
    tts = FakeTTS()
    api = APIClient(client=source, tts=tts, conversation_session=session, retry_wait=0)
    forgotten = []
    api._forget_provider_conversations = lambda identity, **kwargs: forgotten.append(identity)
    assistant = runtime(
        ["Emilia first question", "orphan followup", "Emilia new question"], now, api=api, tts=tts
    )
    try:
        assert assistant.run_once()
        old_id = session.session_id
        session.bind_provider_thread("synthetic", "old-thread", session_id=old_id)
        if ending == "expiry":
            now[0] = 5
        elif ending == "voice_end":
            assistant.process_command("end session")
        else:
            assert assistant.reset_conversation()
        assert not assistant.run_once()
        assert session.session_id != old_id
        assert session.snapshot().history_message_count == 0
        assert session.snapshot().provider_threads == ()
        assert forgotten == [old_id]
        assert not session.bind_provider_thread("synthetic", "late-thread", session_id=old_id)
        assert assistant.run_once()
        assert source.calls[-1]["messages"] == [{"role": "user", "content": "new question"}]
    finally:
        assistant.close()


def test_suspend_resume_preserves_context_but_ended_session_cannot_resume():
    now = [0.0]
    source = FakeClient([chunk("Synthetic answer.", done=True)])
    api = APIClient(client=source, tts=FakeTTS(), retry_wait=0)
    assistant = runtime(
        [
            "Emilia first question",
            "pause session",
            "resume session",
            "followup",
            "end session",
            "resume session",
            "ignored",
        ],
        now,
        api=api,
        barge_in_enabled=False,
    )
    try:
        assert assistant.run_once()
        old_id = api.conversation.session_id
        assert assistant.run_once()
        now[0] = 20
        assert assistant.run_once()
        assert assistant.run_once()
        assert api.conversation.session_id == old_id
        assert len(source.calls[-1]["messages"]) == 3
        assert assistant.run_once()
        assert assistant.run_once()
        assert not assistant.run_once()
        assert api.conversation.session_id != old_id
        assert len(source.calls) == 2
    finally:
        assistant.close()


def test_provider_fallback_preserves_activation_and_ten_turn_history(tmp_path):
    unavailable = ProviderError(
        ErrorCategory.PROVIDER_UNAVAILABLE,
        "synthetic unavailable",
        provider="remote",
        model="remote-model",
        transmitted=False,
    )
    remote = FakeRemoteProvider([unavailable] * 10)
    api, local, tts = make_client(tmp_path, remote)
    now = [0.0]
    assistant = runtime(
        ["Emilia", *[f"question {n}" for n in range(10)]],
        now,
        api=api,
        tts=tts,
        barge_in_enabled=False,
    )
    try:
        assert assistant.run_once()
        for _ in range(10):
            now[0] += 4
            assert assistant.run_once()
        assert len(local.calls) == 10
        assert local.calls[-1]["messages"][0]["role"] == "system"
        assert len(local.calls[-1]["messages"][1:]) == 19
        assert api.conversation.snapshot().turn_count == 10
        assert 1 <= len(remote.calls) <= 10  # Health may open the failed route.
    finally:
        assistant.close()


@pytest.mark.parametrize("fails", [False, True])
def test_reset_is_nonblocking_during_generation_and_runs_once_after_unwind(fails):
    entered, release = threading.Event(), threading.Event()

    class Source:
        def chat(self, **kwargs):
            entered.set()
            assert release.wait(timeout=2)
            if fails:
                raise RuntimeError("synthetic provider failure")
            yield chunk("Synthetic answer.", done=True)

    api = APIClient(client=Source(), tts=FakeTTS(), retry_wait=0)
    assistant = runtime([], [0.0], api=api, barge_in_enabled=False)
    forgotten = []
    api._forget_provider_conversations = lambda identity, **kwargs: forgotten.append(identity)
    try:
        original = api.conversation.session_id
        with ThreadPoolExecutor(max_workers=2) as pool:
            request = pool.submit(assistant.process_command, "Emilia synthetic question")
            try:
                assert entered.wait(timeout=2)
                assert api.try_reset_conversation() is None
                assert pool.submit(assistant.reset_conversation).result(timeout=0.5) is False
                assert pool.submit(assistant.reset_conversation).result(timeout=0.5) is False
                assert api.conversation.session_id == original and forgotten == []
                assert assistant._pending_history_reset == "voice_session_ended"
            finally:
                release.set()
            if fails:
                with pytest.raises(APIClientError):
                    request.result(timeout=2)
            else:
                assert request.result(timeout=2) == "Synthetic answer."
        assert api.conversation.session_id != original
        assert api.conversation.snapshot().history_message_count == 0
        assert forgotten == [original]
        assert assistant._pending_history_reset is None
    finally:
        release.set()
        assistant.close()


def test_failed_reset_fails_closed_then_recovers_without_sending_old_history():
    api = APIClient(
        client=FakeClient([chunk("Synthetic answer.", done=True)]), tts=FakeTTS(), retry_wait=0
    )
    assistant = runtime([], [0.0], api=api, barge_in_enabled=False)
    real_reset = api.try_reset_conversation
    try:
        assistant.process_command("Emilia first question")
        old_id = api.conversation.session_id

        def fail(**kwargs):
            raise RuntimeError("synthetic reset failure")

        api.try_reset_conversation = fail
        assert not assistant.reset_conversation()
        with pytest.raises(AssistantRuntimeError, match="reset is pending"):
            assistant.process_command("Emilia new question")
        assert api.conversation.session_id == old_id
        assert api.conversation.snapshot().turn_count == 1
        api.try_reset_conversation = real_reset
        assert assistant.process_command("Emilia new question") == "Synthetic answer."
        assert api.conversation.session_id != old_id
        assert api.conversation.snapshot().turn_count == 1
    finally:
        assistant.close()


def test_api_reset_refusal_does_not_forget_an_active_turn():
    api = APIClient(client=FakeClient(), tts=FakeTTS(), retry_wait=0)
    forgotten = []
    api._forget_provider_conversations = lambda *args, **kwargs: forgotten.append(args)
    try:
        turn = api.conversation.begin_turn("active synthetic request")
        original = api.conversation.session_id
        assert api.try_reset_conversation() is None
        with pytest.raises(ConversationSessionError):
            api.reset_conversation()
        assert forgotten == [] and api.conversation.session_id == original
        api.conversation.fail_turn(turn, interrupted=True)
        assert api.try_reset_conversation() != original
        assert forgotten == [(original,)]
    finally:
        api.close()


@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf"), "30", None])
def test_activation_timeout_rejects_invalid_values(value):
    with pytest.raises(config.ConfigurationError, match="activation_timeout"):
        config.Settings(activation_timeout_seconds=value)


def test_activation_timeout_environment_is_independent_of_other_timeouts():
    settings = config.Settings.from_env(environ={"HELIOS_ACTIVATION_TIMEOUT_SECONDS": "42.5"})
    assert settings.activation_timeout_seconds == 42.5
    assert settings.listen_timeout == 6.5
    assert settings.llm.context_idle_timeout_seconds == 900
