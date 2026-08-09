"""Runtime orchestration for the voice assistant."""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from enum import Enum, auto
from typing import Any

import config
from api.api_client import APIClient, APIClientError
from api.metrics import record_safely
from audio.sound_player import SoundPlaybackError, SoundPlayer
from audio.tts import PiperTTS, TTSError
from recognizer.speech_recognizer import (
    RecognitionResult,
    SpeechRecognitionError,
    SpeechRecognizer,
)

logger = logging.getLogger(__name__)


class AssistantState(Enum):
    COMMAND = auto()
    RAG = auto()


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
        sound_executor: Any | None = None,
        choice: Callable[[tuple[str, ...]], str] = random.choice,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        metrics: Any | None = None,
    ) -> None:
        self.settings = settings
        self.profile = settings.profile

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

        self._rag_searcher = rag_searcher
        self._rag_factory = rag_factory
        self._rag_lock = threading.RLock()
        self._rag_prepare_future: Future[Any] | None = None
        self._rag_prepared = False
        self._sound_executor = sound_executor or ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="helios-sound",
        )
        self._owns_sound_executor = sound_executor is None
        self._choice = choice
        self._sleep = sleep
        self._clock = clock
        self.state = AssistantState.COMMAND
        self._running = False
        self._closed = False

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
            if isinstance(self.api_client, APIClient):
                return self.api_client.think(
                    think_prompt,
                    context=None,
                    tts=True,
                    pipeline_started_at=pipeline_started_at,
                )
            return self.api_client.think(think_prompt, context=None, tts=True)

        if isinstance(self.api_client, APIClient):
            return self.api_client.talk(
                model_prompt,
                context=None,
                pipeline_started_at=pipeline_started_at,
            )
        return self.api_client.talk(model_prompt, context=None)

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
            self._rag_prepare_future = self._sound_executor.submit(self._prepare_rag)
            return self._rag_prepare_future

    @staticmethod
    def _log_sound_failure(future: Future[Any]) -> None:
        try:
            future.result()
        except Exception:
            logger.warning("Notification sound playback failed", exc_info=True)

    def play_sound_async(self, sound_file: str) -> Any:
        future = self._sound_executor.submit(
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

    def run_once(self) -> bool:
        """Process at most one finalized utterance.

        Returns ``True`` when an utterance caused a state transition or command
        execution, and ``False`` for a timeout/partial-only result.
        """

        result = self._recognize_once()
        if result is None or not result.text or not result.is_final:
            return False
        command = result.text
        finalized_at = self._clock()

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
            if self.contains_wake_word(command):
                model_prompt = self._without_wake_word(command)
                selected_mode = "think" if self._think_prompt(model_prompt) is not None else "talk"
                record_safely(
                    self.metrics,
                    "wake_word_detected",
                    mode=selected_mode,
                    outcome="detected",
                    success=True,
                    wake_word_count=1,
                )
                try:
                    self.process_command(command, pipeline_started_at=finalized_at)
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
                    raise
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
        self._running = False
        cancel = getattr(self.api_client, "cancel_current", None)
        if callable(cancel):
            cancel()
        record_safely(
            self.metrics,
            "voice_cancelled",
            outcome="cancelled",
            success=False,
            cancellation_count=1,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._running = False

        if self._owns_sound_executor:
            self._sound_executor.shutdown(wait=True)

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
        self._closed = True

    def __enter__(self) -> VoiceAssistant:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
