import json
import queue
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

import pytest

from api.providers.contracts import (
    ChatMessage,
    ChatRequest,
    Completed,
    ContentOrigin,
    ErrorCategory,
    FinishReason,
    PrivacyLevel,
    ProviderCapabilities,
    ProviderError,
    ReasoningDelta,
    Refused,
    Role,
    TextDelta,
    Timeouts,
)
from api.providers.openai_chat_sse import (
    OpenAIChatSSEAdapter,
    _TimedHttpxResponse,
)


class FakeResponse:
    def __init__(
        self,
        lines: Iterable[str | bytes | Exception] = (),
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        error_payload: Any = None,
    ) -> None:
        self.lines = list(lines)
        self.status_code = status_code
        self.headers = dict(headers or {"content-type": "text/event-stream"})
        self.error_payload = error_payload
        self.first_token_marks = 0

    def iter_lines(self) -> Iterable[str | bytes]:
        for line in self.lines:
            if isinstance(line, Exception):
                raise line
            yield line

    def json(self) -> Any:
        if self.error_payload is None:
            raise ValueError("no JSON error body")
        return self.error_payload

    def mark_first_token(self) -> None:
        self.first_token_marks += 1


class FakeResponseContext:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.entered = False
        self.exited = False

    def __enter__(self) -> FakeResponse:
        self.entered = True
        return self.response

    def __exit__(self, *args: object) -> None:
        self.exited = True


class FakeTransport:
    def __init__(
        self,
        response: FakeResponse | None = None,
        *,
        open_error: Exception | None = None,
    ) -> None:
        self.response = response or FakeResponse()
        self.context = FakeResponseContext(self.response)
        self.open_error = open_error
        self.calls: list[dict[str, Any]] = []
        self.close_calls = 0

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, Any],
        timeouts: Any,
    ) -> FakeResponseContext:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json": dict(json),
                "timeouts": timeouts,
            }
        )
        if self.open_error is not None:
            raise self.open_error
        return self.context

    def close(self) -> None:
        self.close_calls += 1


def _request(
    *,
    privacy: PrivacyLevel = PrivacyLevel.REMOTE_ALLOWED,
    remote_authorized: bool = True,
    redacted: bool = False,
    options: Mapping[str, Any] | None = None,
    max_output_tokens: int | None = 64,
    required_features: frozenset[str] = frozenset(),
    messages: tuple[ChatMessage, ...] | None = None,
) -> ChatRequest:
    return ChatRequest(
        model="remote-model",
        messages=messages
        or (
            ChatMessage(
                Role.USER,
                "Dimmi qualcosa.",
                origin=ContentOrigin.RAW_TRANSCRIPT,
                redacted=redacted,
            ),
        ),
        mode="talk",
        language="it",
        privacy=privacy,
        remote_authorized=remote_authorized,
        max_output_tokens=max_output_tokens,
        required_features=required_features,
        options=options or {},
    )


def _adapter(
    monkeypatch: pytest.MonkeyPatch,
    response: FakeResponse | None = None,
    **kwargs: Any,
) -> tuple[OpenAIChatSSEAdapter, FakeTransport]:
    monkeypatch.setenv("TEST_REMOTE_API_KEY", "test-secret-key")
    transport = FakeTransport(response)
    adapter = OpenAIChatSSEAdapter(
        "test-provider",
        "https://provider.invalid/openai/v1/",
        "TEST_REMOTE_API_KEY",
        transport=transport,
        **kwargs,
    )
    return adapter, transport


def _event(payload: Mapping[str, Any]) -> list[str]:
    return [f"data: {json.dumps(payload)}", ""]


def _successful_lines(text: str = "Ciao.") -> list[str]:
    return [
        *_event(
            {
                "id": "request_1",
                "model": "resolved-model",
                "choices": [{"index": 0, "delta": {"content": text}}],
            }
        ),
        *_event(
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            }
        ),
        "data: [DONE]",
        "",
    ]


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_consumer_pause_is_not_counted_as_provider_total_time(monkeypatch):
    clock = ManualClock()
    response = FakeResponse(_successful_lines())
    adapter, _transport = _adapter(monkeypatch, response, clock=clock)
    request = replace(
        _request(),
        timeouts=Timeouts(
            connect_seconds=1,
            first_token_seconds=1,
            read_seconds=1,
            total_seconds=1,
        ),
    )
    stream = adapter.stream(request)

    assert next(stream) == TextDelta("Ciao.")
    clock.value += 30  # synchronous Piper/TTS or another slow consumer
    remaining = list(stream)

    assert any(isinstance(event, Completed) for event in remaining)
    assert response.first_token_marks >= 1


