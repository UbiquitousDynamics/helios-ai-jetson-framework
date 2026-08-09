from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from api.budget import BudgetLedger, BudgetLimits
from api.catalog import ModelPrice
from api.health import HealthTracker
from api.metrics import SafeMetricsRecorder
from api.providers.contracts import (
    ChatMessage,
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
    Role,
    TextDelta,
)
from api.routing import ProviderRegistry, ProviderTarget
from api.streaming import (
    CancellationController,
    ExecutionTarget,
    SpeechReplayUnsafeError,
    StreamingResponseCoordinator,
)


def completion(provider: str, model: str = "model") -> Completed:
    return Completed(
        CompletionMetadata(
            provider=provider,
            requested_model=model,
            finish_reason=FinishReason.STOP,
        )
    )


def request() -> ChatRequest:
    return ChatRequest(
        model="placeholder",
        messages=(
            ChatMessage(
                Role.USER,
                "Emilia, answer",
                origin=ContentOrigin.RAW_TRANSCRIPT,
            ),
        ),
        mode="talk",
        language="en",
    )


class FakeProvider:
    def __init__(
        self,
        name: str,
        streams: list[object],
        *,
        expected_cancellation: object | None = None,
    ) -> None:
        self.name = name
        self.streams = iter(streams)
        self.calls: list[ChatRequest] = []
        self.expected_cancellation = expected_cancellation

    @property
    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(self.name, "http://127.0.0.1", remote=False)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def stream(
        self,
        chat_request: ChatRequest,
        *,
        cancellation: object | None = None,
    ) -> Iterable[object]:
        assert cancellation is self.expected_cancellation
        self.calls.append(chat_request)
        stream = next(self.streams)
        if isinstance(stream, Exception):
            raise stream
        return stream  # type: ignore[return-value]

    def warm_up(self, model: str) -> None:
        assert model

    def close(self) -> None:
        return None


def target(name: str, *, remote: bool = False) -> ProviderTarget:
    return ProviderTarget(
        name=name,
        provider=name,
        model="model",
        remote=remote,
        modes=frozenset({"talk"}),
    )


def coordinator(*providers: FakeProvider, **kwargs: object) -> StreamingResponseCoordinator:
    registry = ProviderRegistry()
    for provider in providers:
        registry.register_instance(provider.name, provider)
    return StreamingResponseCoordinator(registry, **kwargs)


def interrupted_before_speech() -> Iterable[object]:
    yield TextDelta("unspoken partial")
    raise ProviderError(
        ErrorCategory.READ_TIMEOUT,
        "stream interrupted",
        provider="first",
        model="model",
        transmitted=True,
    )


def interrupted_after_speech() -> Iterable[object]:
    yield TextDelta("First sentence.")
    raise ProviderError(
        ErrorCategory.READ_TIMEOUT,
        "stream interrupted",
        provider="first",
        model="model",
        retryable_same_provider=True,
        transmitted=True,
    )


def test_unspoken_partial_output_is_discarded_before_fallback() -> None:
    first = FakeProvider("first", [interrupted_before_speech()])
    second = FakeProvider(
        "second",
        [[TextDelta("Ready."), completion("second")]],
    )
    spoken: list[str] = []
    runner = coordinator(first, second, retry_wait=0)

    result = runner.run(
        request(),
        (
            ExecutionTarget(target("first")),
            ExecutionTarget(target("second")),
        ),
        speak=spoken.append,
    )

    assert result.text == "Ready."
    assert result.target.name == "second"
    assert result.attempts == 2
    assert spoken == ["Ready."]


def test_retrying_same_provider_is_allowed_only_before_speech() -> None:
    transient = ProviderError(
        ErrorCategory.CONNECTIVITY,
        "temporarily unavailable",
        provider="first",
        model="model",
        retryable_same_provider=True,
    )
    provider = FakeProvider(
        "first",
        [transient, [TextDelta("Recovered."), completion("first")]],
    )
    sleeps: list[float] = []
    runner = coordinator(provider, retry_wait=0.25, sleep=sleeps.append)

    result = runner.run(
        request(),
        (ExecutionTarget(target("first"), retry_attempts=2),),
    )

    assert result.text == "Recovered."
    assert result.attempts == 2
    assert sleeps == [0.25]


