from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import config
from api.api_client import APIClient
from api.conversation import safe_conversation_identifier
from api.providers.codex_app_server import CodexAppServerAdapter
from assistant import VoiceAssistant, VoiceConversationState
from recognizer.barge_in_detector import BargeInDetector
from recognizer.speech_recognizer import RecognitionResult


class BlockingTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.interrupt_calls = 0
        self._speaking = threading.Event()
        self._release_first_playback = threading.Event()

    @property
    def is_speaking(self) -> bool:
        return self._speaking.is_set()

    def wait_until_speaking(self, timeout: float = 1.0) -> bool:
        return self._speaking.wait(timeout)

    def speak(self, text: str) -> None:
        self.spoken.append(text)
        self._speaking.set()
        try:
            if len(self.spoken) == 1:
                assert self._release_first_playback.wait(timeout=2)
        finally:
            self._speaking.clear()

    def interrupt(self) -> bool:
        self.interrupt_calls += 1
        was_speaking = self._speaking.is_set()
        self._release_first_playback.set()
        return was_speaking

    def close(self) -> None:
        pass


class CancellableAPI:
    def __init__(self, tts: BlockingTTS) -> None:
        self.tts = tts
        self.messages: list[str] = []
        self.cancel_calls = 0
        self._lock = threading.Lock()
        self._active: threading.Event | None = None
        self.response_finished = threading.Event()

    def talk(self, message: str, context: str | None = None) -> str:
        assert context is None
        cancellation = threading.Event()
        with self._lock:
            self._active = cancellation
        self.messages.append(message)
        try:
            self.tts.speak(f"response for {message}")
            if cancellation.is_set():
                raise RuntimeError("request cancelled")
            self.tts.speak("queued fragment")
            return f"complete response for {message}"
        finally:
            with self._lock:
                if self._active is cancellation:
                    self._active = None
            self.response_finished.set()

    def cancel_current(self) -> None:
        self.cancel_calls += 1
        with self._lock:
            active = self._active
        if active is not None:
            active.set()

    def close(self) -> None:
        pass


class BargeInRecognizer:
    def __init__(self, api: CancellableAPI) -> None:
        self.api = api
        self._barge_events_emitted = False

    def listen_once(self, timeout: float) -> RecognitionResult:
        assert timeout > 0
        return RecognitionResult("Emilia, prima domanda", is_final=True)

    def listen_events(
        self,
        timeout: float | None,
        *,
        stop_event: object,
    ):
        assert timeout is None
        if self._barge_events_emitted:
            return
        self._barge_events_emitted = True
        yield RecognitionResult("nuova", is_final=False)
        yield RecognitionResult("nuova domanda in corso", is_final=False)
        assert self.api.response_finished.wait(timeout=1)
        # Cancelling the first response must not stop/flush the microphone
        # before the user finishes the interruption utterance.
        assert getattr(stop_event, "is_set")() is False
        yield RecognitionResult("nuova domanda", is_final=True)

    def close(self) -> None:
        pass


class SilentSoundPlayer:
    def play_sound(self, _sound_file: str) -> None:
        pass

    def close(self) -> None:
        pass


class RepeatedBlockingTTS:
    def __init__(self, interruptions: int) -> None:
        self.interruptions = interruptions
        self.spoken: list[str] = []
        self.interrupt_calls = 0
        self._lock = threading.Lock()
        self._speaking = threading.Event()
        self._active_release: threading.Event | None = None

    @property
    def is_speaking(self) -> bool:
        return self._speaking.is_set()

    def wait_until_speaking(self, timeout: float = 1.0) -> bool:
        return self._speaking.wait(timeout)

    def speak(self, text: str) -> None:
        release = threading.Event()
        with self._lock:
            self.spoken.append(text)
            position = len(self.spoken)
            self._active_release = release
            self._speaking.set()
        try:
            if position <= self.interruptions:
                assert release.wait(timeout=2)
        finally:
            with self._lock:
                if self._active_release is release:
                    self._active_release = None
                self._speaking.clear()

    def interrupt(self) -> bool:
        with self._lock:
            self.interrupt_calls += 1
            release = self._active_release
        if release is None:
            return False
        release.set()
        return True

    def close(self) -> None:
        pass


