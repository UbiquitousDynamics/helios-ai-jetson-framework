from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from api.providers.contracts import (
    ChatMessage,
    ChatRequest,
    Completed,
    ContentOrigin,
    ErrorCategory,
    FinishReason,
    ProviderError,
    ReasoningDelta,
    Role,
    TextDelta,
)
from api.providers.ollama import OllamaAdapter
from api.streaming import CancellationController


def request(
    *,
    model: str = "test-model",
    max_output_tokens: int | None = None,
    options: dict[str, object] | None = None,
) -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=(
            ChatMessage(
                role=Role.SYSTEM,
                content="Be concise",
                origin=ContentOrigin.STATIC_INSTRUCTION,
            ),
            ChatMessage(
                role=Role.USER,
                content="Hello",
                origin=ContentOrigin.RAW_TRANSCRIPT,
            ),
        ),
        mode="talk",
        language="en",
        max_output_tokens=max_output_tokens,
        options=options or {},
    )


class FakeClient:
    def __init__(self, response: object = ()) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []
        self.close_calls = 0

    def chat(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.response

    def close(self) -> None:
        self.close_calls += 1


def test_cancellation_aborts_blocked_first_token_and_recreates_owned_client() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingChunks:
        def __iter__(self):
            return self

        def __next__(self) -> object:
            entered.set()
            release.wait(timeout=2)
            raise StopIteration

        def close(self) -> None:
            release.set()

    class BlockingClient(FakeClient):
        def chat(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return BlockingChunks()

        def close(self) -> None:
            super().close()
            release.set()

    first = BlockingClient()
    second = FakeClient([{"message": {"content": "Recovered"}, "done": True}])
    clients = iter([first, second])
    adapter = OllamaAdapter(
        "127.0.0.1:11434",
        client_factory=lambda _host: next(clients),
        cancellation_ack_timeout_seconds=0.1,
    )
    cancellation = CancellationController()
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            list(adapter.stream(request(), cancellation=cancellation))
        except BaseException as error:
            errors.append(error)

    consumer = threading.Thread(target=consume)
    consumer.start()
    assert entered.wait(timeout=1)
    cancellation.cancel()
    consumer.join(timeout=1)

    assert not consumer.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ProviderError)
    assert errors[0].category is ErrorCategory.CANCELLED
    assert first.close_calls == 1

    events = list(adapter.stream(request()))
    assert events[0] == TextDelta("Recovered")
    assert isinstance(events[1], Completed)


def test_cancellation_leaves_generator_cleanup_to_stream_worker(caplog) -> None:
    entered = threading.Event()
    release = threading.Event()
    cleanup_threads: list[str] = []

    def blocking_chunks():
        try:
            entered.set()
            release.wait(timeout=2)
            yield {"message": {"content": "stale"}, "done": True}
        finally:
            cleanup_threads.append(threading.current_thread().name)

    class BlockingClient(FakeClient):
        def chat(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return blocking_chunks()

        def close(self) -> None:
            super().close()
            release.set()

    raw_client = BlockingClient()
    adapter = OllamaAdapter(
        "127.0.0.1:11434",
        client_factory=lambda _host: raw_client,
        cancellation_ack_timeout_seconds=0.5,
    )
    cancellation = CancellationController()
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            list(adapter.stream(request(), cancellation=cancellation))
        except BaseException as error:
            errors.append(error)

    consumer = threading.Thread(target=consume, name="test-ollama-consumer")
    with caplog.at_level(logging.WARNING, logger="api.providers.ollama"):
        consumer.start()
        assert entered.wait(timeout=1)
        cancellation.cancel()
        consumer.join(timeout=1)

    assert not consumer.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ProviderError)
    assert errors[0].category is ErrorCategory.CANCELLED
    assert raw_client.close_calls == 1
    assert cleanup_threads == ["helios-ollama-stream"]
    assert not any("stream_close_failed" in record.message for record in caplog.records)


def test_unacknowledged_worker_blocks_a_second_stream_until_it_exits() -> None:
    entered = threading.Event()
    release = threading.Event()

    class StubbornChunks:
        def __iter__(self) -> StubbornChunks:
            return self

        def __next__(self) -> object:
            entered.set()
            release.wait(timeout=2)
            raise StopIteration

        def close(self) -> None:
            # Simulate a transport whose generator close cannot interrupt a
            # blocked network read. The adapter must keep its admission slot.
            return None

    class StubbornClient(FakeClient):
        def chat(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return StubbornChunks()

    first = StubbornClient()
    second = FakeClient([{"message": {"content": "Recovered"}, "done": True}])
    clients = iter([first, second])
    adapter = OllamaAdapter(
        "127.0.0.1:11434",
        client_factory=lambda _host: next(clients),
        cancellation_ack_timeout_seconds=0.05,
    )
    cancellation = CancellationController()
    errors: list[BaseException] = []

    def consume_first() -> None:
        try:
            list(adapter.stream(request(), cancellation=cancellation))
        except BaseException as error:
            errors.append(error)

    consumer = threading.Thread(target=consume_first)
    consumer.start()
    assert entered.wait(timeout=1)
    cancellation.cancel()
    consumer.join(timeout=1)

    assert not consumer.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ProviderError)
    assert errors[0].category is ErrorCategory.CANCELLED

    second_started = threading.Event()
    second_done = threading.Event()
    second_events: list[object] = []

    def consume_second() -> None:
        second_started.set()
        try:
            second_events.extend(adapter.stream(request()))
        finally:
            second_done.set()

    successor = threading.Thread(target=consume_second)
    successor.start()
    assert second_started.wait(timeout=1)
    assert not second_done.wait(timeout=0.05)

    release.set()
    successor.join(timeout=1)

    assert not successor.is_alive()
    assert isinstance(second_events[0], TextDelta)
    assert isinstance(second_events[1], Completed)


def test_constructor_is_lazy_and_normalizes_legacy_endpoint() -> None:
    created_hosts: list[str] = []
    raw_client = FakeClient()
    adapter = OllamaAdapter(
        "http://example.test:11434/api/chat",
        client_factory=lambda host: created_hosts.append(host) or raw_client,
    )

    assert adapter.host == "http://example.test:11434"
    assert adapter.identity.endpoint == "http://example.test:11434"
    assert adapter.identity.remote is True
    assert created_hosts == []

    assert adapter.client is raw_client
    assert created_hosts == ["http://example.test:11434"]


def test_stream_serializes_messages_and_options() -> None:
    raw_client = FakeClient([{"message": {"content": "OK"}, "done": True}])
    adapter = OllamaAdapter("http://127.0.0.1:11434", client=raw_client)

    events = list(
        adapter.stream(
            request(
                max_output_tokens=64,
                options={"temperature": 0.2},
            )
        )
    )

    assert raw_client.calls == [
        {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "Be concise"},
                {"role": "user", "content": "Hello"},
            ],
            "stream": True,
            "options": {"temperature": 0.2, "num_predict": 64},
        }
    ]
    assert events[0] == TextDelta("OK")
    assert isinstance(events[1], Completed)


