"""Ollama implementation of the provider-neutral streaming contract."""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

import config
from api.conversation import safe_conversation_identifier
from api.providers.contracts import (
    CancellationToken,
    ChatRequest,
    Completed,
    CompletionMetadata,
    ErrorCategory,
    FinishReason,
    ProviderCapabilities,
    ProviderError,
    ProviderIdentity,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    Usage,
)

_PROVIDER_NAME = "ollama"
_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429})

logger = logging.getLogger(__name__)


def _default_client_factory(host: str) -> Any:
    try:
        from ollama import Client
    except ImportError:
        raise ProviderError(
            ErrorCategory.PROVIDER_UNAVAILABLE,
            "The Ollama Python client is not installed",
            provider=_PROVIDER_NAME,
            transmitted=False,
        ) from None
    return Client(host=host)


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _nested_value(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        current = _value(current, key)
        if current is None:
            return None
    return current


def _text_field(chunk: Any, key: str) -> str:
    message = _value(chunk, "message")
    if message is None:
        return ""
    value = _value(message, key)
    return str(value or "")


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _first_integer(*values: Any) -> int | None:
    for value in values:
        parsed = _integer(value)
        if parsed is not None:
            return parsed
    return None


def _usage(chunk: Any) -> Usage:
    input_tokens = _first_integer(
        _nested_value(chunk, "usage", "prompt_tokens"),
        _nested_value(chunk, "usage", "input_tokens"),
        _value(chunk, "prompt_eval_count"),
    )
    output_tokens = _first_integer(
        _nested_value(chunk, "usage", "completion_tokens"),
        _nested_value(chunk, "usage", "output_tokens"),
        _value(chunk, "eval_count"),
    )
    cached_input_tokens = _first_integer(
        _nested_value(chunk, "usage", "prompt_tokens_details", "cached_tokens"),
        _nested_value(chunk, "usage", "cached_input_tokens"),
    )
    reasoning_tokens = _first_integer(
        _nested_value(chunk, "usage", "completion_tokens_details", "reasoning_tokens"),
        _nested_value(chunk, "usage", "reasoning_tokens"),
    )
    total_tokens = _first_integer(
        _nested_value(chunk, "usage", "total_tokens"),
        _value(chunk, "total_tokens"),
    )
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    return Usage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
    )


def _finish_reason(done: bool, provider_reason: str | None) -> FinishReason:
    if not provider_reason:
        return FinishReason.STOP if done else FinishReason.UNKNOWN

    normalized = provider_reason.strip().lower()
    if normalized in {"stop", "completed", "complete"}:
        return FinishReason.STOP
    if normalized in {"length", "max_tokens", "max_output_tokens"}:
        return FinishReason.LENGTH
    if normalized in {"tool_call", "tool_calls"}:
        return FinishReason.TOOL_CALL
    if normalized in {"safety", "content_filter", "refused"}:
        return FinishReason.SAFETY
    if normalized in {"cancelled", "canceled"}:
        return FinishReason.CANCELLED
    if normalized == "error":
        return FinishReason.ERROR
    return FinishReason.UNKNOWN


def _status_code(error: Exception) -> int | None:
    value = getattr(error, "status_code", None)
    if not isinstance(value, int):
        value = getattr(getattr(error, "response", None), "status_code", None)
    return value if isinstance(value, int) else None