def test_timed_httpx_response_switches_from_first_token_to_read_timeout():
    observed_timeouts: list[float] = []

    def wait_for_item(
        mailbox: queue.Queue[Any],
        timeout: float,
    ) -> Any:
        observed_timeouts.append(timeout)
        return mailbox.get(timeout=1)

    response = FakeResponse(["first", "second"])
    timed = _TimedHttpxResponse(
        response,
        timeouts=Timeouts(
            connect_seconds=1,
            first_token_seconds=2,
            read_seconds=7,
            total_seconds=20,
        ),
        opening_elapsed=0,
        clock=lambda: 0.0,
        wait_for_item=wait_for_item,
    )
    lines = iter(timed.iter_lines())

    assert next(lines) == "first"
    timed.mark_first_token()
    assert next(lines) == "second"
    timed.close()

    assert observed_timeouts[:2] == [2, 7]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://provider.invalid/v1",
        "https://user:password@provider.invalid/v1",
        "https://provider.invalid/v1?api_key=secret",
        "https://provider.invalid/v1#fragment",
        "not-a-url",
    ],
)
def test_endpoint_validation_rejects_unsafe_remote_urls(endpoint):
    with pytest.raises(ValueError):
        OpenAIChatSSEAdapter("provider", endpoint, "API_KEY", transport=FakeTransport())


def test_static_extra_headers_cannot_carry_credentials():
    with pytest.raises(ValueError):
        OpenAIChatSSEAdapter(
            "provider",
            "https://provider.invalid/v1",
            "API_KEY",
            transport=FakeTransport(),
            extra_headers={"X-API-Key": "embedded-secret"},
        )

    with pytest.raises(ValueError):
        OpenAIChatSSEAdapter(
            "provider",
            "https://provider.invalid/v1",
            "API_KEY",
            transport=FakeTransport(),
            allowed_options=frozenset({"model"}),
        )