def test_exhausted_route_reports_total_attempt_count() -> None:
    failures = [
        ProviderError(
            ErrorCategory.CONNECTIVITY,
            "temporarily unavailable",
            provider="first",
            model="model",
            retryable_same_provider=True,
        )
        for _ in range(2)
    ]
    provider = FakeProvider("first", failures)
    runner = coordinator(provider, retry_wait=0)

    with pytest.raises(ProviderError) as captured:
        runner.run(
            request(),
            (ExecutionTarget(target("first"), retry_attempts=2),),
        )

    assert captured.value.attempts == 2


def test_stream_never_retries_or_falls_back_after_speech_commit() -> None:
    first = FakeProvider("first", [interrupted_after_speech()])
    second = FakeProvider(
        "second",
        [[TextDelta("Duplicate."), completion("second")]],
    )
    spoken: list[str] = []
    runner = coordinator(first, second, retry_wait=0)

    with pytest.raises(SpeechReplayUnsafeError):
        runner.run(
            request(),
            (
                ExecutionTarget(target("first"), retry_attempts=3),
                ExecutionTarget(target("second")),
            ),
            speak=spoken.append,
        )

    assert spoken == ["First sentence."]
    assert len(first.calls) == 1
    assert second.calls == []


def test_sentence_streaming_does_not_split_at_commas() -> None:
    provider = FakeProvider(
        "first",
        [
            [
                ReasoningDelta("private chain"),
                TextDelta("Hello, "),
                TextDelta("crew"),
                completion("first"),
            ]
        ],
    )
    spoken: list[str] = []

    result = coordinator(provider).run(
        request(),
        (ExecutionTarget(target("first")),),
        speak=spoken.append,
    )

    assert result.text == "Hello, crew"
    assert "private" not in result.text
    assert spoken == ["Hello, crew"]


def test_multi_sentence_delta_is_spoken_before_remote_completion() -> None:
    spoken: list[str] = []

    def stream() -> Iterable[object]:
        yield TextDelta("Prima frase. Seconda frase.")
        assert spoken == ["Prima frase.", "Seconda frase."]
        yield completion("first")

    provider = FakeProvider("first", [stream()])

    result = coordinator(provider).run(
        request(),
        (ExecutionTarget(target("first")),),
        speak=spoken.append,
    )

    assert result.text == "Prima frase. Seconda frase."
    assert spoken == ["Prima frase.", "Seconda frase."]


def test_before_first_speech_runs_once_immediately_before_first_fragment() -> None:
    provider = FakeProvider(
        "first",
        [[TextDelta("First sentence. Second sentence."), completion("first")]],
    )
    events: list[str] = []

    result = coordinator(provider).run(
        request(),
        (ExecutionTarget(target("first")),),
        before_first_speech=lambda: events.append("before"),
        speak=lambda text: events.append(f"speak:{text}"),
    )

    assert result.text == "First sentence. Second sentence."
    assert events == [
        "before",
        "speak:First sentence.",
        "speak:Second sentence.",
    ]


def test_cancellation_after_first_fragment_discards_queued_sentence_and_is_terminal() -> None:
    cancellation = CancellationController()
    first = FakeProvider(
        "first",
        [[TextDelta("First sentence. Second sentence."), completion("first")]],
        expected_cancellation=cancellation,
    )
    fallback = FakeProvider(
        "fallback",
        [[TextDelta("Duplicate."), completion("fallback")]],
        expected_cancellation=cancellation,
    )
    spoken: list[str] = []

    def cancel_after_speech(text: str) -> None:
        spoken.append(text)
        cancellation.cancel()

    with pytest.raises(SpeechReplayUnsafeError) as captured:
        coordinator(first, fallback, retry_wait=0).run(
            request(),
            (
                ExecutionTarget(target("first"), retry_attempts=3),
                ExecutionTarget(target("fallback")),
            ),
            speak=cancel_after_speech,
            cancellation=cancellation,
        )

    assert captured.value.error.category is ErrorCategory.CANCELLED
    assert spoken == ["First sentence."]
    assert len(first.calls) == 1
    assert fallback.calls == []


