"""Provider-independent streaming, speech buffering, retry, and failover."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from api.budget import BudgetError, BudgetLedger, Reservation
from api.catalog import ModelPrice
from api.health import HealthTracker
from api.metrics import MetricEvent, SafeMetricsRecorder
from api.privacy import PrivacyGuard
from api.providers.contracts import (
    CancellationToken,
    ChatMessage,
    ChatRequest,
    Completed,
    CompletionMetadata,
    ContentOrigin,
    ErrorCategory,
    FinishReason,
    ProviderError,
    ReasoningDelta,
    Refused,
    Role,
    TextDelta,
)
from api.routing import ProviderRegistry, ProviderTarget, RoutePlanner
from api.speech_chunker import SpeechChunker

logger = logging.getLogger(__name__)

_TERMINAL_ERRORS = frozenset(
    {
        ErrorCategory.CANCELLED,
        ErrorCategory.SAFETY_REFUSAL,
    }
)


@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    """A routed target plus attempt-specific controls not used for selection."""

    route: ProviderTarget
    retry_attempts: int = 1
    max_output_tokens: int | None = None
    max_output_words: int | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    price: ModelPrice | None = None

    def __post_init__(self) -> None:
        if self.retry_attempts < 1:
            raise ValueError("retry_attempts must be at least one")
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be at least one")
        if self.max_output_words is not None and (
            isinstance(self.max_output_words, bool)
            or not isinstance(self.max_output_words, int)
            or self.max_output_words < 1
        ):
            raise ValueError("max_output_words must be a positive integer")


@dataclass(frozen=True, slots=True)
class StreamingResult:
    text: str
    metadata: CompletionMetadata
    target: ProviderTarget
    attempts: int
    first_token_seconds: float | None
    first_audio_seconds: float | None
    actual_first_audio_seconds: float | None = None
    tts_synthesis_seconds: float = 0.0
    audio_playback_seconds: float = 0.0
    audio_duration_seconds: float = 0.0
    attempt_latency_seconds: float | None = None
    fallback_count: int = 0
    retry_count: int = 0
    fallback_cause: str | None = None


@dataclass(frozen=True, slots=True)
class StreamingFailureContext:
    """Content-free route accounting attached to a failed streamed request."""

    target: ProviderTarget
    attempts: int
    retry_count: int
    fallback_count: int
    fallback_from: str | None = None
    fallback_to: str | None = None
    fallback_cause: str | None = None
    speech_committed: bool = False


class SpeechReplayUnsafeError(RuntimeError):
    """A provider failed after audio may already have reached the listener."""

    def __init__(self, error: ProviderError) -> None:
        super().__init__("The model stream failed after speech output began; replay is unsafe")
        self.error = error


class _SpeechFailure(Exception):
    def __init__(self, error: Exception) -> None:
        super().__init__("speech output failed")
        self.error = error


class CancellationController:
    """Thread-safe cancellation token used by the synchronous public API."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._commit_lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        with self._commit_lock:
            self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RuntimeError("request cancelled")

    def commit_if_not_cancelled(self, operation: Callable[[], Any]) -> Any:
        """Linearize logical completion against a concurrent cancellation."""

        with self._commit_lock:
            if self._event.is_set():
                raise RuntimeError("request cancelled")
            return operation()


@dataclass(slots=True)
class _AttemptState:
    started_at: float
    first_token_at: float | None = None
    first_audio_at: float | None = None
    actual_first_audio_at: float | None = None
    speech_committed: bool = False
    tts_synthesis_seconds: float = 0.0
    audio_playback_seconds: float = 0.0
    audio_duration_seconds: float = 0.0