def test_adapter_is_lazy_and_serializes_only_the_certified_request_surface(monkeypatch):
    monkeypatch.delenv("TEST_REMOTE_API_KEY", raising=False)
    created: list[FakeTransport] = []

    def factory():
        transport = FakeTransport(FakeResponse(_successful_lines()))
        created.append(transport)
        return transport

    adapter = OpenAIChatSSEAdapter(
        "test-provider",
        "https://provider.invalid/v1",
        "TEST_REMOTE_API_KEY",
        transport_factory=factory,
    )
    stream = adapter.stream(
        _request(
            options={
                "temperature": 0.2,
                "reasoning_effort": "low",
                "include_reasoning": False,
            }
        )
    )
    assert created == []

    monkeypatch.setenv("TEST_REMOTE_API_KEY", "rotated-key")
    events = list(stream)

    assert len(created) == 1
    call = created[0].calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://provider.invalid/v1/chat/completions"
    assert call["headers"] == {
        "Accept": "text/event-stream",
        "Authorization": "Bearer rotated-key",
        "Content-Type": "application/json",
    }
    assert call["json"] == {
        "model": "remote-model",
        "messages": [{"role": "user", "content": "Dimmi qualcosa."}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 64,
        "temperature": 0.2,
        "reasoning_effort": "low",
        "include_reasoning": False,
    }
    assert isinstance(events[-1], Completed)


def test_multiline_sse_comments_reasoning_usage_and_rate_metadata(monkeypatch):
    lines = [
        ": keep-alive",
        "event: message",
        'data: {"id":"request_2","model":"resolved-model",',
        'data: "choices":[{"index":0,"delta":{"reasoning_content":"private"}}]}',
        "",
        *_event(
            {
                "choices": [{"index": 0, "delta": {"content": "Risposta "}}],
            }
        ),
        *_event(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "visibile."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "prompt_tokens_details": {"cached_tokens": 3},
                    "completion_tokens": 5,
                    "completion_tokens_details": {"reasoning_tokens": 2},
                    "total_tokens": 13,
                },
            }
        ),
        "data: [DONE]",
        "",
    ]
    response = FakeResponse(
        lines,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "X-Request-ID": "header_request",
            "X-RateLimit-Remaining-Requests": "27",
            "X-RateLimit-Remaining-Tokens": "900",
            "X-RateLimit-Reset-Requests": "2s",
            "Retry-After": "1.5",
        },
    )
    adapter, _ = _adapter(monkeypatch, response)

    events = list(adapter.stream(_request(required_features=frozenset({"reasoning"}))))

    assert events[:-1] == [
        ReasoningDelta("private"),
        TextDelta("Risposta "),
        TextDelta("visibile."),
    ]
    completed = events[-1]
    assert isinstance(completed, Completed)
    metadata = completed.metadata
    assert metadata.provider == "test-provider"
    assert metadata.requested_model == "remote-model"
    assert metadata.resolved_model == "resolved-model"
    assert metadata.request_id == "request_2"
    assert metadata.finish_reason is FinishReason.STOP
    assert metadata.provider_finish_reason == "stop"
    assert metadata.usage.input_tokens == 8
    assert metadata.usage.cached_input_tokens == 3
    assert metadata.usage.output_tokens == 5
    assert metadata.usage.reasoning_tokens == 2
    assert metadata.usage.total_tokens == 13
    assert metadata.rate_limits is not None
    assert metadata.rate_limits.remaining_requests == 27
    assert metadata.rate_limits.remaining_tokens == 900
    assert metadata.rate_limits.reset_requests == "2s"
    assert metadata.rate_limits.retry_after_seconds == 1.5


@pytest.mark.parametrize(
    ("privacy", "authorized", "redacted"),
    [
        (PrivacyLevel.LOCAL_ONLY, True, False),
        ("local_only", True, False),
        ("invalid-privacy", True, False),
        (PrivacyLevel.REMOTE_ALLOWED, False, False),
        (PrivacyLevel.REMOTE_ALLOWED, "false", False),
        (PrivacyLevel.REMOTE_REDACTED, True, False),
        ("remote_redacted", True, "true"),
    ],
)
def test_remote_dispatch_requires_both_gates_and_valid_redaction(
    monkeypatch,
    privacy,
    authorized,
    redacted,
):
    adapter, transport = _adapter(
        monkeypatch,
        FakeResponse(_successful_lines()),
    )

    with pytest.raises(ProviderError) as captured:
        list(
            adapter.stream(
                _request(
                    privacy=privacy,
                    remote_authorized=authorized,
                    redacted=redacted,
                )
            )
        )

    assert captured.value.category is ErrorCategory.PRIVACY_BLOCKED
    assert captured.value.transmitted is False
    assert transport.calls == []


def test_valid_string_privacy_and_origins_are_coerced_without_adding_policy(monkeypatch):
    messages = (
        ChatMessage(
            Role.USER,
            "Documento autorizzato dal coordinatore.",
            origin="local_document",
            redacted=False,
        ),
    )
    adapter, transport = _adapter(
        monkeypatch,
        FakeResponse(_successful_lines()),
    )

    events = list(
        adapter.stream(
            _request(
                privacy="remote_allowed",
                remote_authorized=True,
                messages=messages,
            )
        )
    )

    assert isinstance(events[-1], Completed)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("origin", ["not-an-origin", object()])
def test_invalid_content_origin_fails_closed_before_transport(monkeypatch, origin):
    messages = (
        ChatMessage(
            Role.USER,
            "Sensitive",
            origin=origin,
            redacted=True,
        ),
    )
    adapter, transport = _adapter(
        monkeypatch,
        FakeResponse(_successful_lines()),
    )

    with pytest.raises(ProviderError) as captured:
        list(adapter.stream(_request(messages=messages)))

    assert captured.value.category is ErrorCategory.PRIVACY_BLOCKED
    assert captured.value.transmitted is False
    assert transport.calls == []