def test_unpunctuated_output_uses_soft_speech_chunk_limit() -> None:
    spoken: list[str] = []

    def stream() -> Iterable[object]:
        yield TextDelta("alpha beta gamma delta")
        assert spoken == ["alpha beta"]
        yield completion("first")

    result = coordinator(FakeProvider("first", [stream()])).run(
        request(),
        (ExecutionTarget(target("first")),),
        speak=spoken.append,
        speech_chunk_max_chars=12,
    )

    assert result.text == "alpha beta gamma delta"
    assert spoken == ["alpha beta", "gamma delta"]


def test_minimum_pre_speech_buffer_delays_the_first_sentence() -> None:
    spoken: list[str] = []

    def stream() -> Iterable[object]:
        yield TextDelta("Hi.")
        assert spoken == []
        yield TextDelta(" More words.")
        assert spoken == ["Hi.", "More words."]
        yield completion("first")

    provider = FakeProvider("first", [stream()])

    coordinator(provider).run(
        request(),
        (ExecutionTarget(target("first")),),
        speak=spoken.append,
        first_speech_min_chars=10,
    )

    assert spoken == ["Hi.", "More words."]


def test_punctuation_only_stream_fragment_is_not_sent_to_tts() -> None:
    provider = FakeProvider(
        "first",
        [[TextDelta("Ready."), TextDelta(":"), completion("first")]],
    )
    spoken: list[str] = []

    result = coordinator(provider).run(
        request(),
        (ExecutionTarget(target("first")),),
        speak=spoken.append,
    )

    assert result.text == "Ready.:"
    assert spoken == ["Ready."]


def test_refusal_is_terminal_and_does_not_fallback() -> None:
    refusal = Refused(
        category="safety",
        safe_message="I cannot help with that.",
        metadata=completion("first").metadata,
    )
    first = FakeProvider("first", [[refusal]])
    second = FakeProvider(
        "second",
        [[TextDelta("Should not run"), completion("second")]],
    )

    with pytest.raises(ProviderError) as captured:
        coordinator(first, second).run(
            request(),
            (
                ExecutionTarget(target("first")),
                ExecutionTarget(target("second")),
            ),
        )

    assert captured.value.category is ErrorCategory.SAFETY_REFUSAL
    assert second.calls == []


def test_tts_failure_is_preserved_and_never_retried() -> None:
    provider = FakeProvider(
        "first",
        [[TextDelta("Ready."), completion("first")]],
    )

    def fail_speech(_text: str) -> None:
        raise RuntimeError("speaker failed")

    metrics = SafeMetricsRecorder()
    with pytest.raises(RuntimeError, match="speaker failed") as captured:
        coordinator(provider, metrics=metrics).run(
            request(),
            (ExecutionTarget(target("first"), retry_attempts=3),),
            speak=fail_speech,
        )

    assert len(provider.calls) == 1
    context = captured.value.streaming_context
    assert context.target.name == "first"
    assert context.attempts == 1
    assert context.speech_committed is True
    events = metrics.snapshot()
    assert [event.event for event in events] == ["llm_attempt_failed", "tts_failed"]
    assert all(event.speech_committed for event in events)


def test_three_route_fallback_reports_the_immediate_hop_and_cause() -> None:
    first_error = ProviderError(
        ErrorCategory.CONNECTIVITY,
        "offline",
        provider="first",
        model="model",
        retryable_same_provider=False,
    )
    second_error = ProviderError(
        ErrorCategory.READ_TIMEOUT,
        "timed out",
        provider="second",
        model="model",
        retryable_same_provider=False,
    )
    first = FakeProvider("first", [first_error])
    second = FakeProvider("second", [second_error])
    third = FakeProvider("third", [[TextDelta("Ready."), completion("third")]])
    metrics = SafeMetricsRecorder()

    result = coordinator(first, second, third, metrics=metrics).run(
        request(),
        (
            ExecutionTarget(target("first")),
            ExecutionTarget(target("second")),
            ExecutionTarget(target("third")),
        ),
    )

    assert result.target.name == "third"
    assert result.attempts == 3
    assert result.retry_count == 0
    assert result.fallback_count == 2
    assert result.fallback_cause == ErrorCategory.READ_TIMEOUT.value
    succeeded = metrics.snapshot()[-1]
    assert succeeded.fallback_from == "second"
    assert succeeded.fallback_to == "third"
    assert succeeded.fallback_cause == ErrorCategory.READ_TIMEOUT.value


