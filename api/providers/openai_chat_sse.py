"""Strict OpenAI-compatible Chat Completions streaming adapter.

The adapter intentionally implements only the small HTTP/SSE surface Helios
uses.  It does not depend on a provider SDK, perform retries, or expose raw
transport objects and exceptions to callers.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from api.providers.contracts import (
    CancellationToken,
    ChatRequest,
    Completed,
    CompletionMetadata,
    ContentOrigin,
    ErrorCategory,
    FinishReason,
    PrivacyLevel,
    ProviderCapabilities,
    ProviderError,
    ProviderIdentity,
    RateLimitSnapshot,
    ReasoningDelta,
    Refused,
    StreamEvent,
    TextDelta,
    Timeouts,
    Usage,
)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SAFE_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")
_SAFE_RATE_RESET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+ -]{0,63}$")
_STANDARD_OPTIONS = frozenset(
    {
        "frequency_penalty",
        "include_reasoning",
        "logit_bias",
        "presence_penalty",
        "reasoning_effort",
        "seed",
        "stop",
        "temperature",
        "top_p",
    }
)
_RESTRICTED_HEADERS = frozenset(
    {"accept", "authorization", "content-length", "content-type", "host"}
)
_SENSITIVE_HEADER_MARKERS = (
    "api-key",
    "apikey",
    "authorization",
    "cookie",
    "secret",
    "token",
)


class _NormalizedProviderError(ProviderError):
    """Marker for failures already sanitized by this adapter."""


class _StreamingResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def iter_lines(self) -> Iterable[str | bytes]: ...


class _ResponseContext(Protocol):
    def __enter__(self) -> _StreamingResponse: ...

    def __exit__(self, *args: object) -> object: ...


class _Transport(Protocol):
    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any],
        timeouts: Timeouts,
    ) -> _ResponseContext: ...

    def close(self) -> None: ...


class _HttpxFirstTokenTimeout(TimeoutError):
    """The first complete output event did not arrive in time."""


class _HttpxReadTimeout(TimeoutError):
    """The stream was idle after output had started."""


class _HttpxTotalTimeout(TimeoutError):
    """Active transport time exhausted the request budget."""


def _wait_for_item(mailbox: queue.Queue[Any], timeout: float) -> Any:
    return mailbox.get(timeout=timeout)


class _TimedHttpxResponse:
    """Serialize response reads while applying stage-specific idle deadlines."""

    def __init__(
        self,
        response: Any,
        *,
        timeouts: Timeouts,
        opening_elapsed: float,
        clock: Callable[[], float],
        wait_for_item: Callable[[queue.Queue[Any], float], Any],
    ) -> None:
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers
        self._clock = clock
        self._wait_for_item = wait_for_item
        self._first_remaining = timeouts.first_token_seconds - opening_elapsed
        self._read_seconds = timeouts.read_seconds
        self._total_remaining = timeouts.total_seconds - opening_elapsed
        self._token_seen = False
        self._closed = False
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._permits: queue.Queue[object] = queue.Queue(maxsize=1)
        self._results: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def __getattr__(self, name: str) -> Any:
        # Error responses still need bounded access to json()/iter_bytes().
        return getattr(self._response, name)

    def mark_first_token(self) -> None:
        self._token_seen = True

    def opening_timeout(self) -> Exception | None:
        if self._total_remaining <= 0:
            return _HttpxTotalTimeout()
        if self._first_remaining <= 0:
            return _HttpxFirstTokenTimeout()
        return None

    def _start_reader(self) -> None:
        if self._reader is not None:
            return

        def read_lines() -> None:
            try:
                iterator = iter(self._response.iter_lines())
                while not self._stop.is_set():
                    self._permits.get()
                    if self._stop.is_set():
                        return
                    try:
                        line = next(iterator)
                    except StopIteration:
                        self._results.put(("eof", None))
                        return
                    except Exception as exc:
                        self._results.put(("error", exc))
                        return
                    self._results.put(("line", line))
            except Exception as exc:
                self._results.put(("error", exc))

        self._reader = threading.Thread(
            target=read_lines,
            name="helios-http-stream-reader",
            daemon=True,
        )
        self._reader.start()

    def _next_timeout(self) -> tuple[float, type[TimeoutError]]:
        if self._total_remaining <= 0:
            raise _HttpxTotalTimeout()
        if not self._token_seen and self._first_remaining <= 0:
            raise _HttpxFirstTokenTimeout()

        stage_timeout = self._read_seconds if self._token_seen else self._first_remaining
        if self._total_remaining <= stage_timeout:
            return self._total_remaining, _HttpxTotalTimeout
        if self._token_seen:
            return stage_timeout, _HttpxReadTimeout
        return stage_timeout, _HttpxFirstTokenTimeout

    def _consume_active_wait(self, elapsed: float) -> None:
        elapsed = max(0.0, elapsed)
        self._total_remaining -= elapsed
        if not self._token_seen:
            self._first_remaining -= elapsed

    def iter_lines(self) -> Iterator[str | bytes]:
        if self._closed:
            raise RuntimeError("response is closed")
        self._start_reader()
        while True:
            timeout, timeout_type = self._next_timeout()
            began = self._clock()
            self._permits.put(object())
            try:
                kind, value = self._wait_for_item(self._results, timeout)
            except queue.Empty:
                elapsed = max(self._clock() - began, timeout)
                self._consume_active_wait(elapsed)
                self.close()
                raise timeout_type() from None
            self._consume_active_wait(self._clock() - began)
            if self._total_remaining < 0:
                self.close()
                raise _HttpxTotalTimeout() from None
            if not self._token_seen and self._first_remaining < 0:
                self.close()
                raise _HttpxFirstTokenTimeout() from None
            if kind == "line":
                yield value
            elif kind == "eof":
                return
            else:
                raise value

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        try:
            self._permits.put_nowait(object())
        except queue.Full:
            pass
        close = getattr(self._response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=0.1)


class _TimedHttpxResponseContext:
    """Apply the first-token/total deadline while awaiting response headers."""

    def __init__(
        self,
        response_context: Any,
        *,
        timeouts: Timeouts,
        clock: Callable[[], float],
        wait_for_item: Callable[[queue.Queue[Any], float], Any],
    ) -> None:
        self._response_context = response_context
        self._timeouts = timeouts
        self._clock = clock
        self._wait_for_item = wait_for_item
        self._response: _TimedHttpxResponse | None = None

    def _close_late_response(self, mailbox: queue.Queue[tuple[str, Any]]) -> None:
        def close_when_available() -> None:
            kind, value = mailbox.get()
            if kind != "response":
                return
            close = getattr(value, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            try:
                self._response_context.__exit__(None, None, None)
            except Exception:
                pass

        threading.Thread(
            target=close_when_available,
            name="helios-http-late-response-cleanup",
            daemon=True,
        ).start()

    def __enter__(self) -> _TimedHttpxResponse:
        mailbox: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def open_response() -> None:
            try:
                mailbox.put(("response", self._response_context.__enter__()))
            except Exception as exc:
                mailbox.put(("error", exc))

        threading.Thread(
            target=open_response,
            name="helios-http-response-open",
            daemon=True,
        ).start()
        timeout = min(
            self._timeouts.first_token_seconds,
            self._timeouts.total_seconds,
        )
        began = self._clock()
        try:
            kind, value = self._wait_for_item(mailbox, timeout)
        except queue.Empty:
            self._close_late_response(mailbox)
            if self._timeouts.total_seconds <= self._timeouts.first_token_seconds:
                raise _HttpxTotalTimeout() from None
            raise _HttpxFirstTokenTimeout() from None
        opening_elapsed = max(0.0, self._clock() - began)
        if kind == "error":
            raise value
        self._response = _TimedHttpxResponse(
            value,
            timeouts=self._timeouts,
            opening_elapsed=opening_elapsed,
            clock=self._clock,
            wait_for_item=self._wait_for_item,
        )
        timeout_error = self._response.opening_timeout()
        if timeout_error is not None:
            self._response.close()
            self._response_context.__exit__(None, None, None)
            raise timeout_error
        return self._response

    def __exit__(self, *args: object) -> object:
        if self._response is not None:
            self._response.close()
        return self._response_context.__exit__(*args)


class _HttpxTransport:
    """Small wrapper keeping httpx types out of the public adapter contract."""

    def __init__(
        self,
        httpx_module: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        wait_for_item: Callable[[queue.Queue[Any], float], Any] = _wait_for_item,
    ) -> None:
        self._httpx = httpx_module
        self._client = httpx_module.Client(follow_redirects=False)
        self._clock = clock
        self._wait_for_item = wait_for_item

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any],
        timeouts: Timeouts,
    ) -> _ResponseContext:
        # The outer response wrapper enforces distinct first-token, idle-read,
        # and active-total budgets.  This bounded socket timeout is only a
        # safety net and must not pre-empt either stream phase.
        read_timeout = max(timeouts.first_token_seconds, timeouts.read_seconds)
        timeout = self._httpx.Timeout(
            timeouts.total_seconds,
            connect=timeouts.connect_seconds,
            read=read_timeout,
            write=min(timeouts.read_seconds, timeouts.total_seconds),
            pool=timeouts.connect_seconds,
        )
        return _TimedHttpxResponseContext(
            self._client.stream(
                method,
                url,
                headers=dict(headers),
                json=dict(json),
                timeout=timeout,
            ),
            timeouts=timeouts,
            clock=self._clock,
            wait_for_item=self._wait_for_item,
        )

    def close(self) -> None:
        self._client.close()


def _default_transport_factory() -> _Transport:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on deployment extras
        raise RuntimeError("HTTP transport dependency is unavailable") from exc
    return _HttpxTransport(httpx)


def _safe_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SAFE_IDENTIFIER.fullmatch(value) else None


def _header_map(headers: Any) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        try:
            headers = dict(headers)
        except (TypeError, ValueError):
            return {}
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if isinstance(key, str) and isinstance(value, (str, int, float)):
            normalized[key.lower()] = str(value).strip()
    return normalized


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _safe_rate_reset(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SAFE_RATE_RESET.fullmatch(value) else None


def _rate_limits(headers: Mapping[str, str]) -> RateLimitSnapshot | None:
    snapshot = RateLimitSnapshot(
        remaining_requests=_optional_int(headers.get("x-ratelimit-remaining-requests")),
        remaining_tokens=_optional_int(headers.get("x-ratelimit-remaining-tokens")),
        reset_requests=_safe_rate_reset(headers.get("x-ratelimit-reset-requests")),
        reset_tokens=_safe_rate_reset(headers.get("x-ratelimit-reset-tokens")),
        retry_after_seconds=_optional_float(headers.get("retry-after")),
    )
    if all(
        value is None
        for value in (
            snapshot.remaining_requests,
            snapshot.remaining_tokens,
            snapshot.reset_requests,
            snapshot.reset_tokens,
            snapshot.retry_after_seconds,
        )
    ):
        return None
    return snapshot


def _request_id(headers: Mapping[str, str]) -> str | None:
    for name in ("x-request-id", "request-id", "cf-ray"):
        request_id = _safe_identifier(headers.get(name))
        if request_id is not None:
            return request_id
    return None


def _finish_reason(value: str | None) -> FinishReason:
    if value in {"stop", "end_turn", "eos"}:
        return FinishReason.STOP
    if value in {"length", "max_tokens", "max_output_tokens"}:
        return FinishReason.LENGTH
    if value in {"tool_calls", "function_call"}:
        return FinishReason.TOOL_CALL
    if value in {"content_filter", "safety", "refusal", "blocked"}:
        return FinishReason.SAFETY
    if value in {"cancelled", "canceled"}:
        return FinishReason.CANCELLED
    if value in {"error"}:
        return FinishReason.ERROR
    return FinishReason.UNKNOWN


def _merge_usage(previous: Usage, payload: Any) -> Usage:
    if not isinstance(payload, Mapping):
        return previous
    prompt_details = payload.get("prompt_tokens_details")
    completion_details = payload.get("completion_tokens_details")
    if not isinstance(prompt_details, Mapping):
        prompt_details = {}
    if not isinstance(completion_details, Mapping):
        completion_details = {}

    def latest(*values: Any, old: int | None) -> int | None:
        for value in values:
            parsed = _optional_int(value)
            if parsed is not None:
                return parsed
        return old

    return Usage(
        input_tokens=latest(
            payload.get("prompt_tokens"),
            payload.get("input_tokens"),
            old=previous.input_tokens,
        ),
        cached_input_tokens=latest(
            prompt_details.get("cached_tokens"),
            payload.get("cached_input_tokens"),
            old=previous.cached_input_tokens,
        ),
        output_tokens=latest(
            payload.get("completion_tokens"),
            payload.get("output_tokens"),
            old=previous.output_tokens,
        ),
        reasoning_tokens=latest(
            completion_details.get("reasoning_tokens"),
            payload.get("reasoning_tokens"),
            old=previous.reasoning_tokens,
        ),
        total_tokens=latest(payload.get("total_tokens"), old=previous.total_tokens),
    )


def _error_text(payload: Any) -> str:
    """Return bounded classification text that is never propagated or logged."""

    try:
        return json.dumps(payload, ensure_ascii=True, default=lambda _: "")[:8192].lower()
    except (TypeError, ValueError):
        return ""


def _body_category(status_code: int, payload: Any) -> ErrorCategory | None:
    text = _error_text(payload)
    if any(
        marker in text
        for marker in (
            "insufficient_quota",
            "quota_exceeded",
            "quota exhausted",
            "billing_hard_limit",
            "credit balance",
            "insufficient balance",
        )
    ):
        return ErrorCategory.QUOTA_EXHAUSTED
    if any(
        marker in text
        for marker in (
            "context_length_exceeded",
            "context window",
            "maximum context",
            "too many tokens",
            "input is too long",
            "reduce the length",
            "request too large",
        )
    ):
        return ErrorCategory.CONTEXT_OVERFLOW
    if any(
        marker in text for marker in ("content_filter", "safety", "moderation", "policy violation")
    ):
        return ErrorCategory.SAFETY_REFUSAL
    if status_code == 413:
        return ErrorCategory.CONTEXT_OVERFLOW
    if status_code in {400, 404, 409, 413, 422}:
        return ErrorCategory.UNSUPPORTED_FEATURE
    return None


def _safe_error_message(category: ErrorCategory) -> str:
    messages = {
        ErrorCategory.AUTHENTICATION: "Remote provider authentication failed",
        ErrorCategory.PERMISSION: "Remote provider permission was denied",
        ErrorCategory.RATE_LIMITED: "Remote provider rate limit was reached",
        ErrorCategory.QUOTA_EXHAUSTED: "Remote provider quota was exhausted",
        ErrorCategory.CONTEXT_OVERFLOW: "The request exceeds the remote model context limit",
        ErrorCategory.SAFETY_REFUSAL: "The remote provider declined the request",
        ErrorCategory.CONNECT_TIMEOUT: "Timed out while connecting to the remote provider",
        ErrorCategory.FIRST_TOKEN_TIMEOUT: "Timed out waiting for the first remote response event",
        ErrorCategory.READ_TIMEOUT: "Timed out while reading the remote response",
        ErrorCategory.DNS: "Could not resolve the remote provider",
        ErrorCategory.TLS: "Could not verify the remote provider TLS connection",
        ErrorCategory.CONNECTIVITY: "Could not connect to the remote provider",
        ErrorCategory.PROVIDER_UNAVAILABLE: "The remote provider is unavailable",
        ErrorCategory.MALFORMED_RESPONSE: "The remote provider returned an invalid stream",
        ErrorCategory.EMPTY_COMPLETION: "The remote provider returned no visible response",
        ErrorCategory.PRIVACY_BLOCKED: "Remote transmission is not authorized for this request",
        ErrorCategory.UNSUPPORTED_FEATURE: "The remote provider does not support this request",
        ErrorCategory.CANCELLED: "The remote request was cancelled",
    }
    return messages.get(category, "The remote provider request failed")


def _transport_category(error: Exception, *, first_event_seen: bool) -> ErrorCategory:
    error_type = type(error)
    marker = (f"{error_type.__module__}.{error_type.__name__} {error}").lower()
    if any(token in marker for token in ("ssl", "tls", "certificate", "certverification")):
        return ErrorCategory.TLS
    if any(
        token in marker
        for token in ("gaierror", "getaddrinfo", "nameresolution", "name resolution", "dns")
    ):
        return ErrorCategory.DNS
    if "timeout" in marker or isinstance(error, TimeoutError):
        if "connect" in marker:
            return ErrorCategory.CONNECT_TIMEOUT
        return ErrorCategory.READ_TIMEOUT if first_event_seen else ErrorCategory.FIRST_TOKEN_TIMEOUT
    if isinstance(error, (ConnectionError, OSError)) or any(
        token in marker
        for token in (
            "connecterror",
            "networkerror",
            "network error",
            "readerror",
            "writeerror",
            "remoteprotocolerror",
        )
    ):
        return ErrorCategory.CONNECTIVITY
    return ErrorCategory.UNKNOWN


@dataclass(slots=True)
class _StreamState:
    provider: str
    requested_model: str
    request_id: str | None
    rate_limits: RateLimitSnapshot | None
    resolved_model: str | None = None
    provider_finish_reason: str | None = None
    finish_reason: FinishReason = FinishReason.UNKNOWN
    usage: Usage = field(default_factory=Usage)
    response_received: bool = False
    saw_token: bool = False
    saw_visible_text: bool = False
    refusal: bool = False

    def metadata(self) -> CompletionMetadata:
        return CompletionMetadata(
            provider=self.provider,
            requested_model=self.requested_model,
            resolved_model=self.resolved_model,
            finish_reason=(FinishReason.SAFETY if self.refusal else self.finish_reason),
            provider_finish_reason=self.provider_finish_reason,
            usage=self.usage,
            request_id=self.request_id,
            rate_limits=self.rate_limits,
        )


class OpenAIChatSSEAdapter:
    """One-attempt adapter for OpenAI-compatible Chat Completions SSE.

    An injected transport implements ``stream(method, url, headers=...,
    json=..., timeouts=...)`` and returns a response context manager.  This
    deliberately tiny protocol keeps unit tests independent of httpx.
    """

    def __init__(
        self,
        provider: str,
        endpoint: str,
        api_key_env: str,
        *,
        transport: _Transport | None = None,
        transport_factory: Callable[[], _Transport] | None = None,
        owns_transport: bool | None = None,
        capabilities: ProviderCapabilities | None = None,
        extra_headers: Mapping[str, str] | None = None,
        include_stream_usage: bool = True,
        output_token_field: str = "max_tokens",
        allowed_options: frozenset[str] = _STANDARD_OPTIONS,
        max_event_bytes: int = 1_048_576,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not _SAFE_NAME.fullmatch(provider):
            raise ValueError("provider must be a short, safe identifier")
        if not _SAFE_ENV_NAME.fullmatch(api_key_env):
            raise ValueError("api_key_env must be an environment-variable name")
        if output_token_field not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("unsupported output token field")
        if max_event_bytes < 1024:
            raise ValueError("max_event_bytes must be at least 1024")
        if transport is not None and transport_factory is not None:
            raise ValueError("pass either transport or transport_factory, not both")
        reserved_options = {
            "messages",
            "model",
            "stream",
            "stream_options",
            "max_tokens",
            "max_completion_tokens",
        }
        if set(allowed_options).intersection(reserved_options):
            raise ValueError("allowed_options cannot include transport-controlled fields")

        self._endpoint = self._validate_endpoint(endpoint)
        self._url = self._completion_url(self._endpoint)
        self._api_key_env = api_key_env
        self._transport = transport
        self._transport_factory = transport_factory or _default_transport_factory
        self._owns_transport = transport is None if owns_transport is None else owns_transport
        self._capabilities = capabilities or ProviderCapabilities(
            supports_system_messages=True,
            supports_streaming_usage=True,
            supports_reasoning=True,
            features=frozenset({"reasoning", "streaming", "streaming_usage", "system_messages"}),
        )
        self._extra_headers = self._validate_headers(extra_headers or {})
        self._include_stream_usage = include_stream_usage
        self._output_token_field = output_token_field
        self._allowed_options = frozenset(allowed_options)
        self._max_event_bytes = max_event_bytes
        self._clock = clock
        self._identity = ProviderIdentity(
            name=provider,
            endpoint=self._endpoint,
            remote=True,
        )
        self._closed = False

    @staticmethod
    def _validate_endpoint(endpoint: str) -> str:
        endpoint = endpoint.strip()
        if any(ord(character) < 32 or ord(character) == 127 for character in endpoint):
            raise ValueError("remote provider endpoint cannot contain control characters")
        parsed = urlsplit(endpoint)
        if parsed.scheme.lower() != "https":
            raise ValueError("remote provider endpoint must use HTTPS")
        if not parsed.hostname:
            raise ValueError("remote provider endpoint must include a host")
        if parsed.username or parsed.password:
            raise ValueError("remote provider endpoint cannot contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("remote provider endpoint cannot contain a query or fragment")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("remote provider endpoint contains an invalid port")
        path = parsed.path.rstrip("/")
        return urlunsplit(("https", parsed.netloc, path, "", ""))

    @staticmethod
    def _completion_url(endpoint: str) -> str:
        if endpoint.endswith("/chat/completions"):
            return endpoint
        return f"{endpoint}/chat/completions"

    @staticmethod
    def _validate_headers(headers: Mapping[str, str]) -> dict[str, str]:
        validated: dict[str, str] = {}
        for key, value in headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("extra headers must contain strings")
            normalized = key.strip().lower()
            if not normalized or normalized in _RESTRICTED_HEADERS:
                raise ValueError("extra headers cannot override transport headers")
            if any(marker in normalized for marker in _SENSITIVE_HEADER_MARKERS):
                raise ValueError("credentials must be supplied through api_key_env")
            if "\r" in key or "\n" in key or "\r" in value or "\n" in value:
                raise ValueError("extra headers cannot contain line breaks")
            validated[key.strip()] = value
        return validated

    @property
    def identity(self) -> ProviderIdentity:
        return self._identity

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def _get_transport(self) -> _Transport:
        if self._closed:
            raise self._error(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                model=None,
                transmitted=False,
            )
        if self._transport is None:
            try:
                self._transport = self._transport_factory()
            except _NormalizedProviderError:
                raise
            except Exception:
                raise self._error(
                    ErrorCategory.PROVIDER_UNAVAILABLE,
                    model=None,
                    transmitted=False,
                ) from None
        return self._transport

    def _error(
        self,
        category: ErrorCategory,
        *,
        model: str | None,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        request_id: str | None = None,
        transmitted: bool | None,
    ) -> ProviderError:
        return _NormalizedProviderError(
            category,
            _safe_error_message(category),
            provider=self.identity.name,
            model=model,
            retryable_same_provider=retryable,
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            request_id=request_id,
            transmitted=transmitted,
        )

    def _authorize_remote(self, request: ChatRequest) -> None:
        try:
            privacy = PrivacyLevel(request.privacy)
            origins = tuple(ContentOrigin(message.origin) for message in request.messages)
        except (TypeError, ValueError):
            raise self._error(
                ErrorCategory.PRIVACY_BLOCKED,
                model=request.model,
                transmitted=False,
            ) from None

        if privacy is PrivacyLevel.LOCAL_ONLY or request.remote_authorized is not True:
            raise self._error(
                ErrorCategory.PRIVACY_BLOCKED,
                model=request.model,
                transmitted=False,
            )
        if privacy is PrivacyLevel.REMOTE_REDACTED:
            unredacted = any(
                origin is not ContentOrigin.STATIC_INSTRUCTION and message.redacted is not True
                for message, origin in zip(request.messages, origins)
            )
            if unredacted:
                raise self._error(
                    ErrorCategory.PRIVACY_BLOCKED,
                    model=request.model,
                    transmitted=False,
                )

    def _preflight(self, request: ChatRequest) -> None:
        if (
            any(message.role.value == "system" for message in request.messages)
            and not self.capabilities.supports_system_messages
        ):
            raise self._error(
                ErrorCategory.UNSUPPORTED_FEATURE,
                model=request.model,
                transmitted=False,
            )
        supported_features = set(self.capabilities.features)
        supported_features.add("streaming")
        if self.capabilities.supports_reasoning:
            supported_features.add("reasoning")
        if self.capabilities.supports_system_messages:
            supported_features.add("system_messages")
        if self.capabilities.supports_streaming_usage:
            supported_features.add("streaming_usage")
        if not request.required_features.issubset(supported_features):
            raise self._error(
                ErrorCategory.UNSUPPORTED_FEATURE,
                model=request.model,
                transmitted=False,
            )
        if self.capabilities.languages and request.language not in self.capabilities.languages:
            raise self._error(
                ErrorCategory.UNSUPPORTED_FEATURE,
                model=request.model,
                transmitted=False,
            )
        if (
            request.max_output_tokens is not None
            and self.capabilities.max_output_tokens is not None
            and request.max_output_tokens > self.capabilities.max_output_tokens
        ):
            raise self._error(
                ErrorCategory.UNSUPPORTED_FEATURE,
                model=request.model,
                transmitted=False,
            )
        unknown_options = set(request.options).difference(self._allowed_options)
        if unknown_options:
            raise self._error(
                ErrorCategory.UNSUPPORTED_FEATURE,
                model=request.model,
                transmitted=False,
            )
        try:
            json.dumps(dict(request.options), allow_nan=False)
        except (TypeError, ValueError):
            raise self._error(
                ErrorCategory.UNSUPPORTED_FEATURE,
                model=request.model,
                transmitted=False,
            ) from None

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
            "stream": True,
        }
        if self._include_stream_usage:
            payload["stream_options"] = {"include_usage": True}
        if request.max_output_tokens is not None:
            payload[self._output_token_field] = request.max_output_tokens
        payload.update(dict(request.options))
        return payload

    def _headers(self) -> dict[str, str]:
        api_key = os.environ.get(self._api_key_env)
        if not api_key or not api_key.strip() or "\r" in api_key or "\n" in api_key:
            raise self._error(
                ErrorCategory.AUTHENTICATION,
                model=None,
                transmitted=False,
            )
        headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        headers.update(self._extra_headers)
        return headers

    def _raise_if_cancelled(
        self,
        cancellation: CancellationToken | None,
        *,
        model: str,
        transmitted: bool,
    ) -> None:
        if cancellation is None:
            return
        try:
            cancellation.raise_if_cancelled()
            cancelled = bool(cancellation.cancelled)
        except Exception:
            cancelled = True
        if cancelled:
            raise self._error(
                ErrorCategory.CANCELLED,
                model=model,
                transmitted=transmitted,
            ) from None

    def _check_deadlines(
        self,
        request: ChatRequest,
        state: _StreamState,
        started: float,
    ) -> None:
        elapsed = self._clock() - started
        if elapsed > request.timeouts.total_seconds:
            raise self._error(
                ErrorCategory.READ_TIMEOUT,
                model=request.model,
                retryable=True,
                request_id=state.request_id,
                transmitted=True,
            )
        if not state.saw_token and elapsed > request.timeouts.first_token_seconds:
            raise self._error(
                ErrorCategory.FIRST_TOKEN_TIMEOUT,
                model=request.model,
                retryable=True,
                request_id=state.request_id,
                transmitted=True,
            )

    @staticmethod
    def _error_payload(response: Any) -> Any:
        try:
            json_method = getattr(response, "json", None)
            if callable(json_method):
                return json_method()
        except Exception:
            pass
        try:
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                if not isinstance(chunk, bytes):
                    continue
                remaining = 65_536 - size
                if remaining <= 0:
                    break
                chunks.append(chunk[:remaining])
                size += min(len(chunk), remaining)
            return json.loads(b"".join(chunks))
        except Exception:
            return None

    def _http_error(
        self,
        response: Any,
        *,
        model: str,
        headers: Mapping[str, str],
    ) -> ProviderError:
        status_code = getattr(response, "status_code", None)
        status = status_code if isinstance(status_code, int) else 0
        payload = self._error_payload(response)
        category = _body_category(status, payload)
        retryable = False
        if status == 401:
            category = ErrorCategory.AUTHENTICATION
        elif status == 403:
            category = ErrorCategory.PERMISSION
        elif status == 408:
            category = ErrorCategory.READ_TIMEOUT
            retryable = True
        elif status == 425:
            category = ErrorCategory.PROVIDER_UNAVAILABLE
            retryable = True
        elif status == 429 and category is not ErrorCategory.QUOTA_EXHAUSTED:
            category = ErrorCategory.RATE_LIMITED
            retryable = True
        elif status >= 500:
            category = ErrorCategory.PROVIDER_UNAVAILABLE
            retryable = True
        elif category is None:
            category = ErrorCategory.UNKNOWN
        return self._error(
            category,
            model=model,
            retryable=retryable,
            status_code=status or None,
            retry_after_seconds=_optional_float(headers.get("retry-after")),
            request_id=_request_id(headers),
            transmitted=True,
        )

    def _transport_error(
        self,
        error: Exception,
        *,
        request: ChatRequest,
        state: _StreamState,
    ) -> ProviderError:
        category = _transport_category(
            error,
            first_event_seen=state.saw_token,
        )
        retryable = category in {
            ErrorCategory.CONNECTIVITY,
            ErrorCategory.DNS,
            ErrorCategory.CONNECT_TIMEOUT,
            ErrorCategory.FIRST_TOKEN_TIMEOUT,
            ErrorCategory.READ_TIMEOUT,
        }
        transmitted: bool | None
        if state.response_received:
            transmitted = True
        elif category in {
            ErrorCategory.DNS,
            ErrorCategory.TLS,
            ErrorCategory.CONNECT_TIMEOUT,
        }:
            transmitted = False
        elif category in {
            ErrorCategory.FIRST_TOKEN_TIMEOUT,
            ErrorCategory.READ_TIMEOUT,
        }:
            transmitted = True
        else:
            transmitted = None
        return self._error(
            category,
            model=request.model,
            retryable=retryable,
            request_id=state.request_id,
            transmitted=transmitted,
        )

    def _parse_event(
        self,
        data: str,
        *,
        request: ChatRequest,
        state: _StreamState,
    ) -> tuple[list[StreamEvent], bool]:
        if data.strip() == "[DONE]":
            return [], True
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            raise self._error(
                ErrorCategory.MALFORMED_RESPONSE,
                model=request.model,
                request_id=state.request_id,
                transmitted=True,
            ) from None
        if not isinstance(payload, Mapping):
            raise self._error(
                ErrorCategory.MALFORMED_RESPONSE,
                model=request.model,
                request_id=state.request_id,
                transmitted=True,
            )

        payload_request_id = _safe_identifier(payload.get("id"))
        if payload_request_id is not None:
            state.request_id = payload_request_id
        resolved_model = _safe_identifier(payload.get("model"))
        if resolved_model is not None:
            state.resolved_model = resolved_model
        state.usage = _merge_usage(state.usage, payload.get("usage"))

        if payload.get("error") is not None:
            category = _body_category(0, payload.get("error")) or ErrorCategory.UNKNOWN
            raise self._error(
                category,
                model=request.model,
                request_id=state.request_id,
                transmitted=True,
            )

        choices = payload.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise self._error(
                ErrorCategory.MALFORMED_RESPONSE,
                model=request.model,
                request_id=state.request_id,
                transmitted=True,
            )
        if not choices:
            return [], False
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise self._error(
                ErrorCategory.MALFORMED_RESPONSE,
                model=request.model,
                request_id=state.request_id,
                transmitted=True,
            )
        index = choice.get("index", 0)
        if index not in {0, None}:
            raise self._error(
                ErrorCategory.MALFORMED_RESPONSE,
                model=request.model,
                request_id=state.request_id,
                transmitted=True,
            )

        finish = choice.get("finish_reason")
        if finish is not None:
            if not isinstance(finish, str):
                raise self._error(
                    ErrorCategory.MALFORMED_RESPONSE,
                    model=request.model,
                    request_id=state.request_id,
                    transmitted=True,
                )
            safe_finish = _safe_identifier(finish)
            state.provider_finish_reason = safe_finish
            state.finish_reason = _finish_reason(finish.lower())
            if state.finish_reason is FinishReason.SAFETY:
                state.refusal = True
            if state.finish_reason is FinishReason.TOOL_CALL:
                raise self._error(
                    ErrorCategory.UNSUPPORTED_FEATURE,
                    model=request.model,
                    request_id=state.request_id,
                    transmitted=True,
                )

        delta = choice.get("delta", {})
        if delta is None:
            delta = {}
        if not isinstance(delta, Mapping):
            raise self._error(
                ErrorCategory.MALFORMED_RESPONSE,
                model=request.model,
                request_id=state.request_id,
                transmitted=True,
            )
        if delta.get("tool_calls") is not None or delta.get("function_call") is not None:
            raise self._error(
                ErrorCategory.UNSUPPORTED_FEATURE,
                model=request.model,
                request_id=state.request_id,
                transmitted=True,
            )

        events: list[StreamEvent] = []
        reasoning = delta.get("reasoning_content")
        if reasoning is None:
            reasoning = delta.get("reasoning")
        if reasoning is not None:
            if not isinstance(reasoning, str):
                raise self._error(
                    ErrorCategory.MALFORMED_RESPONSE,
                    model=request.model,
                    request_id=state.request_id,
                    transmitted=True,
                )
            if reasoning:
                state.saw_token = True
                events.append(ReasoningDelta(reasoning))

        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise self._error(
                    ErrorCategory.MALFORMED_RESPONSE,
                    model=request.model,
                    request_id=state.request_id,
                    transmitted=True,
                )
            if content:
                state.saw_token = True
                state.saw_visible_text = True
                events.append(TextDelta(content))

        refusal = delta.get("refusal")
        if refusal is not None:
            if not isinstance(refusal, str):
                raise self._error(
                    ErrorCategory.MALFORMED_RESPONSE,
                    model=request.model,
                    request_id=state.request_id,
                    transmitted=True,
                )
            state.refusal = True
        return events, False

    def _terminal(self, request: ChatRequest, state: _StreamState) -> StreamEvent:
        metadata = state.metadata()
        if state.refusal:
            return Refused(
                category="safety",
                safe_message=_safe_error_message(ErrorCategory.SAFETY_REFUSAL),
                metadata=metadata,
            )
        if not state.saw_visible_text:
            raise self._error(
                ErrorCategory.EMPTY_COMPLETION,
                model=request.model,
                request_id=state.request_id,
                transmitted=True,
            )
        return Completed(metadata)

    def stream(
        self,
        request: ChatRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> Iterator[StreamEvent]:
        # This is a generator, so all secret and network work remains lazy until
        # the caller starts consuming it.
        self._authorize_remote(request)
        self._preflight(request)
        self._raise_if_cancelled(
            cancellation,
            model=request.model,
            transmitted=False,
        )
        headers = self._headers()
        payload = self._payload(request)
        started = self._clock()
        state = _StreamState(
            provider=self.identity.name,
            requested_model=request.model,
            request_id=None,
            rate_limits=None,
        )

        def emit(events: Iterable[StreamEvent]) -> Iterator[StreamEvent]:
            """Exclude synchronous consumer/TTS work while this generator is paused."""

            nonlocal started
            for event in events:
                suspended_at = self._clock()
                yield event
                started += max(0.0, self._clock() - suspended_at)

        # Defence in depth: routing authorization is checked once during
        # preflight and again at the irreversible transport boundary.
        self._authorize_remote(request)
        try:
            response_context = self._get_transport().stream(
                "POST",
                self._url,
                headers=headers,
                json=payload,
                timeouts=request.timeouts,
            )
            with response_context as response:
                state.response_received = True
                response_headers = _header_map(getattr(response, "headers", {}))
                state.request_id = _request_id(response_headers)
                state.rate_limits = _rate_limits(response_headers)
                status_code = getattr(response, "status_code", None)
                if not isinstance(status_code, int):
                    raise self._error(
                        ErrorCategory.MALFORMED_RESPONSE,
                        model=request.model,
                        request_id=state.request_id,
                        transmitted=True,
                    )
                if not 200 <= status_code < 300:
                    raise self._http_error(
                        response,
                        model=request.model,
                        headers=response_headers,
                    )
                content_type = response_headers.get("content-type", "")
                if content_type and "text/event-stream" not in content_type.lower():
                    raise self._error(
                        ErrorCategory.MALFORMED_RESPONSE,
                        model=request.model,
                        request_id=state.request_id,
                        transmitted=True,
                    )

                data_lines: list[str] = []
                data_size = 0
                terminal_emitted = False
                for raw_line in response.iter_lines():
                    self._raise_if_cancelled(
                        cancellation,
                        model=request.model,
                        transmitted=True,
                    )
                    self._check_deadlines(request, state, started)
                    if isinstance(raw_line, bytes):
                        try:
                            line = raw_line.decode("utf-8")
                        except UnicodeDecodeError:
                            raise self._error(
                                ErrorCategory.MALFORMED_RESPONSE,
                                model=request.model,
                                request_id=state.request_id,
                                transmitted=True,
                            ) from None
                    elif isinstance(raw_line, str):
                        line = raw_line
                    else:
                        raise self._error(
                            ErrorCategory.MALFORMED_RESPONSE,
                            model=request.model,
                            request_id=state.request_id,
                            transmitted=True,
                        )
                    line = line.removesuffix("\r").removeprefix("\ufeff")
                    if not line:
                        if not data_lines:
                            continue
                        events, done = self._parse_event(
                            "\n".join(data_lines),
                            request=request,
                            state=state,
                        )
                        data_lines.clear()
                        data_size = 0
                        if state.saw_token:
                            mark_first_token = getattr(response, "mark_first_token", None)
                            if callable(mark_first_token):
                                mark_first_token()
                        yield from emit(events)
                        if done:
                            yield from emit((self._terminal(request, state),))
                            terminal_emitted = True
                            break
                        continue
                    if line.startswith(":"):
                        continue
                    field, separator, value = line.partition(":")
                    if separator and value.startswith(" "):
                        value = value[1:]
                    if field != "data":
                        continue
                    data_size += len(value.encode("utf-8"))
                    if data_size > self._max_event_bytes:
                        raise self._error(
                            ErrorCategory.MALFORMED_RESPONSE,
                            model=request.model,
                            request_id=state.request_id,
                            transmitted=True,
                        )
                    data_lines.append(value)

                if terminal_emitted:
                    return
                if data_lines:
                    events, done = self._parse_event(
                        "\n".join(data_lines),
                        request=request,
                        state=state,
                    )
                    if state.saw_token:
                        mark_first_token = getattr(response, "mark_first_token", None)
                        if callable(mark_first_token):
                            mark_first_token()
                    yield from emit(events)
                    if done:
                        yield from emit((self._terminal(request, state),))
                        return
                if state.finish_reason is not FinishReason.UNKNOWN:
                    yield from emit((self._terminal(request, state),))
                    return
                raise self._error(
                    ErrorCategory.MALFORMED_RESPONSE,
                    model=request.model,
                    request_id=state.request_id,
                    transmitted=True,
                )
        except _NormalizedProviderError:
            raise
        except Exception as exc:
            raise self._transport_error(
                exc,
                request=request,
                state=state,
            ) from None

    def warm_up(self, model: str) -> None:
        """Remote warm-up is intentionally unsupported because it can cost money."""

        raise self._error(
            ErrorCategory.UNSUPPORTED_FEATURE,
            model=model,
            transmitted=False,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        transport = self._transport
        self._transport = None
        if self._owns_transport and transport is not None:
            close = getattr(transport, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    raise self._error(
                        ErrorCategory.UNKNOWN,
                        model=None,
                        transmitted=False,
                    ) from None

    def __enter__(self) -> OpenAIChatSSEAdapter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
