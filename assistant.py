"""Runtime orchestration for the voice assistant."""

from __future__ import annotations

import inspect
import logging
import math
import random
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError, wait
from dataclasses import replace
from difflib import SequenceMatcher
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
_BARGE_IN_CANDIDATE_INACTIVITY_SECONDS = 1.5
_TTS_ECHO_TAIL_SECONDS = 0.25
_TASK_SHUTDOWN_TIMEOUT_SECONDS = 2.0
_RESPONSE_CANCEL_TIMEOUT_SECONDS = 2.5
_EXPLICIT_SHORT_INTERRUPT_ENERGY = 0.12
_EXPLICIT_SHORT_INTERRUPT_CONFIDENCE = 0.65
_EXPLICIT_SHORT_INTERRUPT_COMMANDS = frozenset(
    {
        "basta",
        "cancella",
        "fermati",
        "interrompi",
        "silenzio",
        "stop",
        "cancel",
        "enough",
        "pause",
    }
)


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
        self._candidate_activity_at: float | None = None
        self._candidate_timeout_expired = False
        self._forced = False
        self._finished = threading.Event()

    def response_finished(self) -> None:
        with self._lock:
            self._response_finished = True

    def barge_in_detected(self, observed_at: float | None = None) -> None:
        with self._lock:
            if self._detected_at is None:
                self._detected_at = self._clock() if observed_at is None else observed_at

    def recognition_activity(self) -> None:
        """Refresh the post-detection inactivity deadline."""

        with self._lock:
            if self._detected_at is not None:
                self._detected_at = self._clock()

    def candidate_activity(self, observed_at: float | None = None) -> None:
        """Start or refresh the provisional-candidate inactivity deadline."""

        with self._lock:
            self._candidate_activity_at = self._clock() if observed_at is None else observed_at
            self._candidate_timeout_expired = False

    def candidate_finished(self) -> None:
        """Clear provisional capture state after rejection or finalization."""

        with self._lock:
            self._candidate_activity_at = None

    def consume_candidate_timeout(self) -> bool:
        """Consume a provisional timeout so capture can restart cleanly."""

        with self._lock:
            expired = self._candidate_timeout_expired
            self._candidate_timeout_expired = False
            if expired:
                self._candidate_activity_at = None
            return expired

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
            candidate_activity_at = self._candidate_activity_at
            response_finished = self._response_finished
            forced = self._forced
        if forced:
            return True
        if detected_at is None:
            candidate_expired = bool(
                candidate_activity_at is not None
                and self._clock() - candidate_activity_at >= _BARGE_IN_CANDIDATE_INACTIVITY_SECONDS
            )
            if candidate_expired:
                with self._lock:
                    self._candidate_timeout_expired = True
            return response_finished or candidate_expired
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