def test_slow_first_audio_counts_against_future_provider_health() -> None:
    provider = FakeProvider(
        "first",
        [[TextDelta("Ready."), completion("first")]],
    )
    health = HealthTracker(failures_to_open=3)
    observed_times = iter([0.0, 2.0, 2.1, 2.2])
    runner = coordinator(
        provider,
        health=health,
        clock=lambda: next(observed_times),
    )

    runner.run(
        replace(
            request(),
            privacy=PrivacyLevel.REMOTE_ALLOWED,
            remote_authorized=True,
        ),
        (
            ExecutionTarget(target("first", remote=True)),
            ExecutionTarget(target("unused")),
        ),
        speak=lambda _text: None,
        maximum_first_audio_seconds=1.5,
    )

    snapshot = health.snapshot(target("first", remote=True).health_key)
    assert snapshot.failures == 1
    assert snapshot.consecutive_failures == 1


def test_slow_success_does_not_open_the_only_local_route() -> None:
    provider = FakeProvider(
        "first",
        [[TextDelta("Ready."), completion("first")]],
    )
    health = HealthTracker(failures_to_open=1)
    observed_times = iter([0.0, 2.0, 2.1, 2.2])

    coordinator(
        provider,
        health=health,
        clock=lambda: next(observed_times),
    ).run(
        request(),
        (ExecutionTarget(target("first")),),
        speak=lambda _text: None,
        maximum_first_audio_seconds=1.5,
    )

    snapshot = health.snapshot(target("first").health_key)
    assert snapshot.successes == 1
    assert snapshot.failures == 0


def test_success_records_attempt_timings_without_response_content() -> None:
    provider = FakeProvider(
        "first",
        [[TextDelta("Ready."), completion("first")]],
    )
    metrics = SafeMetricsRecorder()
    observed_times = iter([10.0, 10.1, 10.2, 10.4])

    result = coordinator(
        provider,
        metrics=metrics,
        clock=lambda: next(observed_times),
    ).run(
        request(),
        (ExecutionTarget(target("first")),),
        speak=lambda _text: None,
        route_reason="network.good",
    )

    assert result.text == "Ready."
    event = metrics.snapshot()[0]
    assert event.event == "llm_attempt_succeeded"
    assert event.provider == "first"
    assert event.model == "model"
    assert event.mode == "talk"
    assert event.language == "en"
    assert event.route_reason == "network.good"
    assert event.latency_ms == pytest.approx(400)
    assert event.first_token_ms == pytest.approx(100)
    assert event.first_audio_ms == pytest.approx(200)
    assert event.fallback_count == 0
    assert "Ready" not in str(event.as_dict())


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        (FinishReason.CANCELLED, ErrorCategory.CANCELLED),
        (FinishReason.ERROR, ErrorCategory.UNKNOWN),
        (FinishReason.TOOL_CALL, ErrorCategory.UNSUPPORTED_FEATURE),
    ],
)
def test_unsuccessful_terminal_reasons_are_not_returned_as_answers(
    reason: FinishReason,
    category: ErrorCategory,
) -> None:
    terminal = Completed(
        CompletionMetadata(
            provider="first",
            requested_model="model",
            finish_reason=reason,
        )
    )
    provider = FakeProvider("first", [[TextDelta("partial"), terminal]])

    with pytest.raises(ProviderError) as captured:
        coordinator(provider).run(
            request(),
            (ExecutionTarget(target("first")),),
        )

    assert captured.value.category is category


def test_coordinator_closes_stream_iterator_after_terminal_event() -> None:
    class ClosableIterator:
        def __init__(self) -> None:
            self.events = iter([TextDelta("Done."), completion("first")])
            self.closed = False

        def __iter__(self) -> ClosableIterator:
            return self

        def __next__(self) -> object:
            return next(self.events)

        def close(self) -> None:
            self.closed = True

    stream = ClosableIterator()
    provider = FakeProvider("first", [stream])

    assert coordinator(provider).run(request(), (ExecutionTarget(target("first")),)).text == "Done."
    assert stream.closed