class RepeatedCancellableAPI:
    def __init__(self, tts: RepeatedBlockingTTS) -> None:
        self.tts = tts
        self.messages: list[str] = []
        self.cancel_calls = 0
        self._lock = threading.Lock()
        self._active: threading.Event | None = None

    def talk(self, message: str, context: str | None = None) -> str:
        assert context is None
        cancellation = threading.Event()
        with self._lock:
            self._active = cancellation
        self.messages.append(message)
        try:
            self.tts.speak(f"response for {message}")
            if cancellation.is_set():
                raise RuntimeError("request cancelled")
            return f"complete response for {message}"
        finally:
            with self._lock:
                if self._active is cancellation:
                    self._active = None

    def cancel_current(self) -> None:
        self.cancel_calls += 1
        with self._lock:
            active = self._active
        if active is not None:
            active.set()

    def close(self) -> None:
        pass


class RepeatedBargeInRecognizer:
    def __init__(self, tts: RepeatedBlockingTTS, follow_ups: list[str]) -> None:
        self._tts = tts
        self._follow_ups = iter(follow_ups)
        self.listen_sessions = 0

    def listen_once(self, timeout: float) -> RecognitionResult:
        assert timeout > 0
        return RecognitionResult("Emilia, initial question", is_final=True)

    def listen_events(
        self,
        timeout: float | None,
        *,
        stop_event: object,
    ):
        assert timeout is None
        self.listen_sessions += 1
        try:
            follow_up = next(self._follow_ups)
        except StopIteration:
            return
        assert self._tts.wait_until_speaking()
        yield RecognitionResult(
            follow_up.split()[0],
            is_final=False,
            frame_energy=0.2,
        )
        assert getattr(stop_event, "is_set")() is False
        yield RecognitionResult(follow_up, is_final=True, frame_energy=0.2)

    def close(self) -> None:
        pass


class ScriptedOllamaClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, object]] = []

    def chat(self, **kwargs: object):
        self.calls.append(kwargs)
        response = next(self._responses)

        def stream():
            yield {"message": {"content": response}, "done": False}
            yield {"message": {"content": ""}, "done": True, "done_reason": "stop"}

        return stream()


class CountingAPIClient(APIClient):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.cancel_calls = 0

    def cancel_current(self) -> None:
        self.cancel_calls += 1
        super().cancel_current()


def test_barge_in_interrupts_audio_and_stream_then_processes_follow_up() -> None:
    tts = BlockingTTS()
    api = CancellableAPI(tts)
    settings = config.Settings(
        project_root=config.PROJECT_ROOT,
        language="it",
        barge_in_enabled=True,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        assistant = VoiceAssistant(
            settings=settings,
            tts=tts,
            sound_player=SilentSoundPlayer(),
            api_client=api,
            speech_recognizer=BargeInRecognizer(api),
            barge_in_detector=BargeInDetector(minimum_active_seconds=0.0),
            sound_executor=executor,
        )

        assert assistant.run_once()
        assert tts.interrupt_calls == 1
        assert api.cancel_calls == 1
        assistant.close()

    assert tts.interrupt_calls == 2
    assert api.cancel_calls == 2
    assert api.messages == ["prima domanda", "nuova domanda"]
    assert tts.spoken == [
        "response for prima domanda",
        "response for nuova domanda",
        "queued fragment",
    ]


def test_three_consecutive_barge_ins_rearm_every_turn() -> None:
    tts = RepeatedBlockingTTS(interruptions=3)
    api = RepeatedCancellableAPI(tts)
    recognizer = RepeatedBargeInRecognizer(
        tts,
        [
            "only the second one",
            "how long is its year",
            "compare it with Earth",
        ]
    )
    settings = config.Settings(
        project_root=config.PROJECT_ROOT,
        language="it",
        barge_in_enabled=True,
    )
    assistant = VoiceAssistant(
        settings=settings,
        tts=tts,
        sound_player=SilentSoundPlayer(),
        api_client=api,
        speech_recognizer=recognizer,
    )

    assert assistant.run_once()

    assert api.messages == [
        "initial question",
        "only the second one",
        "how long is its year",
        "compare it with Earth",
    ]
    assert api.cancel_calls == 3
    assert tts.interrupt_calls == 3
    assert recognizer.listen_sessions == 4
    assistant.close()


def test_three_barge_ins_preserve_api_client_local_history_end_to_end() -> None:
    tts = RepeatedBlockingTTS(interruptions=3)
    ollama = ScriptedOllamaClient(
        [
            "Mercury, Venus, and Earth.",
            "Venus is the second planet.",
            "Its year lasts about 225 Earth days.",
            "Venus has a shorter year than Earth.",
        ]
    )
    api = CountingAPIClient(client=ollama, tts=tts, retry_wait=0)
    recognizer = RepeatedBargeInRecognizer(
        tts,
        [
            "only the second one",
            "how long is its year",
            "compare it with Earth",
        ],
    )
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            language="it",
            barge_in_enabled=True,
        ),
        tts=tts,
        sound_player=SilentSoundPlayer(),
        api_client=api,
        speech_recognizer=recognizer,
        barge_in_detector=BargeInDetector(),
    )

    assert assistant.run_once()

    assert api.cancel_calls == 3
    assert len(ollama.calls) == 4
    assert ollama.calls[3]["messages"] == [
        {"role": "user", "content": "initial question"},
        {"role": "user", "content": "only the second one"},
        {"role": "user", "content": "how long is its year"},
        {"role": "user", "content": "compare it with Earth"},
    ]
    snapshot = api.conversation.snapshot()
    assert snapshot.turn_count == 4
    assert snapshot.history_message_count == 5
    assert snapshot.active_turn is None
    assert assistant.conversation_state is VoiceConversationState.LISTENING
    assistant.close()
    assert assistant.conversation_state is VoiceConversationState.IDLE