class StreamingResponseCoordinator:
    """Execute a deterministic route without replaying spoken partial output."""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        privacy: PrivacyGuard | None = None,
        health: HealthTracker | None = None,
        budget: BudgetLedger | None = None,
        metrics: SafeMetricsRecorder | None = None,
        require_priced_remote: bool = False,
        retry_wait: float = 5.0,
        maximum_retry_delay: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        activity_tracker: Any | None = None,
    ) -> None:
        if retry_wait < 0 or maximum_retry_delay < 0:
            raise ValueError("retry delays cannot be negative")
        self.registry = registry
        self.privacy = privacy
        self.health = health
        self.budget = budget
        self.metrics = metrics or SafeMetricsRecorder(enabled=False)
        self.require_priced_remote = require_priced_remote
        self.retry_wait = retry_wait
        self.maximum_retry_delay = maximum_retry_delay
        self._sleep = sleep
        self._clock = clock
        self._activity_tracker = activity_tracker

    def run(
        self,
        request: ChatRequest,
        targets: tuple[ExecutionTarget, ...],
        *,
        speak: Callable[[str], Any] | None = None,
        before_first_speech: Callable[[], Any] | None = None,
        first_speech_min_chars: int = 0,
        speech_chunk_max_chars: int = 0,
        maximum_first_audio_seconds: float | None = None,
        cancellation: CancellationToken | None = None,
        route_reason: str | None = None,
        complexity_score: int | None = None,
        network_state: str | None = None,
        network_quality_score: float | None = None,
        network_quality_tier: str | None = None,
        network_reason: str | None = None,
        network_forced_local: bool | None = None,
    ) -> StreamingResult:
        if not targets:
            raise ProviderError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                "No language-model route is available",
                provider="router",
                model=request.model,
                transmitted=False,
            )
        if first_speech_min_chars < 0:
            raise ValueError("first_speech_min_chars cannot be negative")
        if speech_chunk_max_chars < 0:
            raise ValueError("speech_chunk_max_chars cannot be negative")
        if before_first_speech is not None and not callable(before_first_speech):
            raise TypeError("before_first_speech must be callable")
        if maximum_first_audio_seconds is not None and maximum_first_audio_seconds <= 0:
            raise ValueError("maximum_first_audio_seconds must be positive")

        metric_dimensions = {
            "complexity_score": complexity_score,
            "network_state": network_state,
            "network_quality_score": network_quality_score,
            "network_quality_tier": network_quality_tier,
            "network_reason": network_reason,
            "network_forced_local": network_forced_local,
        }

        last_error: ProviderError | None = None
        total_attempts = 0
        total_retries = 0
        for fallback_index, execution in enumerate(targets):
            target = execution.route
            fallback_from = targets[fallback_index - 1].route if fallback_index > 0 else None
            fallback_cause = (
                last_error.category.value if fallback_index > 0 and last_error is not None else None
            )
            routed_request = self._request_for_target(request, execution)
            try:
                routed_request = self._authorize(routed_request, target)
            except ProviderError as error:
                last_error = error
                self._record_failure(
                    execution,
                    error,
                    request=request,
                    fallback_index=fallback_index,
                    attempt_id=None,
                    state=None,
                    retry_count=0,
                    route_reason=route_reason,
                    metric_dimensions=metric_dimensions,
                    fallback_from=fallback_from,
                    fallback_cause=fallback_cause,
                )
                self._attach_failure_context(
                    error,
                    target=target,
                    attempts=total_attempts,
                    retry_count=total_retries,
                    fallback_index=fallback_index,
                    fallback_from=fallback_from,
                    fallback_cause=fallback_cause,
                )
                continue

            for provider_attempt in range(1, execution.retry_attempts + 1):
                self._raise_if_cancelled(cancellation, target)
                total_attempts += 1
                if provider_attempt > 1:
                    total_retries += 1
                attempt_id = uuid.uuid4().hex
                reservation: Reservation | None = None
                state = _AttemptState(started_at=self._clock())
                activity_token = self._begin_activity(request, target)
                try:
                    reservation = self._reserve(
                        execution,
                        routed_request,
                        attempt_id=attempt_id,
                    )
                    provider = self._provider(target)
                    result = self._stream_once(
                        provider=provider,
                        execution=execution,
                        request=routed_request,
                        speak=speak,
                        before_first_speech=before_first_speech,
                        first_speech_min_chars=first_speech_min_chars,
                        speech_chunk_max_chars=speech_chunk_max_chars,
                        cancellation=cancellation,
                        state=state,
                    )
                    result = self._finalize_success(
                        result=result,
                        reservation=reservation,
                        execution=execution,
                        request=request,
                        state=state,
                        maximum_first_audio_seconds=maximum_first_audio_seconds,
                        has_fallback=len(targets) > 1,
                        fallback_index=fallback_index,
                        attempt_id=attempt_id,
                        retry_count=provider_attempt - 1,
                        fallback_cause=fallback_cause,
                        fallback_from=fallback_from,
                        route_reason=route_reason,
                        metric_dimensions=metric_dimensions,
                    )
                    return replace(
                        result,
                        attempts=total_attempts,
                        retry_count=total_retries,
                    )
                except _SpeechFailure as wrapped:
                    speech_error = ProviderError(
                        ErrorCategory.UNKNOWN,
                        "Speech output failed during a provider stream",
                        provider=target.provider,
                        model=target.model,
                        retryable_same_provider=False,
                        transmitted=True,
                    )
                    charged: Decimal | None = None
                    try:
                        charged = self._settle_failure(
                            reservation,
                            execution,
                            speech_error,
                            speech_committed=True,
                        )
                    except SpeechReplayUnsafeError:
                        # The outstanding reservation remains a conservative
                        # charge and the original TTS exception stays intact.
                        pass
                    self._record_failure(
                        execution,
                        speech_error,
                        request=request,
                        fallback_index=fallback_index,
                        attempt_id=attempt_id,
                        state=state,
                        retry_count=provider_attempt - 1,
                        route_reason=route_reason,
                        metric_dimensions=metric_dimensions,
                        fallback_from=fallback_from,
                        fallback_cause=fallback_cause,
                        reservation=reservation,
                        charged=charged,
                    )
                    self._record_metric(
                        "tts_failed",
                        provider=target.provider,
                        model=target.model,
                        mode=request.mode,
                        language=request.language,
                        route=target.name,
                        locality="remote" if target.remote else "local",
                        model_tier=target.tier,
                        route_reason=route_reason,
                        outcome="failed",
                        success=False,
                        latency_ms=(self._clock() - state.started_at) * 1_000,
                        error_category=ErrorCategory.UNKNOWN,
                        fallback_count=fallback_index,
                        fallback_from=(fallback_from.name if fallback_from is not None else None),
                        fallback_to=target.name if fallback_from is not None else None,
                        fallback_cause=fallback_cause,
                        speech_committed=True,
                        streaming=True,
                        resource_scope="streaming",
                        **metric_dimensions,
                    )
                    context = self._attach_failure_context(
                        speech_error,
                        target=target,
                        attempts=total_attempts,
                        retry_count=total_retries,
                        fallback_index=fallback_index,
                        fallback_from=fallback_from,
                        fallback_cause=fallback_cause,
                        speech_committed=True,
                    )
                    try:
                        wrapped.error.streaming_error = speech_error
                        wrapped.error.streaming_context = context
                    except Exception:
                        pass
                    raise wrapped.error from None
                except SpeechReplayUnsafeError:
                    raise
                except ProviderError as error:
                    last_error = error
                    charged = self._settle_failure(
                        reservation,
                        execution,
                        error,
                        speech_committed=state.speech_committed,
                    )
                    if self.health is not None and error.category not in {
                        ErrorCategory.BUDGET_EXHAUSTED,
                        ErrorCategory.PRIVACY_BLOCKED,
                    }:
                        self.health.record_failure(target.health_key, error)
                    self._record_failure(
                        execution,
                        error,
                        request=request,
                        fallback_index=fallback_index,
                        attempt_id=attempt_id,
                        state=state,
                        retry_count=provider_attempt - 1,
                        route_reason=route_reason,
                        metric_dimensions=metric_dimensions,
                        fallback_from=fallback_from,
                        fallback_cause=fallback_cause,
                        reservation=reservation,
                        charged=charged,
                    )
                    self._attach_failure_context(
                        error,
                        target=target,
                        attempts=total_attempts,
                        retry_count=total_retries,
                        fallback_index=fallback_index,
                        fallback_from=fallback_from,
                        fallback_cause=fallback_cause,
                        speech_committed=state.speech_committed,
                    )
                    if state.speech_committed:
                        raise SpeechReplayUnsafeError(error) from None
                    if error.category in _TERMINAL_ERRORS:
                        raise
                    if not self._can_retry(error, provider_attempt, execution):
                        break
                    delay = max(self.retry_wait, error.retry_after_seconds or 0.0)
                    if delay > self.maximum_retry_delay:
                        break
                    if delay:
                        self._sleep(delay)
                except Exception:
                    error = ProviderError(
                        ErrorCategory.UNKNOWN,
                        "Language-model provider failed unexpectedly",
                        provider=target.provider,
                        model=target.model,
                        retryable_same_provider=False,
                        transmitted=None,
                    )
                    charged = self._settle_failure(
                        reservation,
                        execution,
                        error,
                        speech_committed=state.speech_committed,
                    )
                    if self.health is not None:
                        self.health.record_failure(target.health_key, error)
                    self._record_failure(
                        execution,
                        error,
                        request=request,
                        fallback_index=fallback_index,
                        attempt_id=attempt_id,
                        state=state,
                        retry_count=provider_attempt - 1,
                        route_reason=route_reason,
                        metric_dimensions=metric_dimensions,
                        fallback_from=fallback_from,
                        fallback_cause=fallback_cause,
                        reservation=reservation,
                        charged=charged,
                    )
                    self._attach_failure_context(
                        error,
                        target=target,
                        attempts=total_attempts,
                        retry_count=total_retries,
                        fallback_index=fallback_index,
                        fallback_from=fallback_from,
                        fallback_cause=fallback_cause,
                        speech_committed=state.speech_committed,
                    )
                    if state.speech_committed:
                        raise SpeechReplayUnsafeError(error) from None
                    last_error = error
                    break
                finally:
                    self._end_activity(activity_token)

        if last_error is not None:
            last_error.attempts = max(1, total_attempts)
            raise last_error
        raise ProviderError(
            ErrorCategory.PROVIDER_UNAVAILABLE,
            "No language-model route completed successfully",
            provider="router",
            model=request.model,
            transmitted=False,
        )

    def _begin_activity(self, request: ChatRequest, target: ProviderTarget) -> object | None:
        begin = getattr(self._activity_tracker, "begin", None)
        if not callable(begin):
            return None
        try:
            return begin(
                mode=request.mode,
                locality="remote" if target.remote else "local",
                provider=target.provider,
                model=target.model,
                route=target.name,
            )
        except Exception:
            return None

    def _end_activity(self, token: object | None) -> None:
        end = getattr(self._activity_tracker, "end", None)
        if not callable(end):
            return
        try:
            end(token)
        except Exception:
            return

    def _stream_once(
        self,
        *,
        provider: Any,
        execution: ExecutionTarget,
        request: ChatRequest,
        speak: Callable[[str], Any] | None,
        before_first_speech: Callable[[], Any] | None,
        first_speech_min_chars: int,
        speech_chunk_max_chars: int,
        cancellation: CancellationToken | None,
        state: _AttemptState,
    ) -> StreamingResult:
        response_parts: list[str] = []
        completed: CompletionMetadata | None = None
        speech_chunker = (
            SpeechChunker(
                first_speech_min_chars=first_speech_min_chars,
                speech_chunk_max_chars=speech_chunk_max_chars,
            )
            if speak is not None
            else None
        )

        def speak_fragment(sentence: str) -> None:
            assert speak is not None
            self._raise_if_cancelled(
                cancellation,
                execution.route,
                transmitted=True,
            )
            if not state.speech_committed and before_first_speech is not None:
                try:
                    before_first_speech()
                except Exception as error:
                    raise _SpeechFailure(error) from None
                self._raise_if_cancelled(
                    cancellation,
                    execution.route,
                    transmitted=True,
                )
            state.speech_committed = True
            if state.first_audio_at is None:
                state.first_audio_at = self._clock()
            try:
                timing = speak(sentence)
            except Exception as error:
                raise _SpeechFailure(error) from None
            self._raise_if_cancelled(
                cancellation,
                execution.route,
                transmitted=True,
            )
            if timing is None:
                return
            try:
                synthesis_ms = float(getattr(timing, "synthesis_ms"))
                playback_ms = float(getattr(timing, "playback_ms"))
                duration_ms = float(getattr(timing, "audio_duration_ms"))
                actual_started_at = float(getattr(timing, "audio_started_at"))
                if not all(
                    value >= 0
                    for value in (synthesis_ms, playback_ms, duration_ms, actual_started_at)
                ):
                    return
            except (AttributeError, TypeError, ValueError):
                return
            state.tts_synthesis_seconds += synthesis_ms / 1_000
            state.audio_playback_seconds += playback_ms / 1_000
            state.audio_duration_seconds += duration_ms / 1_000
            if state.actual_first_audio_at is None:
                state.actual_first_audio_at = actual_started_at

        iterator: Any = None
        try:
            events = provider.stream(request, cancellation=cancellation)
            iterator = iter(events)
            for event in iterator:
                self._raise_if_cancelled(
                    cancellation,
                    execution.route,
                    transmitted=True,
                )
                if completed is not None:
                    raise ProviderError(
                        ErrorCategory.MALFORMED_RESPONSE,
                        "Provider emitted data after completion metadata",
                        provider=execution.route.provider,
                        model=execution.route.model,
                        retryable_same_provider=False,
                        transmitted=True,
                        request_id=completed.request_id,
                    )
                if isinstance(event, TextDelta):
                    if not event.text:
                        continue
                    if state.first_token_at is None:
                        state.first_token_at = self._clock()
                    response_parts.append(event.text)
                    if speech_chunker is not None:
                        for sentence in speech_chunker.push(event.text):
                            speak_fragment(sentence)
                elif isinstance(event, ReasoningDelta):
                    continue
                elif isinstance(event, Refused):
                    raise ProviderError(
                        ErrorCategory.SAFETY_REFUSAL,
                        event.safe_message or "The provider declined this request",
                        provider=execution.route.provider,
                        model=execution.route.model,
                        retryable_same_provider=False,
                        transmitted=True,
                        request_id=event.metadata.request_id,
                    )
                elif isinstance(event, Completed):
                    completed = event.metadata
                    # Exhaust the provider generator naturally. Closing it at
                    # this point injects GeneratorExit into HTTP-backed streams
                    # even though the completion itself was successful.
                    continue
                else:
                    raise ProviderError(
                        ErrorCategory.MALFORMED_RESPONSE,
                        "Provider emitted an unsupported streaming event",
                        provider=execution.route.provider,
                        model=execution.route.model,
                        retryable_same_provider=False,
                        transmitted=True,
                    )
        except _SpeechFailure:
            raise
        except ProviderError:
            raise
        except Exception:
            raise ProviderError(
                ErrorCategory.UNKNOWN,
                "Language-model stream failed unexpectedly",
                provider=execution.route.provider,
                model=execution.route.model,
                retryable_same_provider=False,
                transmitted=None,
            ) from None
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        self._raise_if_cancelled(
            cancellation,
            execution.route,
            transmitted=True,
        )
        if completed is None:
            raise ProviderError(
                ErrorCategory.MALFORMED_RESPONSE,
                "Provider stream ended without completion metadata",
                provider=execution.route.provider,
                model=execution.route.model,
                retryable_same_provider=True,
                transmitted=True,
            )
        if completed.finish_reason is FinishReason.SAFETY:
            raise ProviderError(
                ErrorCategory.SAFETY_REFUSAL,
                "The provider declined this request",
                provider=execution.route.provider,
                model=execution.route.model,
                retryable_same_provider=False,
                transmitted=True,
                request_id=completed.request_id,
            )
        if completed.finish_reason is FinishReason.CANCELLED:
            raise ProviderError(
                ErrorCategory.CANCELLED,
                "Provider stream was cancelled",
                provider=execution.route.provider,
                model=execution.route.model,
                retryable_same_provider=False,
                transmitted=True,
                request_id=completed.request_id,
            )
        if completed.finish_reason is FinishReason.ERROR:
            raise ProviderError(
                ErrorCategory.UNKNOWN,
                "Provider reported an unsuccessful completion",
                provider=execution.route.provider,
                model=execution.route.model,
                retryable_same_provider=False,
                transmitted=True,
                request_id=completed.request_id,
            )
        if completed.finish_reason is FinishReason.TOOL_CALL:
            raise ProviderError(
                ErrorCategory.UNSUPPORTED_FEATURE,
                "Provider returned an unsupported tool call",
                provider=execution.route.provider,
                model=execution.route.model,
                retryable_same_provider=False,
                transmitted=True,
                request_id=completed.request_id,
            )

        text = "".join(response_parts)
        if not text.strip():
            raise ProviderError(
                ErrorCategory.EMPTY_COMPLETION,
                "Provider returned an empty completion",
                provider=execution.route.provider,
                model=execution.route.model,
                retryable_same_provider=True,
                transmitted=True,
            )
        if speech_chunker is not None:
            for sentence in speech_chunker.finish():
                speak_fragment(sentence)
        return StreamingResult(
            text=text,
            metadata=completed,
            target=execution.route,
            attempts=1,
            first_token_seconds=self._elapsed_seconds(
                state.started_at,
                state.first_token_at,
            ),
            first_audio_seconds=self._elapsed_seconds(
                state.started_at,
                state.first_audio_at,
            ),
            actual_first_audio_seconds=self._elapsed_seconds(
                state.started_at,
                state.actual_first_audio_at,
            ),
            tts_synthesis_seconds=state.tts_synthesis_seconds,
            audio_playback_seconds=state.audio_playback_seconds,
            audio_duration_seconds=state.audio_duration_seconds,
        )

    @staticmethod
    def _request_for_target(
        request: ChatRequest,
        execution: ExecutionTarget,
    ) -> ChatRequest:
        max_output = execution.max_output_tokens
        if max_output is None:
            max_output = request.max_output_tokens
        if execution.route.max_output_tokens is not None and max_output is not None:
            max_output = min(max_output, execution.route.max_output_tokens)
        messages = request.messages
        if execution.max_output_words is not None:
            suffix = (
                f" Limita la risposta a un massimo di {execution.max_output_words} parole."
                if request.language == "it"
                else f" Limit the answer to at most {execution.max_output_words} words."
            )
            updated_messages = list(messages)
            for index, message in enumerate(updated_messages):
                if (
                    message.role is Role.SYSTEM
                    and message.origin is ContentOrigin.STATIC_INSTRUCTION
                ):
                    updated_messages[index] = replace(
                        message,
                        content=message.content + suffix,
                    )
                    break
            else:
                updated_messages.insert(
                    0,
                    ChatMessage(
                        Role.SYSTEM,
                        suffix.strip(),
                        origin=ContentOrigin.STATIC_INSTRUCTION,
                    ),
                )
            messages = tuple(updated_messages)
        return replace(
            request,
            model=execution.route.model,
            messages=messages,
            max_output_tokens=max_output,
            options={
                **dict(execution.options),
                **{
                    name: value
                    for name, value in request.options.items()
                    if name not in {"complex", "resource_offload"}
                },
            },
        )

    def _authorize(
        self,
        request: ChatRequest,
        target: ProviderTarget,
    ) -> ChatRequest:
        if not target.remote:
            return PrivacyGuard.for_local(request)
        if self.privacy is None:
            if not request.remote_authorized:
                raise ProviderError(
                    ErrorCategory.PRIVACY_BLOCKED,
                    "Remote request was not privacy-authorized",
                    provider=target.provider,
                    model=target.model,
                    transmitted=False,
                )
            return request
        return self.privacy.authorize_remote(request)

    def _provider(self, target: ProviderTarget) -> Any:
        try:
            return self.registry.get(target.provider)
        except ProviderError:
            raise
        except Exception:
            raise ProviderError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                "Language-model provider could not be initialized",
                provider=target.provider,
                model=target.model,
                retryable_same_provider=False,
                transmitted=False,
            ) from None

    @staticmethod
    def _raise_if_cancelled(
        cancellation: CancellationToken | None,
        target: ProviderTarget,
        *,
        transmitted: bool = False,
    ) -> None:
        if cancellation is None:
            return
        try:
            cancellation.raise_if_cancelled()
            cancelled = cancellation.cancelled
            if not isinstance(cancelled, bool):
                raise TypeError
        except ProviderError:
            raise
        except Exception:
            raise ProviderError(
                ErrorCategory.CANCELLED,
                "Language-model request was cancelled",
                provider=target.provider,
                model=target.model,
                retryable_same_provider=False,
                transmitted=transmitted,
            ) from None
        if cancelled:
            raise ProviderError(
                ErrorCategory.CANCELLED,
                "Language-model request was cancelled",
                provider=target.provider,
                model=target.model,
                retryable_same_provider=False,
                transmitted=transmitted,
            )

    def _reserve(
        self,
        execution: ExecutionTarget,
        request: ChatRequest,
        *,
        attempt_id: str,
    ) -> Reservation | None:
        if not execution.route.remote:
            return None
        if execution.price is None:
            if self.require_priced_remote:
                raise ProviderError(
                    ErrorCategory.BUDGET_EXHAUSTED,
                    "Remote model has no current catalog entry",
                    provider=execution.route.provider,
                    model=execution.route.model,
                    transmitted=False,
                )
            return None
        if self.budget is None:
            if self.require_priced_remote:
                raise ProviderError(
                    ErrorCategory.BUDGET_EXHAUSTED,
                    "Remote budget accounting is unavailable",
                    provider=execution.route.provider,
                    model=execution.route.model,
                    transmitted=False,
                )
            return None
        try:
            return self.budget.reserve(
                provider=execution.route.provider,
                model=execution.route.model,
                attempt_id=attempt_id,
                price=execution.price,
                estimated_input_tokens=RoutePlanner.estimate_input_tokens(request),
                max_output_tokens=(
                    request.max_output_tokens
                    or execution.route.max_output_tokens
                    or execution.price.max_output_tokens
                ),
            )
        except (BudgetError, ValueError):
            raise ProviderError(
                ErrorCategory.BUDGET_EXHAUSTED,
                "Remote request was blocked by the configured budget",
                provider=execution.route.provider,
                model=execution.route.model,
                transmitted=False,
            ) from None

    def _finalize_success(
        self,
        *,
        result: StreamingResult,
        reservation: Reservation | None,
        execution: ExecutionTarget,
        request: ChatRequest,
        state: _AttemptState,
        maximum_first_audio_seconds: float | None,
        has_fallback: bool,
        fallback_index: int,
        attempt_id: str,
        retry_count: int,
        fallback_cause: str | None,
        fallback_from: ProviderTarget | None,
        route_reason: str | None,
        metric_dimensions: Mapping[str, Any],
    ) -> StreamingResult:
        """Validate, settle, and observe one successful provider attempt."""

        self._require_priced_model_identity(execution, result.metadata)
        charged = self._settle_success(
            reservation,
            execution,
            result.metadata,
            speech_committed=state.speech_committed,
        )
        latency = self._clock() - state.started_at
        self._record_success_health(
            execution=execution,
            result=result,
            latency=latency,
            maximum_first_audio_seconds=maximum_first_audio_seconds,
            has_fallback=has_fallback,
        )
        rate_limits = result.metadata.rate_limits
        actual_first_audio_ms = self._elapsed_ms(
            state.started_at,
            state.actual_first_audio_at,
        )
        synthesis_ms = state.tts_synthesis_seconds * 1_000
        playback_ms = state.audio_playback_seconds * 1_000
        self._record_metric(
            "llm_attempt_succeeded",
            usage=result.metadata.usage,
            provider=execution.route.provider,
            model=execution.route.model,
            resolved_model=result.metadata.resolved_model,
            mode=request.mode,
            language=request.language,
            route=execution.route.name,
            locality="remote" if execution.route.remote else "local",
            model_tier=execution.route.tier,
            route_reason=route_reason,
            outcome="succeeded",
            success=True,
            request_id=result.metadata.request_id,
            attempt_id=attempt_id,
            latency_ms=latency * 1_000,
            inference_ms=max(0.0, latency * 1_000 - synthesis_ms - playback_ms),
            first_token_ms=self._elapsed_ms(state.started_at, state.first_token_at),
            first_audio_ms=self._elapsed_ms(state.started_at, state.first_audio_at),
            speech_dispatch_ms=self._elapsed_ms(state.started_at, state.first_audio_at),
            actual_first_audio_ms=actual_first_audio_ms,
            tts_synthesis_ms=synthesis_ms or None,
            audio_playback_ms=playback_ms or None,
            audio_duration_ms=(state.audio_duration_seconds * 1_000 or None),
            streaming_lead_ms=(
                max(0.0, latency * 1_000 - actual_first_audio_ms)
                if actual_first_audio_ms is not None
                else None
            ),
            remaining_requests=(
                rate_limits.remaining_requests if rate_limits is not None else None
            ),
            remaining_tokens=(rate_limits.remaining_tokens if rate_limits is not None else None),
            estimated_input_tokens=RoutePlanner.estimate_input_tokens(request),
            cost_usd=charged,
            retry_count=retry_count,
            fallback_count=fallback_index,
            fallback_from=fallback_from.name if fallback_from is not None else None,
            fallback_to=execution.route.name if fallback_index > 0 else None,
            fallback_cause=fallback_cause,
            speech_committed=state.speech_committed,
            streaming=True,
            **metric_dimensions,
        )
        return replace(
            result,
            attempt_latency_seconds=latency,
            fallback_count=fallback_index,
            fallback_cause=fallback_cause,
        )

    def _record_success_health(
        self,
        *,
        execution: ExecutionTarget,
        result: StreamingResult,
        latency: float,
        maximum_first_audio_seconds: float | None,
        has_fallback: bool,
    ) -> None:
        if self.health is None:
            return
        target = execution.route
        rate_limits = result.metadata.rate_limits
        if rate_limits is not None and (
            rate_limits.remaining_requests == 0 or rate_limits.remaining_tokens == 0
        ):
            self.health.record_failure(
                target.health_key,
                ErrorCategory.RATE_LIMITED,
                retry_after_seconds=rate_limits.retry_after_seconds,
            )
        elif (
            maximum_first_audio_seconds is not None
            and target.remote
            and has_fallback
            and result.first_audio_seconds is not None
            and result.first_audio_seconds > maximum_first_audio_seconds
        ):
            logger.warning(
                "Route %s completed but exceeded the first-audio health "
                "objective (%.0f ms > %.0f ms)",
                target.name,
                result.first_audio_seconds * 1_000,
                maximum_first_audio_seconds * 1_000,
            )
            self.health.record_failure(
                target.health_key,
                ErrorCategory.FIRST_TOKEN_TIMEOUT,
            )
        else:
            self.health.record_success(
                target.health_key,
                latency_seconds=latency,
            )

    def _settle_success(
        self,
        reservation: Reservation | None,
        execution: ExecutionTarget,
        metadata: CompletionMetadata,
        *,
        speech_committed: bool,
    ) -> Decimal | None:
        if reservation is None or self.budget is None:
            return None
        try:
            settlement = self.budget.settle(
                reservation.reservation_id,
                usage=metadata.usage,
                price=execution.price,
            )
        except (BudgetError, ValueError):
            error = ProviderError(
                ErrorCategory.BUDGET_EXHAUSTED,
                "Remote usage could not be reconciled safely",
                provider=execution.route.provider,
                model=execution.route.model,
                transmitted=True,
            )
            if speech_committed:
                raise SpeechReplayUnsafeError(error) from None
            raise error from None
        return settlement.charged_usd

    @staticmethod
    def _require_priced_model_identity(
        execution: ExecutionTarget,
        metadata: CompletionMetadata,
    ) -> None:
        if execution.price is None:
            return
        if metadata.resolved_model != execution.route.model:
            raise ProviderError(
                ErrorCategory.MALFORMED_RESPONSE,
                "Provider model identity did not match the priced route",
                provider=execution.route.provider,
                model=execution.route.model,
                retryable_same_provider=False,
                transmitted=True,
                request_id=metadata.request_id,
            )

    def _settle_failure(
        self,
        reservation: Reservation | None,
        execution: ExecutionTarget,
        error: ProviderError,
        *,
        speech_committed: bool,
    ) -> Decimal | None:
        if reservation is None or self.budget is None:
            return None
        try:
            if error.transmitted is False:
                settlement = self.budget.settle(
                    reservation.reservation_id,
                    actual_amount_usd=Decimal(0),
                )
            else:
                settlement = self.budget.settle(reservation.reservation_id)
            return settlement.charged_usd
        except (BudgetError, ValueError):
            settlement_error = ProviderError(
                ErrorCategory.BUDGET_EXHAUSTED,
                "Remote usage could not be reconciled safely",
                provider=execution.route.provider,
                model=execution.route.model,
                transmitted=error.transmitted,
            )
            if speech_committed:
                raise SpeechReplayUnsafeError(settlement_error) from None
            raise settlement_error from None

    def _record_failure(
        self,
        execution: ExecutionTarget,
        error: ProviderError,
        *,
        request: ChatRequest,
        fallback_index: int,
        attempt_id: str | None,
        state: _AttemptState | None,
        retry_count: int,
        route_reason: str | None,
        metric_dimensions: Mapping[str, Any],
        fallback_from: ProviderTarget | None = None,
        fallback_cause: str | None = None,
        reservation: Reservation | None = None,
        charged: Decimal | None = None,
    ) -> None:
        latency_ms = None
        first_token_ms = None
        first_audio_ms = None
        if state is not None:
            latency_ms = (self._clock() - state.started_at) * 1_000
            first_token_ms = self._elapsed_ms(state.started_at, state.first_token_at)
            first_audio_ms = self._elapsed_ms(state.started_at, state.first_audio_at)
        self._record_metric(
            "llm_attempt_failed",
            provider=execution.route.provider,
            model=execution.route.model,
            mode=request.mode,
            language=request.language,
            route=execution.route.name,
            locality="remote" if execution.route.remote else "local",
            model_tier=execution.route.tier,
            route_reason=route_reason,
            outcome="failed",
            success=False,
            request_id=error.request_id,
            attempt_id=attempt_id,
            latency_ms=latency_ms,
            first_token_ms=first_token_ms,
            first_audio_ms=first_audio_ms,
            speech_dispatch_ms=first_audio_ms,
            actual_first_audio_ms=(
                self._elapsed_ms(state.started_at, state.actual_first_audio_at)
                if state is not None
                else None
            ),
            tts_synthesis_ms=(state.tts_synthesis_seconds * 1_000 if state is not None else None),
            audio_playback_ms=(state.audio_playback_seconds * 1_000 if state is not None else None),
            audio_duration_ms=(state.audio_duration_seconds * 1_000 if state is not None else None),
            estimated_input_tokens=RoutePlanner.estimate_input_tokens(request),
            estimated_output_tokens=(
                request.max_output_tokens
                or execution.max_output_tokens
                or execution.route.max_output_tokens
                or (execution.price.max_output_tokens if execution.price is not None else None)
            ),
            cost_usd=charged,
            estimated_cost_usd=(reservation.reserved_usd if reservation is not None else None),
            error_category=error.category,
            timeout_category=(error.category.value if "timeout" in error.category.value else None),
            retry_count=retry_count,
            fallback_count=fallback_index,
            fallback_from=fallback_from.name if fallback_from is not None else None,
            fallback_to=execution.route.name if fallback_from is not None else None,
            fallback_cause=fallback_cause,
            speech_committed=state.speech_committed if state is not None else False,
            streaming=True,
            **metric_dimensions,
        )

    @staticmethod
    def _attach_failure_context(
        error: ProviderError,
        *,
        target: ProviderTarget,
        attempts: int,
        retry_count: int,
        fallback_index: int,
        fallback_from: ProviderTarget | None,
        fallback_cause: str | None,
        speech_committed: bool = False,
    ) -> StreamingFailureContext:
        context = StreamingFailureContext(
            target=target,
            attempts=attempts,
            retry_count=retry_count,
            fallback_count=fallback_index,
            fallback_from=fallback_from.name if fallback_from is not None else None,
            fallback_to=target.name if fallback_from is not None else None,
            fallback_cause=fallback_cause,
            speech_committed=speech_committed,
        )
        try:
            error.streaming_context = context
        except Exception:
            pass
        return context

    def _record_metric(self, event: str, *, usage: Any | None = None, **values: Any) -> None:
        try:
            metric = (
                MetricEvent(event=event, **values)
                if usage is None
                else MetricEvent.from_usage(event, usage, **values)
            )
            self.metrics.record(metric)
        except Exception:
            # Observability must never change routing, speech, or budget behavior.
            return

    @staticmethod
    def _can_retry(
        error: ProviderError,
        provider_attempt: int,
        execution: ExecutionTarget,
    ) -> bool:
        return error.retryable_same_provider and provider_attempt < execution.retry_attempts

    @staticmethod
    def _elapsed_seconds(started_at: float, observed_at: float | None) -> float | None:
        return None if observed_at is None else observed_at - started_at

    @staticmethod
    def _elapsed_ms(started_at: float, observed_at: float | None) -> float | None:
        elapsed = StreamingResponseCoordinator._elapsed_seconds(started_at, observed_at)
        return None if elapsed is None else elapsed * 1_000


__all__ = [
    "CancellationController",
    "ExecutionTarget",
    "SpeechReplayUnsafeError",
    "StreamingFailureContext",
    "StreamingResponseCoordinator",
    "StreamingResult",
]