class AssistantShutdownTimeout(AssistantRuntimeError):
    """Raised when an owned non-daemon worker cannot stop before process exit."""


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
        self._preparation_stop = threading.Event()
        self._last_backchannel_session: BackchannelSession | None = None
        self._capture_lock = threading.Lock()
        self._active_capture_stop: _BargeInCaptureStop | None = None
        self._response_control_lock = threading.Lock()
        self._active_response_cancellation: CancellationController | None = None
        self._close_lock = threading.Lock()
        self._close_complete = threading.Event()
        self._closing = False
        self._task_lock = threading.Lock()
        self._tasks: set[Future[Any]] = set()
        self._owned_tasks: set[Future[Any]] = set()
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
                and self._clock() - last_activity >= self.settings.llm.context_idle_timeout_seconds
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
            self._owned_tasks.discard(future)

    def _track_task(
        self,
        future: Future[Any],
        *,
        owned: bool = False,
    ) -> Future[Any]:
        with self._task_lock:
            self._tasks.add(future)
            if owned:
                self._owned_tasks.add(future)
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
            owned = bool(
                (executor is self._conversation_executor and self._owns_conversation_executor)
                or (executor is self._sound_executor and self._owns_sound_executor)
            )
            return self._track_task(future, owned=owned)

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
            self._track_task(
                session.future,
                owned=self._owns_conversation_executor,
            )
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
            preload_kwargs: dict[str, Any] = {}
            if self._accepts_keyword(preload, "stop_event"):
                preload_kwargs["stop_event"] = self._preparation_stop
            loaded = preload(self.profile.backchannel_phrases, **preload_kwargs)
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
                self._backchannel_prepare_future = self._track_task(
                    future,
                    owned=self._owns_conversation_executor,
                )
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
        segment_id = getattr(result, "segment_id", None)
        if not isinstance(segment_id, int) or isinstance(segment_id, bool):
            segment_id = None
        segment_started_at = getattr(result, "segment_started_at", None)
        if not isinstance(segment_started_at, (int, float)) or isinstance(segment_started_at, bool):
            segment_started_at = None
        confidence = getattr(result, "confidence", None)
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
        ):
            confidence = None
        speech_duration_seconds = getattr(result, "speech_duration_seconds", None)
        if (
            not isinstance(speech_duration_seconds, (int, float))
            or isinstance(speech_duration_seconds, bool)
            or not math.isfinite(float(speech_duration_seconds))
            or float(speech_duration_seconds) < 0
        ):
            speech_duration_seconds = None
        segment_peak_energy = getattr(result, "segment_peak_energy", None)
        if (
            not isinstance(segment_peak_energy, (int, float))
            or isinstance(segment_peak_energy, bool)
            or not math.isfinite(float(segment_peak_energy))
            or not 0 <= float(segment_peak_energy) <= 1
        ):
            segment_peak_energy = None
        raw_word_confidences = getattr(result, "word_confidences", ())
        word_confidences: tuple[float | None, ...] = ()
        if isinstance(raw_word_confidences, (tuple, list)):
            normalized_confidences: list[float | None] = []
            for value in raw_word_confidences:
                if value is None:
                    normalized_confidences.append(None)
                elif (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    and 0 <= float(value) <= 1
                ):
                    normalized_confidences.append(float(value))
            word_confidences = tuple(normalized_confidences)
        raw_word_timings = getattr(result, "word_timings", ())
        word_timings: tuple[tuple[float, float] | None, ...] = ()
        if isinstance(raw_word_timings, (tuple, list)):
            normalized_timings: list[tuple[float, float] | None] = []
            for value in raw_word_timings:
                if value is None:
                    normalized_timings.append(None)
                elif (
                    isinstance(value, (tuple, list))
                    and len(value) == 2
                    and all(
                        isinstance(item, (int, float))
                        and not isinstance(item, bool)
                        and math.isfinite(float(item))
                        for item in value
                    )
                    and 0 <= float(value[0]) <= float(value[1])
                ):
                    normalized_timings.append((float(value[0]), float(value[1])))
            word_timings = tuple(normalized_timings)
        return RecognitionResult(
            str(getattr(result, "text", "")).strip(),
            bool(getattr(result, "is_final", True)),
            frame_energy=(float(frame_energy) if frame_energy is not None else None),
            segment_id=segment_id,
            segment_started_at=(
                float(segment_started_at) if segment_started_at is not None else None
            ),
            energy_reemit=bool(getattr(result, "energy_reemit", False)),
            confidence=(float(confidence) if confidence is not None else None),
            speech_duration_seconds=(
                float(speech_duration_seconds) if speech_duration_seconds is not None else None
            ),
            segment_peak_energy=(
                float(segment_peak_energy) if segment_peak_energy is not None else None
            ),
            word_confidences=word_confidences,
            word_timings=word_timings,
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

    def _current_tts_echo_references(self, now: float) -> tuple[str, ...]:
        if self._tts_echo_epoch_start(now) is None:
            return ()
        references: list[str] = []
        for attribute in ("active_playback_text", "last_playback_text"):
            playback_text = getattr(self.tts, attribute, None)
            if callable(playback_text):
                playback_text = playback_text()
            normalized = self._normalized_echo_text(playback_text)
            if normalized and normalized not in references:
                references.append(normalized)
        return tuple(references)

    def _matches_current_tts_echo(
        self,
        text: str,
        now: float,
        *,
        references: tuple[str, ...] = (),
    ) -> bool:
        """Return whether recognizer text resembles current/recent TTS."""

        candidate = self._normalized_echo_text(text)
        if not candidate:
            return False
        current = self._current_tts_echo_references(now)
        all_references = tuple(dict.fromkeys((*references, *current)))
        for reference in all_references:
            if self._echo_candidate_matches_reference(candidate, reference):
                return True
        return False

    @staticmethod
    def _echo_candidate_matches_reference(candidate: str, reference: str) -> bool:
        """Compare one normalized recognition hypothesis with one TTS phrase."""

        padded_candidate = f" {candidate} "
        padded_reference = f" {reference} "
        if padded_candidate in padded_reference or padded_reference in padded_candidate:
            return True
        candidate_words = candidate.split()
        reference_words = reference.split()
        if len(candidate_words) < 3 or len(reference_words) < 3:
            return False
        if len(reference) >= len(candidate):
            character_windows = (
                reference[position : position + len(candidate)]
                for position in range(len(reference) - len(candidate) + 1)
            )
        else:
            character_windows = (reference,)
        character_similarity = max(
            SequenceMatcher(None, candidate, window, autojunk=False).ratio()
            for window in character_windows
        )
        # Vosk often changes several word boundaries while preserving most
        # phonetic characters (for example ``Marte`` -> ``parte``).
        if character_similarity >= 0.78:
            return True
        if len(reference_words) >= len(candidate_words):
            windows = (
                reference_words[position : position + len(candidate_words)]
                for position in range(len(reference_words) - len(candidate_words) + 1)
            )
        else:
            windows = (reference_words,)
        word_similarity = max(
            SequenceMatcher(None, candidate_words, window, autojunk=False).ratio()
            for window in windows
        )
        return word_similarity >= 0.65 and character_similarity >= 0.72

    def _remove_current_tts_echo(
        self,
        text: str,
        now: float,
        *,
        references: tuple[str, ...] = (),
    ) -> tuple[str, tuple[int, ...]]:
        """Return only words not attributable to current/recent TTS.

        A Vosk segment can start with loudspeaker output and end with the user's
        correction. Treating that whole segment as echo loses the correction;
        forwarding it whole contaminates the next model prompt with the old
        answer. This method removes an exact or strongly aligned echo prefix
        and reports how many words were removed without logging transcript text.
        """

        candidate = self._normalized_echo_text(text)
        if not candidate:
            return "", ()
        candidate_words = candidate.split()
        current = self._current_tts_echo_references(now)
        all_references = tuple(dict.fromkeys((*references, *current)))
        # Echo can span multiple streamed Piper fragments. Consume only one
        # contiguous prefix region, repeatedly searching the bounded reference
        # set. Never delete a matching phrase from the middle of a user's new
        # request: they may be quoting or referring to the previous answer.
        prefix_cursor = 0
        while prefix_cursor < len(candidate_words):
            remaining_words = candidate_words[prefix_cursor:]
            matched_width = 0

            # Prefer exact alignment across every reference before fuzzy
            # matching; otherwise a similar earlier fragment could consume the
            # wrong number of words ahead of a later exact fragment.
            for reference in all_references:
                reference_words = reference.split()
                if not reference_words:
                    continue
                width = len(reference_words)
                if tuple(remaining_words[:width]) == tuple(reference_words):
                    matched_width = width
                    break

            if not matched_width:
                for reference in all_references:
                    reference_words = reference.split()
                    if not reference_words:
                        continue

                    # For Vosk spelling/word-boundary drift, consume only an
                    # aligned prefix against the start of this fragment. The
                    # general echo matcher searches arbitrary reference
                    # windows, which is useful for suppression but unsafe for
                    # deleting words from a user prompt.
                    max_prefix = min(len(remaining_words), len(reference_words))
                    for prefix_size in range(max_prefix, 2, -1):
                        candidate_prefix_words = remaining_words[:prefix_size]
                        reference_prefix_words = reference_words[:prefix_size]
                        candidate_prefix = " ".join(candidate_prefix_words)
                        reference_prefix = " ".join(reference_prefix_words)
                        character_similarity = SequenceMatcher(
                            None,
                            candidate_prefix,
                            reference_prefix,
                            autojunk=False,
                        ).ratio()
                        word_similarity = SequenceMatcher(
                            None,
                            candidate_prefix_words,
                            reference_prefix_words,
                            autojunk=False,
                        ).ratio()
                        if character_similarity >= 0.78 or (
                            word_similarity >= 0.65 and character_similarity >= 0.72
                        ):
                            matched_width = prefix_size
                            break
                    if matched_width:
                        break

            if not matched_width:
                break
            prefix_cursor += matched_width

        removed_indices = tuple(range(prefix_cursor))
        return " ".join(candidate_words[prefix_cursor:]), removed_indices

    @staticmethod
    def _is_short_unconfirmed_final(
        result: RecognitionResult,
        detector: Any,
    ) -> bool:
        """Treat a short final as unsafe unless its partial was already armed."""

        if not result.is_final or len(result.text.split()) >= 3:
            return False
        normalized = VoiceAssistant._normalized_echo_text(result.text)
        peak_energy = (
            result.segment_peak_energy
            if result.segment_peak_energy is not None
            else result.frame_energy
        )
        if (
            normalized in _EXPLICIT_SHORT_INTERRUPT_COMMANDS
            and peak_energy is not None
            and peak_energy >= _EXPLICIT_SHORT_INTERRUPT_ENERGY
            and result.confidence is not None
            and result.confidence >= _EXPLICIT_SHORT_INTERRUPT_CONFIDENCE
        ):
            return False
        if not bool(getattr(detector, "recognition_candidate_pending", False)):
            return True
        pending_segment_id = getattr(detector, "recognition_candidate_segment_id", None)
        return (
            result.segment_id is not None
            and pending_segment_id is not None
            and result.segment_id != pending_segment_id
        )

    @staticmethod
    def _is_confirmed_explicit_interrupt(result: RecognitionResult) -> bool:
        if not result.is_final:
            return False
        peak_energy = (
            result.segment_peak_energy
            if result.segment_peak_energy is not None
            else result.frame_energy
        )
        return bool(
            VoiceAssistant._normalized_echo_text(result.text) in _EXPLICIT_SHORT_INTERRUPT_COMMANDS
            and peak_energy is not None
            and peak_energy >= _EXPLICIT_SHORT_INTERRUPT_ENERGY
            and result.confidence is not None
            and result.confidence >= _EXPLICIT_SHORT_INTERRUPT_CONFIDENCE
        )

    @staticmethod
    def _has_strong_vosk_final(result: RecognitionResult) -> bool:
        """Return whether Vosk supplied enough metadata to trust a final alone."""

        peak_energy = (
            result.segment_peak_energy
            if result.segment_peak_energy is not None
            else result.frame_energy
        )
        return bool(
            result.is_final
            and len(result.text.split()) >= 3
            and result.confidence is not None
            and result.confidence >= 0.65
            and result.speech_duration_seconds is not None
            and result.speech_duration_seconds >= 0.2
            and peak_energy is not None
            and peak_energy >= _EXPLICIT_SHORT_INTERRUPT_ENERGY
        )

    def _duck_tts_for_barge_in_candidate(self, segment_id: int | None) -> None:
        """Reversibly pause response audio while Vosk finalizes a candidate."""

        duck = getattr(self.tts, "duck", None)
        if not callable(duck):
            # Legacy/injected TTS implementations cannot provide a reversible
            # pause. Do not fall back to interrupt(): that would truncate a
            # response for a provisional hypothesis and cannot be undone.
            return
        try:
            duck()
            logger.info("event=barge_in_tts_ducked segment=%s", segment_id)
        except Exception:
            logger.warning(
                "Unable to duck speech for a barge-in candidate",
                exc_info=True,
            )

    def _resume_tts_after_barge_in_candidate(self) -> None:
        """Release a reversible TTS duck after rejection or capture teardown."""

        resume = getattr(self.tts, "resume", None)
        if not callable(resume):
            return
        try:
            if resume():
                logger.info("event=barge_in_tts_resumed reason=candidate_not_committed")
        except Exception:
            logger.warning(
                "Unable to resume speech after a barge-in candidate",
                exc_info=True,
            )

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
            self._resume_tts_after_barge_in_candidate()
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
        echo_references: list[str] = []
        detector_playback_epoch: float | None = None
        while not response_future.done() or bool(
            getattr(detector, "recognition_candidate_pending", False)
        ):
            candidate_pending = bool(getattr(detector, "recognition_candidate_pending", False))
            if capture_stop.is_set() and not candidate_pending:
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
                    result = self._coerce_recognition_result(raw_result)
                    if not result.text:
                        continue
                    candidate_pending = bool(
                        getattr(detector, "recognition_candidate_pending", False)
                    )
                    if (
                        response_future.done()
                        and not candidate_pending
                        and not self._has_strong_vosk_final(result)
                        and not self._is_confirmed_explicit_interrupt(result)
                    ):
                        logger.debug(
                            "Discarding recognizer finalization after the response boundary"
                        )
                        return None
                    now = self._clock()
                    actual_started_at = self._tts_echo_epoch_start(now)
                    for reference in self._current_tts_echo_references(now):
                        if reference not in echo_references:
                            echo_references.append(reference)

                    residual, removed_echo_indices = self._remove_current_tts_echo(
                        result.text,
                        now,
                        references=tuple(echo_references),
                    )
                    echo_word_count = len(removed_echo_indices)
                    if removed_echo_indices and not residual:
                        discard_candidate = getattr(detector, "discard_recognition_candidate", None)
                        if callable(discard_candidate):
                            discard_candidate()
                        capture_stop.candidate_finished()
                        self._resume_tts_after_barge_in_candidate()
                        logger.info("event=barge_in_candidate_suppressed reason=tts_text_match")
                        continue
                    if removed_echo_indices:
                        candidate_word_count = len(self._normalized_echo_text(result.text).split())
                        kept_indices = tuple(
                            index
                            for index in range(candidate_word_count)
                            if index not in frozenset(removed_echo_indices)
                        )
                        residual_confidences = tuple(
                            result.word_confidences[index]
                            for index in kept_indices
                            if index < len(result.word_confidences)
                        )
                        known_confidences = tuple(
                            value for value in residual_confidences if value is not None
                        )
                        residual_timings = tuple(
                            result.word_timings[index]
                            for index in kept_indices
                            if index < len(result.word_timings)
                        )
                        known_timings = tuple(
                            value for value in residual_timings if value is not None
                        )
                        has_word_metadata = bool(result.word_confidences or result.word_timings)
                        residual_metadata_complete = bool(
                            result.word_confidences
                            and len(residual_confidences) == len(kept_indices)
                            and len(known_confidences) == len(kept_indices)
                        )
                        result = replace(
                            result,
                            text=residual,
                            confidence=(
                                sum(known_confidences) / len(known_confidences)
                                if residual_metadata_complete
                                else (0.0 if has_word_metadata else result.confidence)
                            ),
                            speech_duration_seconds=(
                                max(end for _, end in known_timings)
                                - min(start for start, _ in known_timings)
                                if known_timings
                                else (None if has_word_metadata else result.speech_duration_seconds)
                            ),
                            # Whole-segment RMS includes the loudspeaker. After
                            # echo removal only per-word ASR evidence remains
                            # trustworthy; do not let speaker energy validate
                            # the residual as user speech.
                            segment_peak_energy=(
                                result.segment_peak_energy
                                if not has_word_metadata
                                else (result.frame_energy if residual_metadata_complete else 0.0)
                            ),
                            word_confidences=residual_confidences,
                            word_timings=residual_timings,
                        )
                        logger.info(
                            "event=barge_in_echo_prefix_removed echo_words=%s residual_words=%s",
                            echo_word_count,
                            len(result.text.split()),
                        )

                    pending_segment_id = getattr(detector, "recognition_candidate_segment_id", None)
                    same_pending_segment = bool(
                        getattr(detector, "recognition_candidate_pending", False)
                    ) and (
                        result.segment_id is None
                        or pending_segment_id is None
                        or result.segment_id == pending_segment_id
                    )
                    epoch_changed = actual_started_at != detector_playback_epoch
                    carry_pending_across_epoch = bool(
                        epoch_changed
                        and same_pending_segment
                        and result.segment_started_at is not None
                        and (
                            detector_playback_epoch is None
                            or result.segment_started_at >= detector_playback_epoch
                        )
                        and not result.energy_reemit
                    )
                    if epoch_changed:
                        if not carry_pending_across_epoch:
                            discard_candidate = getattr(
                                detector, "discard_recognition_candidate", None
                            )
                            if callable(discard_candidate):
                                discard_candidate()
                            capture_stop.candidate_finished()
                            self._resume_tts_after_barge_in_candidate()
                        detector_playback_epoch = actual_started_at

                    crossed_playback_boundary = (
                        actual_started_at is not None
                        and result.segment_started_at is not None
                        and result.segment_started_at < actual_started_at
                    )
                    if result.energy_reemit:
                        # Vosk can re-emit an unchanged hypothesis solely
                        # because the latest PCM frame became louder. At TTS
                        # onset that energy is the loudspeaker, not independent
                        # evidence that the user started speaking.
                        logger.info("event=barge_in_candidate_suppressed reason=energy_only_reemit")
                        continue
                    if crossed_playback_boundary and not carry_pending_across_epoch:
                        # A hypothesis first observed only after its segment
                        # crossed into loudspeaker playback has no independent
                        # pre-playback evidence. Re-evaluate every revision so a
                        # later cleaned user segment is still eligible, but do
                        # not arm this stale segment from speaker energy.
                        logger.info(
                            "event=barge_in_candidate_suppressed reason=pre_playback_segment"
                        )
                        continue
                    if self._is_short_unconfirmed_final(result, detector):
                        # A first-event one/two-word final during playback is a
                        # common Vosk rendering of a short TTS/backchannel echo.
                        # Require an earlier high-energy partial from the same
                        # segment; otherwise keep speaking.
                        discard_candidate = getattr(
                            detector,
                            "discard_recognition_candidate",
                            None,
                        )
                        if callable(discard_candidate):
                            discard_candidate()
                        capture_stop.candidate_finished()
                        self._resume_tts_after_barge_in_candidate()
                        logger.info(
                            "event=barge_in_candidate_suppressed reason=short_unconfirmed_final"
                        )
                        continue

                    if actual_started_at is not None:
                        playback_started_at = actual_started_at
                    elif self._tts_is_speaking():
                        if playback_started_at is None:
                            playback_started_at = now
                    else:
                        playback_started_at = None
                    if playback_started_at is not None:
                        self._transition_voice_conversation(VoiceConversationState.SPEAKING)
                    elapsed = (
                        max(0.0, now - playback_started_at)
                        if playback_started_at is not None
                        else 0.0
                    )
                    acoustic_energy = (
                        result.segment_peak_energy
                        if result.segment_peak_energy is not None
                        else result.frame_energy
                    )
                    process_recognition = detector.process_recognition
                    detector_kwargs: dict[str, Any] = {
                        "elapsed_since_tts_start": elapsed,
                        "frame_energy": acoustic_energy,
                        "observed_at": now,
                    }
                    if (
                        removed_echo_indices
                        and not result.is_final
                        and result.confidence is not None
                        and result.confidence >= _EXPLICIT_SHORT_INTERRUPT_CONFIDENCE
                        and self._accepts_keyword(process_recognition, "allow_short_partial")
                    ):
                        # Echo stripping may leave only the first two words of
                        # the actual correction. The residual still cannot
                        # cancel anything: it only arms a same-segment final.
                        detector_kwargs["allow_short_partial"] = True
                    allow_strong_completed_final = bool(
                        response_future.done() and self._has_strong_vosk_final(result)
                    )
                    if (
                        self._is_confirmed_explicit_interrupt(result)
                        or allow_strong_completed_final
                    ) and self._accepts_keyword(process_recognition, "allow_unarmed_final"):
                        detector_kwargs["allow_unarmed_final"] = True
                    was_pending = bool(getattr(detector, "recognition_candidate_pending", False))
                    accepted = bool(process_recognition(result, **detector_kwargs))
                    is_pending = bool(getattr(detector, "recognition_candidate_pending", False))
                    logger.debug(
                        "event=barge_in_stt_decision segment=%s final=%s words=%s "
                        "confidence=%s duration_ms=%s frame_rms=%s peak_rms=%s "
                        "pending=%s accepted=%s crossed_playback=%s",
                        result.segment_id,
                        result.is_final,
                        len(result.text.split()),
                        (round(result.confidence, 3) if result.confidence is not None else None),
                        (
                            round(result.speech_duration_seconds * 1_000, 1)
                            if result.speech_duration_seconds is not None
                            else None
                        ),
                        (
                            round(result.frame_energy, 3)
                            if result.frame_energy is not None
                            else None
                        ),
                        (
                            round(result.segment_peak_energy, 3)
                            if result.segment_peak_energy is not None
                            else None
                        ),
                        is_pending,
                        accepted,
                        crossed_playback_boundary,
                    )
                    if not result.is_final:
                        if is_pending and not was_pending:
                            logger.info(
                                "event=barge_in_candidate_armed segment=%s words=%s",
                                result.segment_id,
                                len(result.text.split()),
                            )
                            # Pause only the loudspeaker. The model turn and
                            # canonical history remain untouched until a final
                            # confirms this segment.
                            self._duck_tts_for_barge_in_candidate(result.segment_id)
                            capture_stop.candidate_activity(now)
                        elif is_pending:
                            capture_stop.candidate_activity(now)
                        elif was_pending and not is_pending:
                            capture_stop.candidate_finished()
                            self._resume_tts_after_barge_in_candidate()
                        # Partial hypotheses are never an irreversible action.
                        continue
                    if not accepted:
                        capture_stop.candidate_finished()
                        self._resume_tts_after_barge_in_candidate()
                        logger.info(
                            "event=barge_in_candidate_suppressed "
                            "reason=final_not_confirmed segment=%s",
                            result.segment_id,
                        )
                        continue

                    phase = "playback" if playback_started_at is not None else "pre_playback"
                    capture_stop.candidate_finished()
                    session_id, turn_number = self._conversation_coordinates()
                    logger.info(
                        "conversation_session=%s turn=%s event=barge_in_detected "
                        "phase=%s segment=%s",
                        session_id,
                        turn_number,
                        phase,
                        result.segment_id,
                    )
                    capture_stop.barge_in_detected(now)
                    self._transition_voice_conversation(VoiceConversationState.BARGE_IN_DETECTED)
                    self._transition_voice_conversation(VoiceConversationState.CANCELLING)
                    self._interrupt_current_response(cancellation)
                    self._transition_voice_conversation(VoiceConversationState.FOLLOW_UP_FINALIZED)
                    next_session, next_turn = self._conversation_coordinates(next_turn=True)
                    logger.info(
                        "conversation_session=%s turn=%s event=stt_finalized "
                        "source=barge_in words=%s segment=%s",
                        next_session,
                        next_turn,
                        len(result.text.split()),
                        result.segment_id,
                    )
                    return result.text
            except Exception:
                self._interrupt_current_response(cancellation)
                raise
            finally:
                close_events = getattr(events, "close", None)
                if callable(close_events):
                    close_events()
            if continuous:
                if capture_stop.consume_candidate_timeout():
                    discard_candidate = getattr(
                        detector,
                        "discard_recognition_candidate",
                        None,
                    )
                    if callable(discard_candidate):
                        discard_candidate()
                    self._resume_tts_after_barge_in_candidate()
                    logger.info(
                        "event=barge_in_candidate_suppressed reason=provisional_inactivity_timeout"
                    )
                    if not response_future.done() and not self._stop_requested and not self._closed:
                        # Reopen one continuous Vosk session. A provisional
                        # timeout must not disable barge-in for the rest of the
                        # response after playback has safely resumed.
                        continue
                return None
            if response_future.done():
                discard_candidate = getattr(
                    detector,
                    "discard_recognition_candidate",
                    None,
                )
                if callable(discard_candidate):
                    discard_candidate()
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
            except Exception as exc:
                # The provider surfaces cancellation by raising from the
                # response worker. This is the expected barge-in terminal path,
                # not an application failure that warrants a traceback.
                logger.debug(
                    "event=interrupted_response_unwound outcome=%s",
                    type(exc).__name__,
                )
            finally:
                self._set_active_response_cancellation(None)

            model_prompt = follow_up.strip()
            if self.contains_wake_word(model_prompt):
                model_prompt = self._without_wake_word(model_prompt)
            if not model_prompt:
                return None
            if self._normalized_echo_text(model_prompt) in _EXPLICIT_SHORT_INTERRUPT_COMMANDS:
                # An explicit stop is control input, not a new conversational
                # request. The interrupted turn marker is committed by the API
                # cancellation path; return to listening without asking a model
                # to answer "stop".
                self._transition_voice_conversation(VoiceConversationState.LISTENING)
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
                model_prompt = (
                    self._without_wake_word(command) if has_wake_word else command.strip()
                )
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
        self._preparation_stop.clear()
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
            # This work is immediately required before accepting the first
            # command, so keep it on the main thread. If startup is interrupted,
            # there is no non-daemon executor worker left behind for CPython's
            # atexit hook to join indefinitely.
            if self.settings.barge_in_enabled:
                self._prepare_backchannels()
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
        self._preparation_stop.set()
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
            if self._close_complete.is_set():
                return
            if self._closing:
                close_complete = self._close_complete
                owns_close = False
            else:
                self._closing = True
                close_complete = self._close_complete
                owns_close = True
            self._closed = True
            self._stop_requested = True
        if not owns_close:
            if not close_complete.wait(_TASK_SHUTDOWN_TIMEOUT_SECONDS):
                logger.warning("event=assistant_concurrent_close_timeout")
            return

        self._running = False
        self._preparation_stop.set()
        close_succeeded = False
        shutdown_timed_out = False
        try:
            self._deactivate_voice_conversation()

            with self._capture_lock:
                capture_stop = self._active_capture_stop
            if capture_stop is not None:
                capture_stop.force_stop()

            self._interrupt_current_response()

            with self._backchannel_lock:
                backchannel = self._last_backchannel_session
            if backchannel is not None:
                stopped = backchannel.supersede(
                    timeout=_TASK_SHUTDOWN_TIMEOUT_SECONDS,
                )
                if not stopped and self._owns_conversation_executor:
                    shutdown_timed_out = True
                    logger.warning("event=backchannel_shutdown_timeout")

            # Retire model transports before waiting for their workers. This is
            # the operation that unblocks an Ollama HTTP read or Codex app-server
            # turn; waiting first leaves a non-daemon executor alive at exit.
            closed_ids: set[int] = set()
            api_close = getattr(self.api_client, "close", None)
            if callable(api_close):
                try:
                    api_close()
                except Exception:
                    logger.warning(
                        "Unable to close %s",
                        type(self.api_client).__name__,
                        exc_info=True,
                    )
            closed_ids.add(id(self.api_client))

            with self._task_lock:
                tasks = tuple(self._tasks)
                owned_tasks = frozenset(self._owned_tasks)
            for future in tasks:
                future.cancel()
            completed, pending = wait(
                tasks,
                timeout=_TASK_SHUTDOWN_TIMEOUT_SECONDS,
            )
            for future in completed:
                try:
                    future.result()
                except Exception:
                    # Cancellation and response failures are expected during
                    # teardown; contentful errors were observed by their owner.
                    pass
            if pending:
                logger.warning(
                    "event=assistant_task_shutdown_timeout pending_tasks=%s",
                    len(pending),
                )
                shutdown_timed_out = shutdown_timed_out or any(
                    future in owned_tasks for future in pending
                )

            if capture_stop is not None:
                # Do not terminate PyAudio while listen_events() may still be in
                # a device read or generator cleanup.
                if not capture_stop.wait_finished(_TASK_SHUTDOWN_TIMEOUT_SECONDS):
                    logger.warning("event=capture_shutdown_timeout")
                    shutdown_timed_out = True

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

            if shutdown_timed_out:
                # Service close methods may acquire locks held by the same
                # native worker (notably Piper synthesis). Let the CLI's
                # bounded hard-exit path run instead of blocking here forever.
                close_succeeded = True
                raise AssistantShutdownTimeout(
                    "Owned background workers did not stop before the shutdown deadline"
                )

            for service in (
                self.speech_recognizer,
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
            close_succeeded = True
        finally:
            with self._close_lock:
                self._closing = False
                # A KeyboardInterrupt must leave teardown retryable. The main
                # entry point force-exits on a repeated signal, while tests and
                # embedding callers can call close() again safely.
                if close_succeeded:
                    self._close_complete.set()

    def __enter__(self) -> VoiceAssistant:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