def test_redacted_mode_allows_static_instructions_and_redacted_content(monkeypatch):
    messages = (
        ChatMessage(
            Role.SYSTEM,
            "Rispondi brevemente.",
            origin="static_instruction",
        ),
        ChatMessage(
            Role.USER,
            "[NAME] chiede aiuto.",
            origin="raw_transcript",
            redacted=True,
        ),
    )
    adapter, transport = _adapter(
        monkeypatch,
        FakeResponse(_successful_lines()),
    )

    list(
        adapter.stream(
            _request(
                privacy=PrivacyLevel.REMOTE_REDACTED,
                redacted=True,
                messages=messages,
            )
        )
    )

    assert transport.calls[0]["json"]["messages"] == [
        {"role": "system", "content": "Rispondi brevemente."},
        {"role": "user", "content": "[NAME] chiede aiuto."},
    ]


def test_missing_key_fails_before_transport_construction(monkeypatch):
    monkeypatch.delenv("MISSING_REMOTE_KEY", raising=False)
    factories: list[object] = []

    def factory():
        factories.append(object())
        return FakeTransport(FakeResponse(_successful_lines()))

    adapter = OpenAIChatSSEAdapter(
        "provider",
        "https://provider.invalid/v1",
        "MISSING_REMOTE_KEY",
        transport_factory=factory,
    )

    with pytest.raises(ProviderError) as captured:
        list(adapter.stream(_request()))

    assert captured.value.category is ErrorCategory.AUTHENTICATION
    assert captured.value.transmitted is False
    assert factories == []


@pytest.mark.parametrize(
    ("status", "body", "category", "retryable"),
    [
        (401, {"error": {"message": "secret credential"}}, ErrorCategory.AUTHENTICATION, False),
        (403, {"error": {"message": "secret policy"}}, ErrorCategory.PERMISSION, False),
        (408, {}, ErrorCategory.READ_TIMEOUT, True),
        (425, {}, ErrorCategory.PROVIDER_UNAVAILABLE, True),
        (429, {"error": {"code": "rate_limit"}}, ErrorCategory.RATE_LIMITED, True),
        (
            429,
            {"error": {"code": "insufficient_quota", "message": "secret balance"}},
            ErrorCategory.QUOTA_EXHAUSTED,
            False,
        ),
        (
            400,
            {"error": {"code": "context_length_exceeded", "message": "secret prompt"}},
            ErrorCategory.CONTEXT_OVERFLOW,
            False,
        ),
        (503, {"error": {"message": "secret incident"}}, ErrorCategory.PROVIDER_UNAVAILABLE, True),
    ],
)
def test_http_failures_are_normalized_without_body_leakage(
    monkeypatch,
    status,
    body,
    category,
    retryable,
):
    response = FakeResponse(
        status_code=status,
        headers={
            "content-type": "application/json",
            "retry-after": "3",
            "x-request-id": "safe_request",
        },
        error_payload=body,
    )
    adapter, transport = _adapter(monkeypatch, response)

    with pytest.raises(ProviderError) as captured:
        list(adapter.stream(_request()))

    error = captured.value
    assert error.category is category
    assert error.status_code == status
    assert error.retryable_same_provider is retryable
    assert error.retry_after_seconds == 3
    assert error.request_id == "safe_request"
    assert error.transmitted is True
    assert "secret" not in str(error).lower()
    assert error.__cause__ is None
    assert len(transport.calls) == 1
    assert transport.context.exited is True


class ConnectTimeoutWithSecret(Exception):
    pass


class NameResolutionErrorWithSecret(Exception):
    pass


class SSLCertificateErrorWithSecret(Exception):
    pass


class ReadTimeoutWithSecret(Exception):
    pass


