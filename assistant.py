"""Runtime orchestration for the voice assistant."""

from __future__ import annotations

import inspect
import logging
import random
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError, wait
from enum import Enum, auto
from typing import Any

import config
from api.api_client import APIClient, APIClientError
from api.conversation import safe_conversation_identifier
from api.metrics import record_safely
from api.streaming import CancellationController
from audio.backchannel import BackchannelSession
from audio.sound_player import SoundPlaybackError, SoundPlayer
from audio.tts import PiperTTS, TTSError
from recognizer.barge_in_detector import BargeInDetector
from recognizer.echo_suppression_policy import ConservativeEchoSuppressionPolicy
from recognizer.speech_recognizer import (
    RecognitionResult,
    SpeechRecognitionError,
    SpeechRecognizer,
)

logger = logging.getLogger(__name__)

_BARGE_IN_LISTEN_SLICE_SECONDS = 0.25
_TTS_ECHO_TAIL_SECONDS = 0.25
_TASK_SHUTDOWN_TIMEOUT_SECONDS = 2.0
_RESPONSE_CANCEL_TIMEOUT_SECONDS = 2.5


class _BargeInCaptureStop:
    """Stop on response completion, or endpoint after a detected interruption."""

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        follow_up_timeout_seconds: float,
    ) -> None:
        self._clock = clock
        self._follow_up_timeout_seconds = follow_up_timeout_seconds
        self._lock = threading.Lock()
        self._response_finished = False
        self._detected_at: float | None = None
        self._forced = False
        self._finished = threading.Event()

    def response_finished(self) -> None:
        with self._lock:
            self._response_finished = True

    def barge_in_detected(self) -> None:
        with self._lock:
            if self._detected_at is None:
                self._detected_at = self._clock()

    def recognition_activity(self) -> None:
        """Refresh the post-detection inactivity deadline."""

        with self._lock:
            if self._detected_at is not None:
                self._detected_at = self._clock()

    def force_stop(self) -> None:
        with self._lock:
            self._forced = True

    def capture_finished(self) -> None:
        self._finished.set()

    def wait_finished(self, timeout: float | None = None) -> bool:
        return self._finished.wait(timeout)

    def is_set(self) -> bool:
        with self._lock:
            detected_at = self._detected_at
            response_finished = self._response_finished
            forced = self._forced
        if forced:
            return True
        if detected_at is None:
            return response_finished
        return self._clock() - detected_at >= self._follow_up_timeout_seconds


class AssistantState(Enum):
    COMMAND = auto()
    RAG = auto()


class VoiceConversationState(Enum):
    IDLE = auto()
    LISTENING = auto()
    USER_TURN_FINALIZED = auto()
    GENERATING = auto()
    SPEAKING = auto()
    BARGE_IN_DETECTED = auto()
    CANCELLING = auto()
    CAPTURING_FOLLOW_UP = auto()
    FOLLOW_UP_FINALIZED = auto()


class AssistantRuntimeError(RuntimeError):
    """Base class for recoverable orchestration failures."""


class RagCommandError(AssistantRuntimeError):
    """Raised when retrieval or RAG result presentation fails."""