def test_dict_chunk_emits_text_before_terminal_with_usage() -> None:
    raw_client = FakeClient(
        [
            {
                "model": "resolved-model",
                "message": {"content": "Ready.", "thinking": "private"},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 11,
                "eval_count": 3,
                "request_id": "request-123",
            }
        ]
    )
    adapter = OllamaAdapter("127.0.0.1:11434", client=raw_client)

    events = list(adapter.stream(request()))

    assert raw_client.calls == [
        {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "Be concise"},
                {"role": "user", "content": "Hello"},
            ],
            "stream": True,
        }
    ]
    assert events[0] == TextDelta("Ready.")
    assert events[1] == ReasoningDelta("private")
    assert isinstance(events[2], Completed)
    metadata = events[2].metadata
    assert metadata.provider == "ollama"
    assert metadata.requested_model == "test-model"
    assert metadata.resolved_model == "resolved-model"
    assert metadata.finish_reason is FinishReason.STOP
    assert metadata.provider_finish_reason == "stop"
    assert metadata.request_id == "request-123"
    assert metadata.usage.input_tokens == 11
    assert metadata.usage.output_tokens == 3
    assert metadata.usage.total_tokens == 14


def test_object_chunks_are_supported_and_length_is_normalized() -> None:
    raw_client = FakeClient(
        [
            SimpleNamespace(
                model="test-model",
                message=SimpleNamespace(content="Too long", thinking=""),
                done=False,
                done_reason="length",
                usage=SimpleNamespace(
                    prompt_tokens=5,
                    completion_tokens=7,
                    total_tokens=12,
                ),
            )
        ]
    )
    adapter = OllamaAdapter("127.0.0.1:11434", client=raw_client)

    events = list(adapter.stream(request()))

    assert events[0] == TextDelta("Too long")
    assert isinstance(events[1], Completed)
    assert events[1].metadata.finish_reason is FinishReason.LENGTH
    assert events[1].metadata.usage.total_tokens == 12


def test_clean_end_without_ollama_terminal_emits_unknown_completion() -> None:
    raw_client = FakeClient([{"message": {"content": "Partial"}}])
    adapter = OllamaAdapter("127.0.0.1:11434", client=raw_client)

    events = list(adapter.stream(request()))

    assert events[0] == TextDelta("Partial")
    assert isinstance(events[1], Completed)
    assert events[1].metadata.finish_reason is FinishReason.UNKNOWN