@pytest.mark.parametrize(
    ("exception", "category", "retryable", "transmitted"),
    [
        (
            ConnectTimeoutWithSecret("secret endpoint"),
            ErrorCategory.CONNECT_TIMEOUT,
            True,
            False,
        ),
        (
            NameResolutionErrorWithSecret("secret hostname"),
            ErrorCategory.DNS,
            True,
            False,
        ),
        (
            SSLCertificateErrorWithSecret("secret certificate"),
            ErrorCategory.TLS,
            False,
            False,
        ),
        (
            ProviderError(
                ErrorCategory.AUTHENTICATION,
                "secret provider SDK message",
                provider="raw-sdk",
            ),
            ErrorCategory.UNKNOWN,
            False,
            None,
        ),
    ],
)
def test_opening_transport_failures_are_sanitized(
    monkeypatch,
    exception,
    category,
    retryable,
    transmitted,
):
    monkeypatch.setenv("TEST_REMOTE_API_KEY", "secret-key")
    transport = FakeTransport(open_error=exception)
    adapter = OpenAIChatSSEAdapter(
        "provider",
        "https://provider.invalid/v1",
        "TEST_REMOTE_API_KEY",
        transport=transport,
    )

    with pytest.raises(ProviderError) as captured:
        list(adapter.stream(_request()))

    error = captured.value
    assert error.category is category
    assert error.retryable_same_provider is retryable
    assert error.transmitted is transmitted
    assert "secret" not in str(error).lower()
    assert error.__cause__ is None
    assert len(transport.calls) == 1


def test_stream_timeout_after_provider_event_is_a_read_timeout(monkeypatch):
    response = FakeResponse(
        [
            *_event(
                {
                    "choices": [{"index": 0, "delta": {"content": "Parziale"}}],
                }
            ),
            ReadTimeoutWithSecret("secret response"),
        ]
    )
    adapter, transport = _adapter(monkeypatch, response)
    stream = adapter.stream(_request())

    assert next(stream) == TextDelta("Parziale")
    with pytest.raises(ProviderError) as captured:
        next(stream)

    assert captured.value.category is ErrorCategory.READ_TIMEOUT
    assert captured.value.retryable_same_provider is True
    assert captured.value.transmitted is True
    assert "secret" not in str(captured.value).lower()
    assert transport.context.exited is True


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(["data: {bad json", ""]),
        FakeResponse([b"data: \xff", b""]),
        FakeResponse(
            _successful_lines(),
            headers={"content-type": "application/json"},
        ),
        FakeResponse(_event({"choices": [{"index": 0, "delta": {"content": "truncated"}}]})),
        FakeResponse(
            [
                *_event(
                    {
                        "choices": [
                            {"index": 0, "delta": {"content": "one"}},
                            {"index": 1, "delta": {"content": "two"}},
                        ]
                    }
                ),
                "data: [DONE]",
                "",
            ]
        ),
    ],
)
def test_malformed_or_truncated_stream_is_rejected_and_closed(monkeypatch, response):
    adapter, transport = _adapter(monkeypatch, response)

    with pytest.raises(ProviderError) as captured:
        list(adapter.stream(_request()))

    assert captured.value.category is ErrorCategory.MALFORMED_RESPONSE
    assert captured.value.transmitted is True
    assert transport.context.exited is True


def test_refusal_is_terminal_and_provider_text_is_not_exposed(monkeypatch):
    response = FakeResponse(
        [
            *_event(
                {
                    "id": "refusal_1",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"refusal": "Your secret input violates X"},
                            "finish_reason": "content_filter",
                        }
                    ],
                }
            ),
            "data: [DONE]",
            "",
        ]
    )
    adapter, _ = _adapter(monkeypatch, response)

    events = list(adapter.stream(_request()))

    assert len(events) == 1
    refused = events[0]
    assert isinstance(refused, Refused)
    assert refused.category == "safety"
    assert refused.metadata.finish_reason is FinishReason.SAFETY
    assert refused.metadata.request_id == "refusal_1"
    assert "secret" not in (refused.safe_message or "").lower()


def test_empty_completion_and_stream_error_are_normalized(monkeypatch):
    empty_adapter, _ = _adapter(
        monkeypatch,
        FakeResponse(
            [
                *_event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
                "data: [DONE]",
                "",
            ]
        ),
    )
    with pytest.raises(ProviderError) as empty:
        list(empty_adapter.stream(_request()))
    assert empty.value.category is ErrorCategory.EMPTY_COMPLETION

    error_adapter, _ = _adapter(
        monkeypatch,
        FakeResponse(
            [
                *_event(
                    {
                        "error": {
                            "code": "context_length_exceeded",
                            "message": "secret prompt",
                        }
                    }
                )
            ]
        ),
    )
    with pytest.raises(ProviderError) as stream_error:
        list(error_adapter.stream(_request()))
    assert stream_error.value.category is ErrorCategory.CONTEXT_OVERFLOW
    assert "secret" not in str(stream_error.value).lower()