def _safe_headers(error: Exception) -> Mapping[str, Any]:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        return headers
    headers = getattr(error, "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def _header(headers: Mapping[str, Any], name: str) -> Any:
    expected = name.lower()
    for key, value in headers.items():
        if str(key).lower() == expected:
            return value
    return None


def _retry_after(error: Exception) -> float | None:
    raw = _header(_safe_headers(error), "retry-after")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _request_id(error: Exception) -> str | None:
    raw = _header(_safe_headers(error), "x-request-id")
    if raw is None:
        raw = getattr(error, "request_id", None)
    if not isinstance(raw, str):
        return None
    # Request IDs are useful for support while remaining bounded and log-safe.
    return raw if len(raw) <= 128 and raw.isprintable() else None


def _is_transient_transport_error(error: Exception) -> bool:
    """Match the retry classification used by the original API client."""

    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return True

    status_code = _status_code(error)
    if status_code is not None:
        return status_code in _TRANSIENT_STATUS_CODES or status_code >= 500

    error_type = type(error)
    name = error_type.__name__.lower()
    return error_type.__module__.split(".", maxsplit=1)[0] in {"httpx", "httpcore"} and (
        "connect" in name
        or "network" in name
        or "timeout" in name
        or "readerror" in name
        or "writeerror" in name
    )


def _error_category(error: Exception, status_code: int | None) -> ErrorCategory:
    if status_code == 401:
        return ErrorCategory.AUTHENTICATION
    if status_code == 403:
        return ErrorCategory.PERMISSION
    if status_code == 408:
        return ErrorCategory.READ_TIMEOUT
    if status_code == 429:
        return ErrorCategory.RATE_LIMITED
    if status_code == 425 or (status_code is not None and status_code >= 500):
        return ErrorCategory.PROVIDER_UNAVAILABLE

    name = type(error).__name__.lower()
    module = type(error).__module__.split(".", maxsplit=1)[0]
    if isinstance(error, TimeoutError) or "timeout" in name:
        return ErrorCategory.CONNECT_TIMEOUT if "connect" in name else ErrorCategory.READ_TIMEOUT
    if isinstance(error, (ConnectionError, OSError)):
        return ErrorCategory.CONNECTIVITY
    if module in {"httpx", "httpcore"} and any(
        part in name for part in ("connect", "network", "readerror", "writeerror")
    ):
        return ErrorCategory.CONNECTIVITY
    return ErrorCategory.UNKNOWN


def _provider_error(
    error: Exception,
    *,
    model: str | None,
    transmitted: bool | None,
) -> ProviderError:
    if isinstance(error, ProviderError):
        return error

    status_code = _status_code(error)
    category = _error_category(error, status_code)
    if status_code is None:
        safe_message = f"Ollama request failed ({category.value})"
    else:
        safe_message = f"Ollama request failed with HTTP status {status_code}"

    return ProviderError(
        category,
        safe_message,
        provider=_PROVIDER_NAME,
        model=model,
        retryable_same_provider=_is_transient_transport_error(error),
        status_code=status_code,
        retry_after_seconds=_retry_after(error),
        request_id=_request_id(error),
        transmitted=transmitted,
    )


def _is_remote_endpoint(endpoint: str) -> bool:
    hostname = (urlsplit(endpoint).hostname or "").lower()
    return hostname not in {"localhost", "127.0.0.1", "::1"}


class OllamaAdapter:
    """Lazy, typed boundary around the official Ollama Python client."""

    def __init__(
        self,
        host: str,
        *,
        client: Any | None = None,
        client_factory: Callable[[str], Any] | None = None,
        cancellation_ack_timeout_seconds: float = 1.0,
    ) -> None:
        if (
            isinstance(cancellation_ack_timeout_seconds, bool)
            or not isinstance(cancellation_ack_timeout_seconds, (int, float))
            or not math.isfinite(float(cancellation_ack_timeout_seconds))
            or cancellation_ack_timeout_seconds <= 0
        ):
            raise ValueError("cancellation_ack_timeout_seconds must be positive and finite")
        self.host = config.normalize_ollama_host(host)
        self._client = client
        self._client_factory = client_factory or _default_client_factory
        self._owns_client = client is None
        self._client_lock = threading.Lock()
        self._client_epoch = 0
        self._client_unusable = False
        self._cancellation_ack_timeout_seconds = float(
            cancellation_ack_timeout_seconds
        )
        self._closed = False

    @property
    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            name=_PROVIDER_NAME,
            endpoint=self.host,
            remote=_is_remote_endpoint(self.host),
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_system_messages=True,
            supports_streaming_usage=True,
            supports_reasoning=True,
            features=frozenset({"reasoning", "streaming", "streaming_usage", "system_messages"}),
        )

    @property
    def client(self) -> Any:
        with self._client_lock:
            if self._closed or self._client_unusable:
                raise ProviderError(
                    ErrorCategory.PROVIDER_UNAVAILABLE,
                    "Ollama provider is unavailable",
                    provider=_PROVIDER_NAME,
                    transmitted=False,
                )
            if self._client is not None:
                return self._client
            epoch = self._client_epoch
        try:
            candidate = self._client_factory(self.host)
        except Exception as exc:
            raise _provider_error(exc, model=None, transmitted=False) from None
        with self._client_lock:
            stale = (
                self._closed
                or self._client_unusable
                or self._client_epoch != epoch
            )
            if not stale and self._client is None:
                self._client = candidate
                return candidate
            selected = self._client
        self._start_control_operation(
            getattr(candidate, "close", lambda: None),
            "stale_client_close",
        )
        if stale or selected is None:
            raise ProviderError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                "Ollama provider is unavailable",
                provider=_PROVIDER_NAME,
                transmitted=False,
            )
        return selected

    @client.setter
    def client(self, value: Any) -> None:
        with self._client_lock:
            self._client_epoch += 1
            self._client = value
            self._owns_client = value is None
            self._client_unusable = False
            self._closed = False

    def _start_control_operation(
        self,
        operation: Callable[[], Any],
        operation_name: str,
    ) -> threading.Event:
        done = threading.Event()

        def invoke() -> None:
            try:
                operation()
            except Exception:
                logger.warning(
                    "provider=%s event=%s_failed",
                    _PROVIDER_NAME,
                    operation_name,
                    exc_info=True,
                )
            finally:
                done.set()

        threading.Thread(
            target=invoke,
            name=f"helios-ollama-{operation_name}",
            daemon=True,
        ).start()
        return done

    def _retire_client(self, client: Any | None) -> threading.Event | None:
        with self._client_lock:
            if client is not None and self._client is not client:
                return None
            selected = self._client
            self._client_epoch += 1
            self._client = None
            if not self._owns_client:
                self._client_unusable = True
        if selected is None:
            return None
        close = getattr(selected, "close", None)
        if not callable(close):
            return None
        return self._start_control_operation(close, "client_retire")

    def stream(
        self,
        request: ChatRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> Iterable[StreamEvent]:
        unsupported = request.required_features - self.capabilities.features
        if unsupported:
            raise ProviderError(
                ErrorCategory.UNSUPPORTED_FEATURE,
                "Ollama does not support all requested features",
                provider=_PROVIDER_NAME,
                model=request.model,
                transmitted=False,
            )

        self._check_cancellation(cancellation, request.model)
        options = dict(request.options)
        if request.max_output_tokens is not None:
            options.setdefault("num_predict", request.max_output_tokens)

        arguments: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
            "stream": True,
        }
        if options:
            arguments["options"] = options

        return self._cancellable_stream(
            arguments,
            request=request,
            cancellation=cancellation,
        )

    def _cancellable_stream(
        self,
        arguments: Mapping[str, Any],
        *,
        request: ChatRequest,
        cancellation: CancellationToken | None,
    ) -> Iterator[StreamEvent]:
        mailbox: queue.Queue[tuple[str, Any]] = queue.Queue()
        holder: dict[str, Any] = {}
        stop_requested = threading.Event()
        worker_done = threading.Event()

        def worker() -> None:
            try:
                client = self.client
                holder["client"] = client
                if stop_requested.is_set():
                    return
                holder["chat_attempted"] = True
                try:
                    chunks = client.chat(**dict(arguments))
                except Exception as exc:
                    mailbox.put(("chat_error", exc))
                    return
                if stop_requested.is_set():
                    close = getattr(chunks, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass
                    return
                holder["chunks"] = chunks
                for event in self._stream_events(
                    chunks,
                    request=request,
                    cancellation=cancellation,
                ):
                    if stop_requested.is_set():
                        return
                    mailbox.put(("event", event))
                mailbox.put(("eof", None))
            except BaseException as exc:
                mailbox.put(("error", exc))
            finally:
                worker_done.set()

        threading.Thread(
            target=worker,
            name="helios-ollama-stream",
            daemon=True,
        ).start()

        cancelled_worker = False
        began = time.monotonic()
        last_event = began
        received_event = False

        def stop_worker(reason: str) -> None:
            nonlocal cancelled_worker
            if cancelled_worker:
                return
            cancelled_worker = True
            stop_requested.set()
            # ``chunks`` is commonly a Python generator owned by the stream
            # worker. Closing it from this consumer thread while ``__next__``
            # is blocked in HTTP I/O raises ``ValueError: generator already
            # executing`` and leaves cancellation waiting for the transport
            # anyway. Retiring the client closes the underlying transport and
            # unblocks the worker; ``_stream_events`` then closes ``chunks`` in
            # its own ``finally`` block on the worker thread.
            self._retire_client(holder.get("client"))
            if not worker_done.wait(self._cancellation_ack_timeout_seconds):
                logger.warning(
                    "conversation_session=%s turn=%s provider=%s "
                    "event=cancellation_ack_timeout reason=%s",
                    safe_conversation_identifier(request.conversation_id),
                    request.conversation_turn,
                    _PROVIDER_NAME,
                    reason,
                )
            else:
                logger.info(
                    "conversation_session=%s turn=%s provider=%s "
                    "event=cancellation_acknowledged reason=%s",
                    safe_conversation_identifier(request.conversation_id),
                    request.conversation_turn,
                    _PROVIDER_NAME,
                    reason,
                )

        try:
            while True:
                try:
                    self._check_cancellation(cancellation, request.model)
                except ProviderError:
                    stop_worker("cancelled")
                    raise
                now = time.monotonic()
                total_remaining = request.timeouts.total_seconds - (now - began)
                if "chunks" not in holder:
                    stage_limit = request.timeouts.connect_seconds
                elif not received_event:
                    stage_limit = request.timeouts.first_token_seconds
                else:
                    stage_limit = request.timeouts.read_seconds
                stage_remaining = stage_limit - (now - last_event)
                wait_seconds = min(0.05, total_remaining, stage_remaining)
                if wait_seconds <= 0:
                    stop_worker("timeout")
                    if "chunks" not in holder:
                        category = ErrorCategory.CONNECT_TIMEOUT
                    elif not received_event:
                        category = ErrorCategory.FIRST_TOKEN_TIMEOUT
                    else:
                        category = ErrorCategory.READ_TIMEOUT
                    raise ProviderError(
                        category,
                        "Ollama request timed out",
                        provider=_PROVIDER_NAME,
                        model=request.model,
                        retryable_same_provider=True,
                        transmitted=bool(holder.get("chat_attempted")),
                    )
                try:
                    kind, value = mailbox.get(timeout=wait_seconds)
                except queue.Empty:
                    continue
                if kind == "event":
                    received_event = True
                    last_event = time.monotonic()
                    yield value
                    continue
                if kind == "eof":
                    return
                if kind == "chat_error" and isinstance(value, Exception):
                    raise _provider_error(
                        value,
                        model=request.model,
                        transmitted=None,
                    ) from None
                if isinstance(value, ProviderError):
                    raise value
                if isinstance(value, Exception):
                    raise _provider_error(
                        value,
                        model=request.model,
                        transmitted=bool(holder.get("chat_attempted")),
                    ) from None
                raise ProviderError(
                    ErrorCategory.UNKNOWN,
                    "Ollama stream worker failed",
                    provider=_PROVIDER_NAME,
                    model=request.model,
                    transmitted=bool(holder.get("chat_attempted")),
                )
        finally:
            if not worker_done.is_set():
                stop_worker("consumer_closed")

    def _stream_events(
        self,
        chunks: Iterable[Any],
        *,
        request: ChatRequest,
        cancellation: CancellationToken | None,
    ) -> Iterator[StreamEvent]:
        last_chunk: Any = None
        try:
            iterator = iter(chunks)
            for chunk in iterator:
                last_chunk = chunk
                self._check_cancellation(cancellation, request.model)

                text = _text_field(chunk, "content")
                reasoning = _text_field(chunk, "thinking")
                done = bool(_value(chunk, "done", False))
                raw_reason = _value(chunk, "done_reason")
                provider_reason = str(raw_reason) if raw_reason else None

                # A terminal Ollama chunk may also contain its final text.
                if text:
                    yield TextDelta(text)
                if reasoning:
                    yield ReasoningDelta(reasoning)

                if done or provider_reason:
                    yield Completed(
                        self._metadata(
                            request=request,
                            chunk=chunk,
                            done=done,
                            provider_reason=provider_reason,
                        )
                    )
                    return

            # Preserve the old client's clean-EOF behavior while still giving
            # provider-neutral consumers a terminal event.
            yield Completed(
                self._metadata(
                    request=request,
                    chunk=last_chunk,
                    done=False,
                    provider_reason=None,
                )
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise _provider_error(exc, model=request.model, transmitted=True) from None
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    # Stream cleanup must not hide the completion or primary
                    # transport error, and provider details must not leak.
                    pass

    @staticmethod
    def _metadata(
        *,
        request: ChatRequest,
        chunk: Any,
        done: bool,
        provider_reason: str | None,
    ) -> CompletionMetadata:
        raw_model = _value(chunk, "model") if chunk is not None else None
        resolved_model = str(raw_model) if raw_model else None
        raw_request_id = _value(chunk, "request_id") if chunk is not None else None
        request_id = (
            raw_request_id
            if isinstance(raw_request_id, str)
            and len(raw_request_id) <= 128
            and raw_request_id.isprintable()
            else None
        )
        return CompletionMetadata(
            provider=_PROVIDER_NAME,
            requested_model=request.model,
            resolved_model=resolved_model,
            finish_reason=_finish_reason(done, provider_reason),
            provider_finish_reason=provider_reason,
            usage=_usage(chunk) if chunk is not None else Usage(),
            request_id=request_id,
        )

    @staticmethod
    def _check_cancellation(
        cancellation: CancellationToken | None,
        model: str,
    ) -> None:
        if cancellation is None:
            return
        try:
            cancellation.raise_if_cancelled()
        except ProviderError:
            raise
        except Exception:
            raise ProviderError(
                ErrorCategory.CANCELLED,
                "Ollama request was cancelled",
                provider=_PROVIDER_NAME,
                model=model,
                retryable_same_provider=False,
                transmitted=None,
            ) from None

    def warm_up(self, model: str) -> None:
        if not model or not model.strip():
            raise ValueError("model cannot be empty")
        try:
            self.client.chat(
                model=model,
                messages=[{"role": "user", "content": ""}],
                stream=False,
            )
        except Exception as exc:
            raise _provider_error(exc, model=model, transmitted=None) from None

    def close(self) -> None:
        with self._client_lock:
            if self._closed:
                return
            self._closed = True
            self._client_epoch += 1
            client = self._client
            self._client = None
            owns_client = self._owns_client
        if not owns_client or client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            done = self._start_control_operation(close, "client_close")
            if not done.wait(self._cancellation_ack_timeout_seconds):
                logger.warning(
                    "provider=%s event=client_close_timeout",
                    _PROVIDER_NAME,
                )

    def __enter__(self) -> OllamaAdapter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