def test_warm_up_uses_non_streaming_empty_user_message() -> None:
    raw_client = FakeClient(response={"done": True})
    adapter = OllamaAdapter("127.0.0.1:11434", client=raw_client)

    adapter.warm_up("warm-model")

    assert raw_client.calls == [
        {
            "model": "warm-model",
            "messages": [{"role": "user", "content": ""}],
            "stream": False,
        }
    ]


def test_call_time_transport_error_is_sanitized_and_retryable() -> None:
    class FailingClient(FakeClient):
        def chat(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            raise OSError("secret endpoint and response body")

    adapter = OllamaAdapter("127.0.0.1:11434", client=FailingClient())

    with pytest.raises(ProviderError) as captured:
        list(adapter.stream(request()))

    error = captured.value
    assert error.category is ErrorCategory.CONNECTIVITY
    assert error.retryable_same_provider is True
    assert error.transmitted is None
    assert "secret" not in str(error)
    assert error.__cause__ is None


def test_iteration_time_transport_error_is_sanitized_and_retryable() -> None:
    def interrupted() -> Any:
        yield {"message": {"content": "Buffered"}, "done": False}
        raise TimeoutError("secret streamed response body")

    adapter = OllamaAdapter(
        "127.0.0.1:11434",
        client=FakeClient(interrupted()),
    )
    events = iter(adapter.stream(request()))

    assert next(events) == TextDelta("Buffered")
    with pytest.raises(ProviderError) as captured:
        next(events)

    error = captured.value
    assert error.category is ErrorCategory.READ_TIMEOUT
    assert error.retryable_same_provider is True
    assert error.transmitted is True
    assert "secret" not in str(error)
    assert error.__cause__ is None


@pytest.mark.parametrize(
    ("status_code", "category", "retryable"),
    [
        (400, ErrorCategory.UNKNOWN, False),
        (401, ErrorCategory.AUTHENTICATION, False),
        (403, ErrorCategory.PERMISSION, False),
        (408, ErrorCategory.READ_TIMEOUT, True),
        (425, ErrorCategory.PROVIDER_UNAVAILABLE, True),
        (429, ErrorCategory.RATE_LIMITED, True),
        (500, ErrorCategory.PROVIDER_UNAVAILABLE, True),
        (503, ErrorCategory.PROVIDER_UNAVAILABLE, True),
    ],
)
def test_http_status_classification(
    status_code: int,
    category: ErrorCategory,
    retryable: bool,
) -> None:
    class HTTPFailure(Exception):
        def __init__(self) -> None:
            super().__init__("secret provider response")
            self.status_code = status_code
            self.response = SimpleNamespace(
                status_code=status_code,
                headers={"Retry-After": "2.5", "X-Request-ID": "req-safe"},
            )

    class FailingClient(FakeClient):
        def chat(self, **kwargs: object) -> object:
            raise HTTPFailure()

    adapter = OllamaAdapter("127.0.0.1:11434", client=FailingClient())

    with pytest.raises(ProviderError) as captured:
        list(adapter.stream(request()))

    error = captured.value
    assert error.category is category
    assert error.status_code == status_code
    assert error.retryable_same_provider is retryable
    assert error.retry_after_seconds == 2.5
    assert error.request_id == "req-safe"
    assert "secret" not in str(error)
    assert error.__cause__ is None


def test_factory_error_is_sanitized_without_constructing_eagerly() -> None:
    def failing_factory(_host: str) -> object:
        raise RuntimeError("secret package or host details")

    adapter = OllamaAdapter("127.0.0.1:11434", client_factory=failing_factory)

    with pytest.raises(ProviderError) as captured:
        _ = adapter.client

    error = captured.value
    assert error.category is ErrorCategory.UNKNOWN
    assert error.transmitted is False
    assert "secret" not in str(error)
    assert error.__cause__ is None


def test_owned_client_is_closed_once_without_being_created_by_close() -> None:
    raw_client = FakeClient()
    created: list[FakeClient] = []
    adapter = OllamaAdapter(
        "127.0.0.1:11434",
        client_factory=lambda _host: created.append(raw_client) or raw_client,
    )

    adapter.close()
    adapter.close()

    assert created == []
    assert raw_client.close_calls == 0

    second = OllamaAdapter(
        "127.0.0.1:11434",
        client_factory=lambda _host: created.append(raw_client) or raw_client,
    )
    assert second.client is raw_client
    second.close()
    second.close()

    assert raw_client.close_calls == 1


def test_injected_client_remains_caller_owned() -> None:
    raw_client = FakeClient()
    adapter = OllamaAdapter("127.0.0.1:11434", client=raw_client)

    adapter.close()
    adapter.close()

    assert raw_client.close_calls == 0
