"""Ollama chat client with optional sentence-by-sentence speech output."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable
from typing import Any, Protocol

import config

logger = logging.getLogger(__name__)
_SPEECH_MARKUP = re.compile(r"[*$#@]")
_SENTENCE_BOUNDARY = re.compile(r"[.!?;:,](?:\s|$)")


class TextToSpeech(Protocol):
    def speak(self, text: str) -> Any: ...


class APIClientError(RuntimeError):
    """Raised after communication with Ollama has failed."""


class _DoNotRetry(Exception):
    """Internal wrapper that preserves an exception across the retry boundary."""

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


def _is_transient_transport_error(error: Exception) -> bool:
    """Return whether retrying an Ollama operation is plausibly safe."""

    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return True

    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return status_code in {408, 425, 429} or status_code >= 500

    error_type = type(error)
    return error_type.__module__.split(".", maxsplit=1)[0] in {"httpx", "httpcore"} and (
        "connect" in error_type.__name__.lower()
        or "network" in error_type.__name__.lower()
        or "timeout" in error_type.__name__.lower()
        or "readerror" in error_type.__name__.lower()
        or "writeerror" in error_type.__name__.lower()
    )


def _default_client_factory(host: str) -> Any:
    try:
        from ollama import Client
    except ImportError as exc:  # pragma: no cover - depends on deployment extras
        raise APIClientError("The 'ollama' package is required to use the language model") from exc
    return Client(host=host)


def _chunk_value(chunk: Any, key: str, default: Any = None) -> Any:
    if isinstance(chunk, dict):
        return chunk.get(key, default)
    return getattr(chunk, key, default)


def _chunk_text(chunk: Any) -> str:
    message = _chunk_value(chunk, "message")
    if message is None:
        return ""
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


class APIClient:
    """Small boundary around the official Ollama SDK.

    Hardware-heavy TTS and the Ollama SDK are loaded lazily.  Passing the
    assistant's TTS instance ensures a single Piper model is shared across
    direct responses and model responses.
    """

    def __init__(
        self,
        api_url: str = config.OLLAMA_API_URL,
        model_talk: str = config.MODEL_TALK,
        model_think: str = config.MODEL_THINK,
        tts: TextToSpeech | None = None,
        *,
        client: Any | None = None,
        client_factory: Callable[[str], Any] | None = None,
        warm_up: bool = False,
        retry_attempts: int = 3,
        retry_wait: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if retry_attempts < 1:
            raise ValueError("retry_attempts must be at least one")
        if retry_wait < 0:
            raise ValueError("retry_wait cannot be negative")

        self.host = config.normalize_ollama_host(api_url)
        self.models = {"talk": model_talk, "think": model_think}
        self._client = client
        self._client_factory = client_factory or _default_client_factory
        self._tts = tts
        self._owns_tts = False
        self.retry_attempts = retry_attempts
        self.retry_wait = retry_wait
        self._sleep = sleep

        if warm_up:
            self.warm_up()

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory(self.host)
        return self._client

    @client.setter
    def client(self, value: Any) -> None:
        self._client = value

    @property
    def configured_tts(self) -> TextToSpeech | None:
        """Return an injected TTS instance without triggering lazy creation."""

        return self._tts

    @property
    def tts(self) -> TextToSpeech:
        if self._tts is None:
            from audio.tts import PiperTTS

            self._tts = PiperTTS()
            self._owns_tts = True
        return self._tts

    @tts.setter
    def tts(self, value: TextToSpeech) -> None:
        self._tts = value
        self._owns_tts = False

    def warm_up(self, mode: str = "talk") -> None:
        """Explicitly load a model; constructors remain free of network I/O."""

        if mode not in self.models:
            raise ValueError(f"Unknown model mode: {mode!r}")
        self._call_with_retry(
            lambda: self.client.chat(
                model=self.models[mode],
                messages=[{"role": "user", "content": ""}],
                stream=False,
            ),
            operation_name=f"warm up the {mode} model",
        )

    def _call_with_retry(
        self,
        operation: Callable[[], Any],
        *,
        operation_name: str = "contact Ollama",
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return operation()
            except _DoNotRetry as wrapped:
                if wrapped.__cause__ is wrapped.error:
                    raise wrapped.error from None
                raise wrapped.error from wrapped.__cause__
            except APIClientError:
                raise
            except Exception as exc:
                if not _is_transient_transport_error(exc):
                    raise APIClientError(f"Unable to {operation_name}: {exc}") from exc
                last_error = exc
                if attempt >= self.retry_attempts:
                    break
                logger.warning(
                    "Unable to %s (attempt %s/%s): %s",
                    operation_name,
                    attempt,
                    self.retry_attempts,
                    exc,
                )
                if self.retry_wait:
                    self._sleep(self.retry_wait)

        assert last_error is not None
        raise APIClientError(
            f"Unable to {operation_name} after {self.retry_attempts} attempt(s)"
        ) from last_error

    @staticmethod
    def _messages(message: str, context: str | None) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if context:
            messages.append({"role": "system", "content": context})
        messages.append({"role": "user", "content": message})
        return messages

    def _stream_once(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        speak: bool,
    ) -> str:
        chunks: Iterable[Any] = self.client.chat(
            model=model,
            messages=messages,
            stream=True,
        )
        response_parts: list[str] = []
        sentence_parts: list[str] = []
        speech_started = False

        def flush_speech() -> None:
            nonlocal speech_started
            if not sentence_parts:
                return
            sentence = _SPEECH_MARKUP.sub("", "".join(sentence_parts)).strip()
            sentence_parts.clear()
            if sentence:
                try:
                    self.tts.speak(sentence)
                except Exception as exc:
                    raise _DoNotRetry(exc) from exc
                speech_started = True

        try:
            for chunk in chunks:
                text = _chunk_text(chunk)
                done = bool(_chunk_value(chunk, "done", False))
                done_reason = _chunk_value(chunk, "done_reason")

                if text:
                    response_parts.append(text)
                    if speak:
                        sentence_parts.append(text)
                        if _SENTENCE_BOUNDARY.search(text):
                            flush_speech()

                if speak and (done or done_reason):
                    flush_speech()
        except _DoNotRetry:
            raise
        except Exception as exc:
            if speech_started:
                error = APIClientError(
                    "Ollama stream was interrupted after speech output began; "
                    "the request was not retried"
                )
                raise _DoNotRetry(error) from exc
            raise

        if speak:
            flush_speech()
        return "".join(response_parts)

    def _stream(
        self,
        *,
        mode: str,
        message: str,
        context: str | None,
        speak: bool,
    ) -> str:
        if not message or not message.strip():
            raise ValueError("message cannot be empty")
        if mode not in self.models:
            raise ValueError(f"Unknown model mode: {mode!r}")

        logger.info("Sending a request to Ollama model %s", self.models[mode])
        return self._call_with_retry(
            lambda: self._stream_once(
                model=self.models[mode],
                messages=self._messages(message, context),
                speak=speak,
            ),
            operation_name=f"stream a response from {self.models[mode]}",
        )

    def talk(self, message: str, context: str | None = None) -> str:
        """Stream a conversational response and speak it as sentences arrive."""

        return self._stream(
            mode="talk",
            message=message,
            context=context,
            speak=True,
        )

    def think(self, message: str, context: str | None = None, tts: bool = False) -> str:
        """Stream a reasoning response and optionally speak it."""

        return self._stream(
            mode="think",
            message=message,
            context=context,
            speak=tts,
        )

    def close(self) -> None:
        if self._owns_tts and self._tts is not None:
            close = getattr(self._tts, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> APIClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