def test_fifty_turn_repeated_barge_in_stress_keeps_state_bounded() -> None:
    turn_count = 50
    tts = RepeatedBlockingTTS(interruptions=turn_count - 1)
    ollama = ScriptedOllamaClient(
        [f"Scripted answer {position}." for position in range(1, turn_count + 1)]
    )
    api = CountingAPIClient(client=ollama, tts=tts, retry_wait=0)
    follow_ups = [f"follow up {position}" for position in range(2, turn_count + 1)]
    recognizer = RepeatedBargeInRecognizer(tts, follow_ups)
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            language="it",
            barge_in_enabled=True,
        ),
        tts=tts,
        sound_player=SilentSoundPlayer(),
        api_client=api,
        speech_recognizer=recognizer,
        barge_in_detector=BargeInDetector(),
    )

    assert assistant.run_once()

    assert api.cancel_calls == turn_count - 1
    assert tts.interrupt_calls == turn_count - 1
    assert len(ollama.calls) == turn_count
    snapshot = api.conversation.snapshot()
    assert snapshot.turn_count == turn_count
    assert snapshot.retained_turn_count == 20
    assert snapshot.history_message_count == 21
    # The configured cap bounds provider input without resetting the logical
    # session. Interrupted assistant text is absent, so 20 prior user turns plus
    # the current user turn are transmitted on the final request.
    assert len(ollama.calls[-1]["messages"]) == 21
    assert ollama.calls[-1]["messages"][-1] == {
        "role": "user",
        "content": "follow up 50",
    }
    assistant.close()


class RepeatedCodexTurn:
    def __init__(self, thread_id: str, turn_id: str, text: str) -> None:
        self.thread_id = thread_id
        self.id = turn_id
        self._text = text
        self.interrupt_calls = 0

    def stream(self):
        return [
            {"method": "item/agentMessage/delta", "payload": {"delta": self._text}},
            {"method": "turn/completed", "payload": {"turn": {"status": "completed"}}},
        ]

    def interrupt(self) -> None:
        self.interrupt_calls += 1


class RepeatedCodexRuntime:
    def __init__(self) -> None:
        self.responses = iter(
            [
                "Mercury Venus Earth",
                "Venus is second",
                "Its year is 225 days",
                "Venus has the shorter year",
            ]
        )
        self.calls: list[dict[str, object]] = []
        self.thread_number = 0
        self.turn_number = 0
        self.closed = False

    def account_kind(self) -> str:
        return "chatgpt"

    def start_turn(self, **kwargs: object) -> RepeatedCodexTurn:
        self.turn_number += 1
        call = dict(kwargs)
        if "thread_id" in call:
            call["operation"] = "resume"
            thread_id = str(call["thread_id"])
        else:
            call["operation"] = "start"
            self.thread_number += 1
            thread_id = f"thread-{self.thread_number}"
        self.calls.append(call)
        return RepeatedCodexTurn(
            thread_id,
            f"turn-{self.turn_number}",
            next(self.responses),
        )

    def close(self) -> None:
        self.closed = True