class CancelAfterChecks:
    def __init__(self, checks: int) -> None:
        self._remaining = checks

    @property
    def cancelled(self) -> bool:
        return self._remaining <= 0

    def raise_if_cancelled(self) -> None:
        self._remaining -= 1
        if self.cancelled:
            raise RuntimeError("secret cancellation detail")


def test_cancellation_is_sanitized_and_closes_an_open_stream(monkeypatch):
    response = FakeResponse(_successful_lines())
    adapter, transport = _adapter(monkeypatch, response)

    with pytest.raises(ProviderError) as captured:
        list(adapter.stream(_request(), cancellation=CancelAfterChecks(2)))

    assert captured.value.category is ErrorCategory.CANCELLED
    assert captured.value.transmitted is True
    assert "secret" not in str(captured.value).lower()
    assert captured.value.__cause__ is None
    assert transport.context.exited is True


def test_preflight_rejects_unknown_options_features_and_system_role(monkeypatch):
    adapter, transport = _adapter(
        monkeypatch,
        FakeResponse(_successful_lines()),
        capabilities=ProviderCapabilities(
            supports_system_messages=False,
            features=frozenset({"streaming"}),
        ),
    )
    system_request = _request(
        messages=(
            ChatMessage(
                Role.SYSTEM,
                "System",
                origin=ContentOrigin.STATIC_INSTRUCTION,
            ),
            ChatMessage(
                Role.USER,
                "User",
                origin=ContentOrigin.RAW_TRANSCRIPT,
            ),
        )
    )
    requests = [
        system_request,
        _request(options={"provider_magic": True}),
        _request(required_features=frozenset({"tools"})),
    ]

    for request in requests:
        with pytest.raises(ProviderError) as captured:
            list(adapter.stream(request))
        assert captured.value.category is ErrorCategory.UNSUPPORTED_FEATURE
        assert captured.value.transmitted is False
    assert transport.calls == []


def test_no_retry_and_idempotent_transport_ownership(monkeypatch):
    response = FakeResponse(status_code=503, error_payload={"error": {}})
    adapter, transport = _adapter(
        monkeypatch,
        response,
        owns_transport=True,
    )

    with pytest.raises(ProviderError):
        list(adapter.stream(_request()))
    assert len(transport.calls) == 1

    adapter.close()
    adapter.close()
    assert transport.close_calls == 1


def test_injected_transport_is_caller_owned_by_default(monkeypatch):
    adapter, transport = _adapter(
        monkeypatch,
        FakeResponse(_successful_lines()),
    )
    list(adapter.stream(_request()))

    adapter.close()
    adapter.close()

    assert transport.close_calls == 0


def test_factory_transport_is_owned_lazy_and_closed_once(monkeypatch):
    monkeypatch.setenv("TEST_REMOTE_API_KEY", "test-key")
    transports: list[FakeTransport] = []

    def factory():
        transport = FakeTransport(FakeResponse(_successful_lines()))
        transports.append(transport)
        return transport

    adapter = OpenAIChatSSEAdapter(
        "provider",
        "https://provider.invalid/v1",
        "TEST_REMOTE_API_KEY",
        transport_factory=factory,
    )
    assert transports == []
    list(adapter.stream(_request()))
    assert len(transports) == 1

    adapter.close()
    adapter.close()

    assert transports[0].close_calls == 1


def test_remote_warm_up_is_never_billable(monkeypatch):
    adapter, transport = _adapter(monkeypatch)

    with pytest.raises(ProviderError) as captured:
        adapter.warm_up("model")

    assert captured.value.category is ErrorCategory.UNSUPPORTED_FEATURE
    assert captured.value.transmitted is False
    assert transport.calls == []