class VoiceAssistant:
    """Compose runtime services and coordinate the command/RAG state machine."""

    def __init__(
        self,
        *,
        settings: config.Settings = config.SETTINGS,
        tts: Any | None = None,
        sound_player: Any | None = None,
        api_client: Any | None = None,
        speech_recognizer: Any | None = None,
        rag_searcher: Any | None = None,
        rag_factory: Callable[[], Any] | None = None,
        barge_in_detector: Any | None = None,
        sound_executor: Any | None = None,
        conversation_executor: Any | None = None,
        choice: Callable[[tuple[str, ...]], str] = random.choice,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        metrics: Any | None = None,
    ) -> None:
        self.settings = settings
        self.profile = settings.profile
        if conversation_executor is not None and settings.barge_in_enabled:
            max_workers = getattr(conversation_executor, "_max_workers", None)
            if isinstance(max_workers, int) and max_workers < 2:
                raise ValueError(
                    "conversation_executor needs at least two workers when barge-in is enabled"
                )

        if tts is None and api_client is not None:
            if isinstance(api_client, APIClient):
                tts = api_client.configured_tts
            else:
                tts = getattr(api_client, "tts", None)
        self.tts = tts if tts is not None else PiperTTS(self.profile.tts_model)
        self.sound_player = sound_player if sound_player is not None else SoundPlayer()
        if api_client is None:
            self.api_client = APIClient(
                api_url=settings.ollama_host,
                model_talk=self.profile.talk_model,
                model_think=settings.think_model,
                tts=self.tts,
                language=settings.language,
                llm_settings=settings.llm,
                kpi_settings=settings.kpi,
                metrics=metrics,
            )
        else:
            self.api_client = api_client
            if isinstance(api_client, APIClient) and api_client.configured_tts is None:
                api_client.tts = self.tts
        self.metrics = metrics if metrics is not None else getattr(self.api_client, "metrics", None)
        self.speech_recognizer = (
            speech_recognizer
            if speech_recognizer is not None
            else SpeechRecognizer(self.profile.vosk_model)
        )
        if barge_in_detector is not None:
            self._barge_in_detector = barge_in_detector
        elif settings.barge_in_enabled:
            self._barge_in_detector = BargeInDetector(
                recognition_event_energy=settings.barge_in_event_energy,
                suppression_policy=ConservativeEchoSuppressionPolicy(
                    expected_echo_energy=settings.barge_in_expected_echo_energy,
                    minimum_interrupt_energy=(settings.barge_in_minimum_interrupt_energy),
                ),
            )
        else:
            self._barge_in_detector = None

        self._rag_searcher = rag_searcher
        self._rag_factory = rag_factory
        self._rag_lock = threading.RLock()
        self._rag_prepare_future: Future[Any] | None = None
        self._rag_prepared = False
        self._backchannel_lock = threading.RLock()
        self._backchannel_index = 0
        self._ready_backchannel_phrases: tuple[str, ...] = ()
        self._backchannel_prepare_future: Future[Any] | None = None
        self._last_backchannel_session: BackchannelSession | None = None
        self._capture_lock = threading.Lock()
        self._active_capture_stop: _BargeInCaptureStop | None = None
        self._response_control_lock = threading.Lock()
        self._active_response_cancellation: CancellationController | None = None
        self._close_lock = threading.Lock()
        self._task_lock = threading.Lock()
        self._tasks: set[Future[Any]] = set()
        self._sound_executor = sound_executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="helios-sound",
        )
        self._owns_sound_executor = sound_executor is None
        self._conversation_executor = conversation_executor or ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="helios-conversation",
        )
        self._owns_conversation_executor = conversation_executor is None
        self._choice = choice
        self._sleep = sleep
        self._clock = clock
        self._voice_conversation_lock = threading.Lock()
        self._voice_conversation_active = False
        self._voice_conversation_last_activity_at: float | None = None
        self._voice_conversation_state = VoiceConversationState.IDLE
        self.state = AssistantState.COMMAND
        self._running = False
        self._stop_requested = False
        self._closed = False

    @property
    def conversation_state(self) -> VoiceConversationState:
        with self._voice_conversation_lock:
            return self._voice_conversation_state

    def _conversation_coordinates(self, *, next_turn: bool = False) -> tuple[str, int | None]:
        session_id = "none"
        turn_number: int | None = None
        conversation = getattr(self.api_client, "conversation", None)
        snapshot = getattr(conversation, "snapshot", None)
        if callable(snapshot):
            try:
                current = snapshot()
                raw_session_id = str(getattr(current, "session_id", "")) or None
                session_id = safe_conversation_identifier(raw_session_id)
                if next_turn:
                    turn_number = int(getattr(current, "turn_count", 0)) + 1
                else:
                    turn_number = getattr(current, "active_turn", None)
            except Exception:
                pass
        return session_id, turn_number

    def _transition_voice_conversation(self, state: VoiceConversationState) -> None:
        with self._voice_conversation_lock:
            previous = self._voice_conversation_state
            if previous is state:
                return
            self._voice_conversation_state = state
        session_id, turn_number = self._conversation_coordinates()
        logger.info(
            "conversation_session=%s turn=%s event=state_transition previous=%s next=%s",
            session_id,
            turn_number,
            previous.name.lower(),
            state.name.lower(),
        )

    def _activate_voice_conversation(self) -> None:
        with self._voice_conversation_lock:
            self._voice_conversation_active = True
            self._voice_conversation_last_activity_at = self._clock()
        logger.info("event=voice_conversation_activated")

    def _touch_voice_conversation(self) -> None:
        with self._voice_conversation_lock:
            if self._voice_conversation_active:
                self._voice_conversation_last_activity_at = self._clock()

    def _deactivate_voice_conversation(self) -> None:
        with self._voice_conversation_lock:
            self._voice_conversation_active = False
            self._voice_conversation_last_activity_at = None
        self._transition_voice_conversation(VoiceConversationState.IDLE)

    def _voice_conversation_is_active(self) -> bool:
        expired = False
        with self._voice_conversation_lock:
            if not self._voice_conversation_active:
                return False
            last_activity = self._voice_conversation_last_activity_at
            if (
                last_activity is not None
                and self._clock() - last_activity
                >= self.settings.llm.context_idle_timeout_seconds
            ):
                self._voice_conversation_active = False
                self._voice_conversation_last_activity_at = None
                expired = True
        if expired:
            self._transition_voice_conversation(VoiceConversationState.IDLE)
            reset = getattr(self.api_client, "reset_conversation", None)
            if callable(reset):
                try:
                    reset(reason="voice_idle_timeout")
                except Exception:
                    logger.warning("Unable to reset an expired conversation", exc_info=True)
            logger.info("event=voice_conversation_expired reason=idle_timeout")
            return False
        return True

    def _discard_task(self, future: Future[Any]) -> None:
        with self._task_lock:
            self._tasks.discard(future)

    def _track_task(self, future: Future[Any]) -> Future[Any]:
        with self._task_lock:
            self._tasks.add(future)
        add_done_callback = getattr(future, "add_done_callback", None)
        if callable(add_done_callback):
            add_done_callback(self._discard_task)
        return future

    def _submit_task(
        self,
        executor: Any,
        function: Callable[..., Any],
        *args: Any,
    ) -> Future[Any]:
        with self._close_lock:
            if self._closed or self._stop_requested:
                raise AssistantRuntimeError("Voice assistant is closed")
            future = executor.submit(function, *args)
            return self._track_task(future)

    def _speak_observed(self, text: str, *, scope: str) -> Any:
        started_at = self._clock()
        try:
            speak_with_timing = getattr(self.tts, "speak_with_timing", None)
            timing = (
                speak_with_timing(text) if callable(speak_with_timing) else self.tts.speak(text)
            )
        except Exception:
            record_safely(
                self.metrics,
                "tts_failed",
                resource_scope=scope,
                outcome="failed",
                success=False,
                latency_ms=(self._clock() - started_at) * 1_000,
            )
            raise
        values: dict[str, Any] = {}
        if timing is not None:
            for attribute, field in (
                ("synthesis_ms", "tts_synthesis_ms"),
                ("playback_ms", "audio_playback_ms"),
                ("audio_duration_ms", "audio_duration_ms"),
            ):
                value = getattr(timing, attribute, None)
                if value is not None:
                    values[field] = value
        record_safely(
            self.metrics,
            "tts_completed",
            resource_scope=scope,
            outcome="succeeded",
            success=True,
            latency_ms=(self._clock() - started_at) * 1_000,
            **values,
        )
        return timing

    @staticmethod
    def _contains_phrase(text: str, phrase: str) -> bool:
        return bool(
            re.search(
                rf"(?<!\w){re.escape(phrase.lower())}(?!\w)",
                text.lower(),
            )
        )

    def contains_wake_word(self, command: str) -> bool:
        if not command:
            return False
        return any(
            self._contains_phrase(command, wake_word)
            for wake_word in self.profile.wake_word_aliases
        )

    @staticmethod
    def _remove_first_phrase(text: str, phrases: tuple[str, ...]) -> str:
        for phrase in phrases:
            match = re.search(
                rf"(?<!\w){re.escape(phrase)}(?!\w)",
                text,
                flags=re.IGNORECASE,
            )
            if match is None:
                continue
            left = re.sub(r"[\s,;:!?.-]+$", "", text[: match.start()])
            right = re.sub(r"^[\s,;:!?.-]+", "", text[match.end() :])
            return " ".join(part for part in (left, right) if part).strip()
        return text.strip()

    def _without_wake_word(self, command: str) -> str:
        ordered_aliases = (
            self.profile.wake_word,
            *(alias for alias in self.profile.wake_word_aliases if alias != self.profile.wake_word),
        )
        return self._remove_first_phrase(command, ordered_aliases)

    def _think_prompt(self, command: str) -> str | None:
        for word in self.profile.think_words:
            match = re.match(
                rf"^\s*{re.escape(word)}(?!\w)",
                command,
                flags=re.IGNORECASE,
            )
            if match is not None:
                return re.sub(r"^[\s,;:!?.-]+", "", command[match.end() :]).strip()
        return None

    def _contains_rag_word(self, command: str) -> bool:
        return self._contains_phrase(command, self.profile.rag_word)

    def process_command(
        self,
        command: str,
        *,
        pipeline_started_at: float | None = None,
        cancellation: CancellationController | None = None,
    ) -> str | None:
        if not command:
            logger.warning("No command to process")
            return None
        if not self.contains_wake_word(command):
            logger.info("Ignoring command without a configured wake word")
            return None

        model_prompt = self._without_wake_word(command)
        if not model_prompt:
            logger.info("Ignoring wake word without a command")
            return None

        return self._process_model_prompt(
            model_prompt,
            pipeline_started_at=pipeline_started_at,
            cancellation=cancellation,
        )

    def _process_model_prompt(
        self,
        model_prompt: str,
        *,
        pipeline_started_at: float | None = None,
        cancellation: CancellationController | None = None,
    ) -> str | None:
        """Process an already-authorized conversational prompt."""

        normalized = model_prompt.lower()
        for question in self.profile.presentation_questions:
            if question in normalized:
                response = self._choice(self.profile.presentation_answers)
                self._speak_observed(response, scope="presentation")
                return response

        think_prompt = self._think_prompt(model_prompt)
        if think_prompt is not None:
            if not think_prompt:
                logger.info("Ignoring think trigger without a question")
                return None
            return self._invoke_model(
                self.api_client.think,
                think_prompt,
                pipeline_started_at=pipeline_started_at,
                tts=True,
                cancellation=cancellation,
            )

        return self._invoke_model(
            self.api_client.talk,
            model_prompt,
            pipeline_started_at=pipeline_started_at,
            cancellation=cancellation,
        )

    @staticmethod
    def _accepts_keyword(function: Callable[..., Any], name: str) -> bool:
        try:
            parameters = inspect.signature(function).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.name == name or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    def _next_backchannel_phrase(self) -> str:
        has_preloaded = getattr(self.tts, "has_preloaded_phrase", None)
        if callable(has_preloaded):
            phrases = tuple(
                phrase for phrase in self.profile.backchannel_phrases if has_preloaded(phrase)
            )
        else:
            with self._backchannel_lock:
                phrases = self._ready_backchannel_phrases
            if (
                not phrases
                and not callable(getattr(self.tts, "preload_phrases", None))
                and callable(getattr(self.tts, "speak_preloaded", None))
            ):
                # Injected test/deployment backends may represent an inherently
                # pre-recorded cue set without exposing Piper's cache query.
                phrases = self.profile.backchannel_phrases
        if not phrases:
            raise AssistantRuntimeError("No preloaded backchannel phrase is ready")
        with self._backchannel_lock:
            phrase = phrases[self._backchannel_index % len(phrases)]
            self._backchannel_index += 1
            return phrase

    def _start_backchannel(self) -> BackchannelSession:
        with self._close_lock:
            if self._closed:
                raise AssistantRuntimeError("Voice assistant is closed")
            session = BackchannelSession(
                tts=self.tts,
                phrase=self._next_backchannel_phrase(),
                delay_seconds=self.settings.backchannel_delay_seconds,
                executor=self._conversation_executor,
            )
            self._track_task(session.future)
            with self._backchannel_lock:
                self._last_backchannel_session = session
        return session

    def _invoke_model(
        self,
        method: Callable[..., str],
        prompt: str,
        *,
        pipeline_started_at: float | None,
        tts: bool | None = None,
        cancellation: CancellationController | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {"context": None}
        if tts is not None:
            kwargs["tts"] = tts
        if isinstance(self.api_client, APIClient):
            kwargs["pipeline_started_at"] = pipeline_started_at
        if cancellation is not None and self._accepts_keyword(method, "cancellation"):
            kwargs["cancellation"] = cancellation

        backchannel: BackchannelSession | None = None
        if self.settings.barge_in_enabled and self._accepts_keyword(
            method,
            "before_first_speech",
        ):
            try:
                backchannel = self._start_backchannel()
            except AssistantRuntimeError:
                logger.debug("No safe preloaded backchannel is currently available")
            else:
                kwargs["before_first_speech"] = backchannel.supersede
        try:
            return method(prompt, **kwargs)
        finally:
            if backchannel is not None:
                backchannel.supersede()

    @staticmethod
    def _rag_result_text(result: Any) -> str:
        if isinstance(result, str):
            return result
        if result is None:
            return ""

        if isinstance(result, (list, tuple)):
            formatted: list[str] = []
            for position, item in enumerate(result, start=1):
                text = getattr(item, "text", None)
                if text is not None:
                    formatted.append(str(text))
                elif (
                    isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[0], int)
                ):
                    formatted.append(
                        f"Risposta {position}: indice {item[0]}, score {float(item[1]):.2f}"
                    )
                else:
                    formatted.append(str(item))
            return "\n".join(formatted)
        return str(result)

    def process_rag_command(self, command: str, searcher: Any | None = None) -> str:
        if not command:
            raise RagCommandError("RAG command cannot be empty")
        started_at = self._clock()
        try:
            if searcher is not None:
                result = searcher.run(
                    query=command,
                    top_k=self.settings.top_k,
                )
            else:
                with self._rag_lock:
                    result = self._get_rag_searcher().run(
                        query=command,
                        top_k=self.settings.top_k,
                    )
                    self._rag_prepared = True
            result_text = self._rag_result_text(result).strip()
            if not result_text:
                raise RagCommandError("The RAG search returned no text")
            rag_ms = (self._clock() - started_at) * 1_000
            record_safely(
                self.metrics,
                "rag_completed",
                outcome="succeeded",
                success=True,
                rag_ms=rag_ms,
            )
        except RagCommandError:
            record_safely(
                self.metrics,
                "rag_failed",
                outcome="failed",
                success=False,
                rag_ms=(self._clock() - started_at) * 1_000,
            )
            raise
        except Exception as exc:
            record_safely(
                self.metrics,
                "rag_failed",
                outcome="failed",
                success=False,
                rag_ms=(self._clock() - started_at) * 1_000,
            )
            raise RagCommandError("Unable to answer the RAG command") from exc

        try:
            self._speak_observed(
                self.profile.rag_result_prefix + result_text,
                scope="rag",
            )
            return result_text
        except Exception as exc:
            raise RagCommandError("Unable to present the RAG result") from exc

    def _get_rag_searcher(self) -> Any:
        with self._rag_lock:
            if self._rag_searcher is None:
                try:
                    if self._rag_factory is not None:
                        self._rag_searcher = self._rag_factory()
                    else:
                        from document.rag_system import RagSystem

                        self._rag_searcher = RagSystem(
                            txt_dir=str(self.settings.upload_folder),
                            emb_file=str(self.settings.embeddings_file),
                            model_name=str(self.settings.sentence_transformer_model),
                            reindex=False,
                        )
                except Exception as exc:
                    raise RagCommandError("Unable to initialize the RAG searcher") from exc
            return self._rag_searcher

    def _prepare_rag(self) -> None:
        try:
            with self._rag_lock:
                searcher = self._get_rag_searcher()
                prepare = getattr(searcher, "prepare", None)
                prepared = True if not callable(prepare) else prepare() is not False
                self._rag_prepared = prepared
                logger.info("RAG background preparation completed (ready=%s)", prepared)
        except Exception:
            logger.warning("RAG background preparation failed", exc_info=True)

    def prepare_rag_async(self) -> Future[Any] | None:
        """Queue lazy RAG preparation behind the entry notification sound."""

        with self._rag_lock:
            if self._closed or self._rag_prepared:
                return self._rag_prepare_future
            if self._rag_prepare_future is not None and not self._rag_prepare_future.done():
                return self._rag_prepare_future
            self._rag_prepare_future = self._submit_task(
                self._sound_executor,
                self._prepare_rag,
            )
            return self._rag_prepare_future

    def _prepare_backchannels(self) -> None:
        preload = getattr(self.tts, "preload_phrases", None)
        if not callable(preload):
            logger.debug("TTS backend cannot preload conversational backchannels")
            return
        try:
            loaded = preload(self.profile.backchannel_phrases)
            has_preloaded = getattr(self.tts, "has_preloaded_phrase", None)
            if callable(has_preloaded):
                ready = tuple(
                    phrase for phrase in self.profile.backchannel_phrases if has_preloaded(phrase)
                )
            elif loaded is None:
                ready = self.profile.backchannel_phrases
            else:
                loaded_values = {str(value) for value in loaded}
                ready = tuple(
                    phrase for phrase in self.profile.backchannel_phrases if phrase in loaded_values
                )
            with self._backchannel_lock:
                self._ready_backchannel_phrases = ready
            logger.info(
                "Prepared %s local backchannel phrase(s) for %s",
                len(ready),
                self.profile.code,
            )
        except Exception:
            # Missing fillers reduce polish but must not prevent normal speech.
            has_preloaded = getattr(self.tts, "has_preloaded_phrase", None)
            if callable(has_preloaded):
                with self._backchannel_lock:
                    self._ready_backchannel_phrases = tuple(
                        phrase
                        for phrase in self.profile.backchannel_phrases
                        if has_preloaded(phrase)
                    )
            logger.warning("Backchannel preparation failed", exc_info=True)

    def prepare_backchannels_async(self) -> Future[Any] | None:
        """Pre-synthesize the active language's fillers away from the turn path."""

        with self._close_lock:
            if not self.settings.barge_in_enabled or self._closed:
                return None
            with self._backchannel_lock:
                future = self._backchannel_prepare_future
                if future is not None and not future.done():
                    return future
                if set(self._ready_backchannel_phrases) == set(self.profile.backchannel_phrases):
                    return future
                future = self._conversation_executor.submit(self._prepare_backchannels)
                self._backchannel_prepare_future = self._track_task(future)
                return self._backchannel_prepare_future

    @staticmethod
    def _log_sound_failure(future: Future[Any]) -> None:
        try:
            future.result()
        except Exception:
            logger.warning("Notification sound playback failed", exc_info=True)

    def play_sound_async(self, sound_file: str) -> Any:
        future = self._submit_task(
            self._sound_executor,
            self.sound_player.play_sound,
            sound_file,
        )
        add_done_callback = getattr(future, "add_done_callback", None)
        if callable(add_done_callback):
            add_done_callback(self._log_sound_failure)
        return future

    def _recognize_once_unobserved(self) -> RecognitionResult | None:
        listen_once = getattr(self.speech_recognizer, "listen_once", None)
        if callable(listen_once):
            result = listen_once(timeout=self.settings.listen_timeout)
            if result is None:
                return None
            if isinstance(result, str):
                return RecognitionResult(result.strip(), is_final=True)
            return RecognitionResult(
                str(getattr(result, "text", "")).strip(),
                bool(getattr(result, "is_final", True)),
            )

        # Compatibility with recognizers that only implement the legacy
        # generator.  The final non-empty item is treated as final.
        latest = ""
        for text in self.speech_recognizer.listen(timeout=self.settings.listen_timeout):
            if str(text).strip():
                latest = str(text).strip()
        return RecognitionResult(latest, is_final=True) if latest else None

    def _recognize_once(self) -> RecognitionResult | None:
        started_at = self._clock()
        try:
            result = self._recognize_once_unobserved()
        except Exception:
            record_safely(
                self.metrics,
                "voice_listen_completed",
                outcome="failed",
                success=False,
                listening_ms=(self._clock() - started_at) * 1_000,
            )
            raise
        elapsed_ms = (self._clock() - started_at) * 1_000
        if result is None:
            outcome = "timeout"
        elif result.is_final and result.text:
            outcome = "final"
        else:
            outcome = "partial"
        record_safely(
            self.metrics,
            "voice_listen_completed",
            outcome=outcome,
            success=outcome == "final",
            listening_ms=elapsed_ms,
        )
        return result

    @staticmethod
    def _coerce_recognition_result(result: Any) -> RecognitionResult:
        if isinstance(result, str):
            return RecognitionResult(result.strip(), is_final=True)
        frame_energy = getattr(result, "frame_energy", None)
        if not isinstance(frame_energy, (int, float)) or isinstance(frame_energy, bool):
            frame_energy = None
        return RecognitionResult(
            str(getattr(result, "text", "")).strip(),
            bool(getattr(result, "is_final", True)),
            frame_energy=(float(frame_energy) if frame_energy is not None else None),
        )

    def _set_active_response_cancellation(
        self,
        cancellation: CancellationController | None,
    ) -> None:
        with self._response_control_lock:
            self._active_response_cancellation = cancellation

    def _interrupt_current_response(
        self,
        cancellation: CancellationController | None = None,
    ) -> None:
        session_id, turn_number = self._conversation_coordinates()
        logger.info(
            "conversation_session=%s turn=%s event=response_cancel_requested",
            session_id,
            turn_number,
        )
        if cancellation is None:
            with self._response_control_lock:
                cancellation = self._active_response_cancellation
        if cancellation is not None:
            cancellation.cancel()
        interrupt = getattr(self.tts, "interrupt", None)
        if callable(interrupt):
            try:
                interrupt()
            except Exception:
                logger.warning("Unable to interrupt active speech", exc_info=True)
        cancel = getattr(self.api_client, "cancel_current", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                logger.warning("Unable to cancel the active model response", exc_info=True)
        logger.info(
            "conversation_session=%s turn=%s event=response_cancel_dispatched",
            session_id,
            turn_number,
        )

    def _tts_is_speaking(self) -> bool:
        speaking = getattr(self.tts, "is_speaking", None)
        if speaking is None:
            # Compatibility with injected TTS implementations that predate the
            # interruptible playback contract.
            return True
        return bool(speaking() if callable(speaking) else speaking)

    def _tts_echo_epoch_start(self, now: float) -> float | None:
        active_started_at = getattr(self.tts, "active_playback_started_at", None)
        if callable(active_started_at):
            active_started_at = active_started_at()
        if isinstance(active_started_at, (int, float)) and not isinstance(
            active_started_at,
            bool,
        ):
            return float(active_started_at)

        playback_window = getattr(self.tts, "last_playback_window", None)
        if callable(playback_window):
            playback_window = playback_window()
        if not isinstance(playback_window, tuple) or len(playback_window) != 2:
            return None
        started_at, ended_at = playback_window
        if (
            not isinstance(started_at, (int, float))
            or isinstance(started_at, bool)
            or not isinstance(ended_at, (int, float))
            or isinstance(ended_at, bool)
        ):
            return None
        if 0 <= now - float(ended_at) <= _TTS_ECHO_TAIL_SECONDS:
            return float(started_at)
        return None

    @staticmethod
    def _normalized_echo_text(value: object) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(re.findall(r"\w+", value.casefold(), flags=re.UNICODE))

    def _matches_current_tts_echo(self, text: str, now: float) -> bool:
        """Return whether recognizer text is contained in current/recent TTS."""

        playback_text = getattr(self.tts, "active_playback_text", None)
        if callable(playback_text):
            playback_text = playback_text()
        if not playback_text and self._tts_echo_epoch_start(now) is not None:
            playback_text = getattr(self.tts, "last_playback_text", None)
            if callable(playback_text):
                playback_text = playback_text()
        candidate = self._normalized_echo_text(text)
        reference = self._normalized_echo_text(playback_text)
        if not candidate or not reference:
            return False
        padded_candidate = f" {candidate} "
        padded_reference = f" {reference} "
        return padded_candidate in padded_reference or padded_reference in padded_candidate

    def _listen_for_barge_in(
        self,
        response_future: Future[Any],
        *,
        cancellation: CancellationController | None = None,
    ) -> str | None:
        detector = self._barge_in_detector
        listen_events = getattr(self.speech_recognizer, "listen_events", None)
        if detector is None or not callable(listen_events):
            return None

        capture_stop = _BargeInCaptureStop(
            clock=self._clock,
            follow_up_timeout_seconds=self.settings.listen_timeout,
        )
        with self._capture_lock:
            self._active_capture_stop = capture_stop
        add_done_callback = getattr(response_future, "add_done_callback", None)
        if callable(add_done_callback):
            add_done_callback(lambda _future: capture_stop.response_finished())
        if response_future.done():
            capture_stop.capture_finished()
            with self._capture_lock:
                if self._active_capture_stop is capture_stop:
                    self._active_capture_stop = None
            return None

        detector.reset()
        try:
            return self._capture_barge_in(
                response_future,
                detector=detector,
                listen_events=listen_events,
                capture_stop=capture_stop,
                cancellation=cancellation,
            )
        finally:
            capture_stop.capture_finished()
            with self._capture_lock:
                if self._active_capture_stop is capture_stop:
                    self._active_capture_stop = None

    def _capture_barge_in(
        self,
        response_future: Future[Any],
        *,
        detector: Any,
        listen_events: Callable[..., Any],
        capture_stop: _BargeInCaptureStop,
        cancellation: CancellationController | None,
    ) -> str | None:
        playback_started_at: float | None = None
        detected = False
        latest_text = ""
        while not response_future.done() or detected:
            if capture_stop.is_set():
                break
            continuous = True
            events: Any = None
            try:
                try:
                    events = listen_events(timeout=None, stop_event=capture_stop)
                except TypeError:
                    # Compatibility for injected recognizers that predate the
                    # continuous stop-event contract.
                    continuous = False
                    events = listen_events(
                        timeout=min(
                            self.settings.listen_timeout,
                            _BARGE_IN_LISTEN_SLICE_SECONDS,
                        )
                    )
                for raw_result in events:
                    if self._stop_requested or self._closed:
                        return None
                    if response_future.done() and not detected:
                        logger.debug(
                            "Discarding recognizer finalization after the response boundary"
                        )
                        return None
                    result = self._coerce_recognition_result(raw_result)
                    if not result.text:
                        continue
                    now = self._clock()
                    if self._matches_current_tts_echo(result.text, now):
                        logger.info(
                            "event=barge_in_candidate_suppressed reason=tts_text_match"
                        )
                        continue
                    latest_text = result.text
                    if not detected:
                        actual_started_at = self._tts_echo_epoch_start(now)
                        if actual_started_at is not None:
                            playback_started_at = actual_started_at
                        elif self._tts_is_speaking():
                            if playback_started_at is None:
                                playback_started_at = now
                        else:
                            playback_started_at = None
                        if playback_started_at is None:
                            # Final recognizer output during generation can be a
                            # real correction, but it can also be a delayed flush
                            # of the local backchannel ("One moment", etc.). Run
                            # it through the same production echo/energy policy as
                            # playback candidates. Treat this as the conservative
                            # start of an unknown playback epoch so an event with
                            # no measured frame energy cannot cancel the model.
                            accepted = result.is_final and detector.process_recognition(
                                result,
                                elapsed_since_tts_start=0.0,
                                frame_energy=result.frame_energy,
                            )
                            if accepted:
                                detected = True
                                session_id, turn_number = self._conversation_coordinates()
                                logger.info(
                                    "conversation_session=%s turn=%s "
                                    "event=barge_in_detected phase=pre_playback",
                                    session_id,
                                    turn_number,
                                )
                                capture_stop.barge_in_detected()
                                self._transition_voice_conversation(
                                    VoiceConversationState.BARGE_IN_DETECTED
                                )
                                logger.info(
                                    "User follow-up finalized before playback; "
                                    "interrupting the active response"
                                )
                                self._transition_voice_conversation(
                                    VoiceConversationState.CANCELLING
                                )
                                self._interrupt_current_response(cancellation)
                                self._transition_voice_conversation(
                                    VoiceConversationState.FOLLOW_UP_FINALIZED
                                )
                                next_session, next_turn = self._conversation_coordinates(
                                    next_turn=True
                                )
                                logger.info(
                                    "conversation_session=%s turn=%s event=stt_finalized "
                                    "source=barge_in",
                                    next_session,
                                    next_turn,
                                )
                                return result.text
                            continue
                        self._transition_voice_conversation(VoiceConversationState.SPEAKING)
                        elapsed = max(0.0, now - playback_started_at)
                        detected = detector.process_recognition(
                            result,
                            elapsed_since_tts_start=elapsed,
                            frame_energy=result.frame_energy,
                        )
                        if detected:
                            session_id, turn_number = self._conversation_coordinates()
                            logger.info(
                                "conversation_session=%s turn=%s event=barge_in_detected "
                                "phase=playback",
                                session_id,
                                turn_number,
                            )
                            capture_stop.barge_in_detected()
                            self._transition_voice_conversation(
                                VoiceConversationState.BARGE_IN_DETECTED
                            )
                            logger.info("Barge-in detected; interrupting the active response")
                            self._transition_voice_conversation(
                                VoiceConversationState.CANCELLING
                            )
                            self._interrupt_current_response(cancellation)
                            self._transition_voice_conversation(
                                VoiceConversationState.CAPTURING_FOLLOW_UP
                            )
                    else:
                        capture_stop.recognition_activity()
                    if detected and result.is_final:
                        self._transition_voice_conversation(
                            VoiceConversationState.FOLLOW_UP_FINALIZED
                        )
                        next_session, next_turn = self._conversation_coordinates(next_turn=True)
                        logger.info(
                            "conversation_session=%s turn=%s event=stt_finalized "
                            "source=barge_in",
                            next_session,
                            next_turn,
                        )
                        return result.text
            except Exception:
                self._interrupt_current_response(cancellation)
                raise
            finally:
                close_events = getattr(events, "close", None)
                if callable(close_events):
                    close_events()
            if detected:
                # Continuous Vosk capture flushes FinalResult when cancellation
                # stops the session. Retain a partial only for legacy injected
                # recognizers that cannot provide that finalization contract.
                return (latest_text or None) if not continuous else None
            if continuous:
                return None
        return None

    def _process_command_with_barge_in(
        self,
        model_prompt: str,
        *,
        pipeline_started_at: float,
    ) -> str | None:
        cancellation = CancellationController()

        def execute_initial() -> str | None:
            return self._process_model_prompt(
                model_prompt,
                pipeline_started_at=pipeline_started_at,
                cancellation=cancellation,
            )

        self._transition_voice_conversation(VoiceConversationState.GENERATING)
        self._set_active_response_cancellation(cancellation)
        response_future = self._submit_task(self._conversation_executor, execute_initial)
        while True:
            try:
                follow_up = self._listen_for_barge_in(
                    response_future,
                    cancellation=cancellation,
                )
            except Exception:
                self._interrupt_current_response(cancellation)
                try:
                    response_future.result(timeout=_RESPONSE_CANCEL_TIMEOUT_SECONDS)
                except FutureTimeoutError:
                    response_future.cancel()
                    logger.error("event=response_cancel_timeout scope=listener_failure")
                except Exception:
                    logger.debug(
                        "Response worker unwound after listener failure",
                        exc_info=True,
                    )
                raise
            if follow_up is None:
                try:
                    result = response_future.result()
                    self._transition_voice_conversation(VoiceConversationState.LISTENING)
                    return result
                finally:
                    self._set_active_response_cancellation(None)

            # Cancellation is an expected terminal outcome after barge-in. Wait
            # for the worker to unwind so its active token and speech queue are
            # cleared before starting the next turn on the same executor.
            try:
                response_future.result(timeout=_RESPONSE_CANCEL_TIMEOUT_SECONDS)
            except FutureTimeoutError:
                response_future.cancel()
                logger.error("event=response_cancel_timeout scope=barge_in")
                raise AssistantRuntimeError(
                    "Interrupted response did not stop before the cancellation deadline"
                ) from None
            except Exception:
                logger.debug("Interrupted response finished with cancellation", exc_info=True)
            finally:
                self._set_active_response_cancellation(None)

            model_prompt = follow_up.strip()
            if self.contains_wake_word(model_prompt):
                model_prompt = self._without_wake_word(model_prompt)
            if not model_prompt:
                return None
            if self._stop_requested or self._closed:
                logger.info("event=follow_up_discarded reason=assistant_stopping")
                return None

            follow_up_started_at = self._clock()
            cancellation = CancellationController()

            def execute_follow_up(prompt: str = model_prompt) -> str | None:
                return self._process_model_prompt(
                    prompt,
                    pipeline_started_at=follow_up_started_at,
                    cancellation=cancellation,
                )

            self._transition_voice_conversation(VoiceConversationState.GENERATING)
            self._set_active_response_cancellation(cancellation)
            response_future = self._submit_task(
                self._conversation_executor,
                execute_follow_up,
            )

    def run_once(self) -> bool:
        """Process at most one finalized utterance.

        Returns ``True`` when an utterance caused a state transition or command
        execution, and ``False`` for a timeout/partial-only result.
        """

        if self._stop_requested or self._closed:
            return False
        result = self._recognize_once()
        if result is None or not result.text or not result.is_final:
            return False
        command = result.text
        finalized_at = self._clock()
        session_id, turn_number = self._conversation_coordinates(next_turn=True)
        logger.info(
            "conversation_session=%s turn=%s event=stt_finalized source=primary_listener",
            session_id,
            turn_number,
        )

        if self.state is AssistantState.COMMAND:
            if self._contains_rag_word(command):
                self.play_sound_async(str(self.settings.wake_sound))
                self.prepare_rag_async()
                self.state = AssistantState.RAG
                logger.info("Entering RAG mode")
                record_safely(
                    self.metrics,
                    "voice_command_completed",
                    mode="rag",
                    outcome="rag_mode_entered",
                    success=True,
                    recognized_count=1,
                    end_to_end_ms=(self._clock() - finalized_at) * 1_000,
                )
                return True
            has_wake_word = self.contains_wake_word(command)
            is_follow_up = (
                self.settings.barge_in_enabled
                and not has_wake_word
                and self._voice_conversation_is_active()
            )
            if has_wake_word or is_follow_up:
                model_prompt = self._without_wake_word(command) if has_wake_word else command.strip()
                if not model_prompt:
                    return False
                selected_mode = "think" if self._think_prompt(model_prompt) is not None else "talk"
                if has_wake_word:
                    self._activate_voice_conversation()
                    record_safely(
                        self.metrics,
                        "wake_word_detected",
                        mode=selected_mode,
                        outcome="detected",
                        success=True,
                        wake_word_count=1,
                    )
                else:
                    self._touch_voice_conversation()
                    logger.info("event=voice_follow_up_accepted wake_word_required=false")
                self._transition_voice_conversation(VoiceConversationState.USER_TURN_FINALIZED)
                try:
                    if self.settings.barge_in_enabled:
                        self._process_command_with_barge_in(
                            model_prompt,
                            pipeline_started_at=finalized_at,
                        )
                    else:
                        self._process_model_prompt(
                            model_prompt,
                            pipeline_started_at=finalized_at,
                        )
                except Exception:
                    record_safely(
                        self.metrics,
                        "voice_command_failed",
                        mode=selected_mode,
                        outcome="failed",
                        success=False,
                        recognized_count=1,
                        end_to_end_ms=(self._clock() - finalized_at) * 1_000,
                    )
                    self._transition_voice_conversation(VoiceConversationState.LISTENING)
                    raise
                self._touch_voice_conversation()
                self._transition_voice_conversation(VoiceConversationState.LISTENING)
                record_safely(
                    self.metrics,
                    "voice_command_completed",
                    mode=selected_mode,
                    outcome="succeeded",
                    success=True,
                    recognized_count=1,
                    end_to_end_ms=(self._clock() - finalized_at) * 1_000,
                )
                return True
            return False

        if self.state is AssistantState.RAG:
            try:
                self.process_rag_command(command)
                record_safely(
                    self.metrics,
                    "voice_command_completed",
                    mode="rag",
                    outcome="succeeded",
                    success=True,
                    recognized_count=1,
                    end_to_end_ms=(self._clock() - finalized_at) * 1_000,
                )
            except Exception:
                record_safely(
                    self.metrics,
                    "voice_command_failed",
                    mode="rag",
                    outcome="failed",
                    success=False,
                    recognized_count=1,
                    end_to_end_ms=(self._clock() - finalized_at) * 1_000,
                )
                raise
            finally:
                self.state = AssistantState.COMMAND
                self.play_sound_async(str(self.settings.stop_sound))
                logger.info("Returning to command mode")
            return True

        return False

    def run(self, *, max_iterations: int | None = None) -> None:
        if max_iterations is not None and max_iterations < 0:
            raise ValueError("max_iterations cannot be negative")
        if self._closed:
            raise AssistantRuntimeError("Voice assistant is closed")

        logger.info("Starting voice assistant")
        record_safely(
            self.metrics,
            "assistant_started",
            outcome="started",
            success=True,
        )
        self._stop_requested = False
        self._running = True
        iterations = 0
        recoverable_errors = (
            APIClientError,
            TTSError,
            SoundPlaybackError,
            SpeechRecognitionError,
            AssistantRuntimeError,
        )
        try:
            prepare_remote = getattr(self.api_client, "prepare_remote_async", None)
            if callable(prepare_remote):
                prepare_remote()
            prepare_recognizer = getattr(
                self.speech_recognizer,
                "prepare_async",
                None,
            )
            if callable(prepare_recognizer):
                prepare_recognizer()
            self._speak_observed(
                self.profile.welcome_message.format(wake_word=self.profile.wake_word),
                scope="welcome",
            )
            backchannel_preparation = self.prepare_backchannels_async()
            if backchannel_preparation is not None:
                # The latency-sensitive turn path must only ever use cached
                # waves. Finish the one-time startup work before accepting a
                # first command so the first slow answer receives the same cue
                # behavior as every later one.
                backchannel_preparation.result()
            while self._running and (max_iterations is None or iterations < max_iterations):
                iterations += 1
                try:
                    self.run_once()
                except recoverable_errors:
                    logger.exception("Recoverable runtime error")
                    self.state = AssistantState.COMMAND
                    self._sleep(1.0)
        except KeyboardInterrupt:
            logger.info("Voice assistant interrupted")
            record_safely(
                self.metrics,
                "voice_interrupted",
                outcome="cancelled",
                success=False,
                interruption_count=1,
            )
        finally:
            self._running = False
            record_safely(
                self.metrics,
                "assistant_stopped",
                outcome="stopped",
                success=True,
            )
            self.close()

    def stop(self) -> None:
        self._stop_requested = True
        self._running = False
        with self._capture_lock:
            capture_stop = self._active_capture_stop
        if capture_stop is not None:
            capture_stop.force_stop()
        self._interrupt_current_response()
        self._deactivate_voice_conversation()
        record_safely(
            self.metrics,
            "voice_cancelled",
            outcome="cancelled",
            success=False,
            cancellation_count=1,
        )

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._stop_requested = True
            self._running = False
        self._deactivate_voice_conversation()

        with self._capture_lock:
            capture_stop = self._active_capture_stop
        if capture_stop is not None:
            capture_stop.force_stop()

        self._interrupt_current_response()

        with self._backchannel_lock:
            backchannel = self._last_backchannel_session
        if backchannel is not None:
            backchannel.supersede()

        with self._task_lock:
            tasks = tuple(self._tasks)
        for future in tasks:
            future.cancel()
        completed, pending = wait(tasks, timeout=_TASK_SHUTDOWN_TIMEOUT_SECONDS)
        for future in completed:
            try:
                future.result()
            except Exception:
                # Cancellation and response failures are expected during
                # teardown; all contentful errors were already observed by the
                # owning runtime path.
                pass
        if pending:
            logger.warning(
                "event=assistant_task_shutdown_timeout pending_tasks=%s",
                len(pending),
            )

        if capture_stop is not None:
            # Do not terminate PyAudio while listen_events() may still be in a
            # device read or generator cleanup. The stop token makes the next
            # bounded frame read exit; this wait is the teardown barrier.
            if not capture_stop.wait_finished(_TASK_SHUTDOWN_TIMEOUT_SECONDS):
                logger.warning("event=capture_shutdown_timeout")

        if self._owns_conversation_executor:
            self._conversation_executor.shutdown(
                wait=not pending,
                cancel_futures=True,
            )
        if self._owns_sound_executor:
            self._sound_executor.shutdown(
                wait=not pending,
                cancel_futures=True,
            )

        closed_ids: set[int] = set()
        for service in (
            self.speech_recognizer,
            self.api_client,
            self.tts,
            self.sound_player,
            self._rag_searcher,
        ):
            if service is None or id(service) in closed_ids:
                continue
            closed_ids.add(id(service))
            close = getattr(service, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.warning(
                        "Unable to close %s",
                        type(service).__name__,
                        exc_info=True,
                    )

    def __enter__(self) -> VoiceAssistant:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