def repeated_codex_settings() -> config.LLMSettings:
    return config.LLMSettings(
        routing_file=config.PROJECT_ROOT / "tests" / "codex-e2e.toml",
        routing_policy="remote_first",
        remote_enabled=True,
        privacy=config.LLMPrivacySettings(
            default="remote_allowed",
            allow_remote_transcripts=True,
            allow_remote_context=True,
        ),
        budget=config.LLMBudgetSettings(enabled=False),
        observability=config.LLMObservabilitySettings(metrics_enabled=False),
        talk=config.LLMModeSettings(candidates=("codex-talk",)),
        providers=(
            config.LLMProviderSettings(
                name="openai-codex",
                adapter="codex_app_server",
                endpoint="stdio://codex",
                locality="remote",
            ),
        ),
        targets=(
            config.LLMTargetSettings(
                name="codex-talk",
                provider="openai-codex",
                model="gpt-5.6-luna",
            ),
        ),
    )


def test_three_barge_ins_recover_codex_without_new_logical_conversation(
    caplog,
) -> None:
    caplog.set_level("INFO")
    tts = RepeatedBlockingTTS(interruptions=3)
    runtime = RepeatedCodexRuntime()
    codex = CodexAppServerAdapter(
        "openai-codex",
        runtime=runtime,
        allow_remote_context=True,
        interrupt_ack_timeout_seconds=0.1,
    )
    api = CountingAPIClient(
        client=ScriptedOllamaClient([]),
        tts=tts,
        llm_settings=repeated_codex_settings(),
        providers={"openai-codex": codex},
        connectivity="online",
        retry_wait=0,
        language="en",
    )
    recognizer = RepeatedBargeInRecognizer(
        tts,
        [
            "only the second one",
            "how long is its year",
            "compare it with Earth",
        ],
    )
    assistant = VoiceAssistant(
        settings=config.Settings(
            project_root=config.PROJECT_ROOT,
            language="en",
            barge_in_enabled=True,
            llm=repeated_codex_settings(),
        ),
        tts=tts,
        sound_player=SilentSoundPlayer(),
        api_client=api,
        speech_recognizer=recognizer,
    )

    assert assistant.run_once()

    assert api.cancel_calls == 3
    assert [call["operation"] for call in runtime.calls] == [
        "start",
        "start",
        "start",
        "start",
    ]
    assert "initial question" in str(runtime.calls[0]["prompt"])
    assert "initial question" in str(runtime.calls[1]["prompt"])
    assert "only the second one" in str(runtime.calls[1]["prompt"])
    assert "Mercury Venus Earth" not in str(runtime.calls[1]["prompt"])
    final_prompt = str(runtime.calls[-1]["prompt"])
    for user_turn in (
        "initial question",
        "only the second one",
        "how long is its year",
        "compare it with Earth",
    ):
        assert final_prompt.count(user_turn) == 1

    snapshot = api.conversation.snapshot()
    assert snapshot.turn_count == 4
    assert snapshot.history_message_count == 5
    assert snapshot.provider_threads == (("openai-codex", "thread-4"),)
    correlation = safe_conversation_identifier(snapshot.session_id)
    trace = [record.getMessage() for record in caplog.records]
    assert any(
        f"conversation_session={correlation}" in message
        and "event=barge_in_detected" in message
        for message in trace
    )
    assert any(
        f"conversation_session={correlation}" in message
        and "action=thread_recover" in message
        for message in trace
    )
    assert snapshot.session_id not in caplog.text
    assert "only the second one" not in caplog.text
    deadline = time.monotonic() + 1
    while any(
        thread.name == "helios-codex-stream" and thread.is_alive()
        for thread in threading.enumerate()
    ) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not any(
        thread.name == "helios-codex-stream" and thread.is_alive()
        for thread in threading.enumerate()
    )
    assistant.close()
    codex.close()