def test_coordinator_exhausts_provider_generator_after_completion() -> None:
    state = {"exhausted": False, "generator_exit": False}

    def events() -> Iterable[object]:
        try:
            yield TextDelta("Done.")
            yield completion("first")
        except GeneratorExit:
            state["generator_exit"] = True
            raise
        state["exhausted"] = True

    provider = FakeProvider("first", [events()])

    assert coordinator(provider).run(request(), (ExecutionTarget(target("first")),)).text == "Done."
    assert state == {"exhausted": True, "generator_exit": False}


def priced_model() -> ModelPrice:
    return ModelPrice(
        id="first/model",
        provider="first",
        model="model",
        input_per_million_usd=Decimal("1"),
        output_per_million_usd=Decimal("1"),
        context_window=4096,
        max_output_tokens=64,
        free_tier=False,
    )


def test_priced_route_rejects_a_different_resolved_model() -> None:
    terminal = Completed(
        CompletionMetadata(
            provider="first",
            requested_model="model",
            resolved_model="different-model",
            finish_reason=FinishReason.STOP,
        )
    )
    provider = FakeProvider("first", [[TextDelta("Answer"), terminal]])

    with pytest.raises(ProviderError) as captured:
        coordinator(provider).run(
            replace(
                request(),
                privacy=PrivacyLevel.REMOTE_ALLOWED,
                remote_authorized=True,
            ),
            (
                ExecutionTarget(
                    target("first", remote=True),
                    max_output_tokens=32,
                    price=priced_model(),
                ),
            ),
        )

    assert captured.value.category is ErrorCategory.MALFORMED_RESPONSE


def test_tts_failure_settles_a_remote_reservation_conservatively(
    tmp_path: Path,
) -> None:
    terminal = Completed(
        CompletionMetadata(
            provider="first",
            requested_model="model",
            resolved_model="model",
            finish_reason=FinishReason.STOP,
        )
    )
    provider = FakeProvider("first", [[TextDelta("Ready."), terminal]])
    ledger = BudgetLedger(
        tmp_path / "usage.jsonl",
        BudgetLimits(
            per_request_usd=Decimal("1"),
            daily_usd=Decimal("1"),
            monthly_usd=Decimal("1"),
        ),
    )
    metrics = SafeMetricsRecorder()

    with pytest.raises(RuntimeError, match="speaker failed"):
        coordinator(
            provider,
            budget=ledger,
            require_priced_remote=True,
            metrics=metrics,
        ).run(
            replace(
                request(),
                privacy=PrivacyLevel.REMOTE_ALLOWED,
                remote_authorized=True,
            ),
            (
                ExecutionTarget(
                    target("first", remote=True),
                    max_output_tokens=32,
                    price=priced_model(),
                ),
            ),
            speak=lambda _text: (_ for _ in ()).throw(RuntimeError("speaker failed")),
        )

    snapshot = ledger.snapshot()
    assert snapshot.outstanding_usd == 0
    assert snapshot.daily_usd > 0
    attempt = next(event for event in metrics.snapshot() if event.event == "llm_attempt_failed")
    assert attempt.cost_usd == snapshot.daily_usd
    assert attempt.estimated_cost_usd is not None
    assert attempt.estimated_cost_usd >= attempt.cost_usd
    assert attempt.estimated_output_tokens == 32


def test_exhausted_rate_limit_snapshot_cools_down_the_target() -> None:
    terminal = Completed(
        CompletionMetadata(
            provider="first",
            requested_model="model",
            resolved_model="model",
            finish_reason=FinishReason.STOP,
            rate_limits=RateLimitSnapshot(
                remaining_requests=0,
                retry_after_seconds=30,
            ),
        )
    )
    provider = FakeProvider("first", [[TextDelta("Done."), terminal]])
    health = HealthTracker()

    coordinator(provider, health=health).run(
        replace(
            request(),
            privacy=PrivacyLevel.REMOTE_ALLOWED,
            remote_authorized=True,
        ),
        (ExecutionTarget(target("first", remote=True)),),
    )

    snapshot = health.snapshot(target("first", remote=True).health_key)
    assert not snapshot.available
    assert snapshot.retry_after_seconds is not None
