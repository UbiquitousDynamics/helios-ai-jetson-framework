"""Typed provider-neutral contracts for streamed chat inference."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, Union


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ContentOrigin(str, Enum):
    STATIC_INSTRUCTION = "static_instruction"
    RAW_TRANSCRIPT = "raw_transcript"
    CONVERSATION_HISTORY = "conversation_history"
    LOCAL_DOCUMENT = "local_document"
    LOCAL_DOCUMENT_DERIVATIVE = "local_document_derivative"
    TOOL_RESULT = "tool_result"
    UNKNOWN = "unknown"


class PrivacyLevel(str, Enum):
    LOCAL_ONLY = "local_only"
    REMOTE_ALLOWED = "remote_allowed"
    REMOTE_REDACTED = "remote_redacted"


class FinishReason(str, Enum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALL = "tool_call"
    SAFETY = "safety"
    CANCELLED = "cancelled"
    ERROR = "error"
    UNKNOWN = "unknown"


class ErrorCategory(str, Enum):
    CONNECTIVITY = "connectivity"
    DNS = "dns"
    TLS = "tls"
    CONNECT_TIMEOUT = "connect_timeout"
    FIRST_TOKEN_TIMEOUT = "first_token_timeout"
    READ_TIMEOUT = "read_timeout"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    QUOTA_EXHAUSTED = "quota_exhausted"
    CONTEXT_OVERFLOW = "context_overflow"
    SAFETY_REFUSAL = "safety_refusal"
    UNSUPPORTED_FEATURE = "unsupported_feature"
    MALFORMED_RESPONSE = "malformed_response"
    EMPTY_COMPLETION = "empty_completion"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PRIVACY_BLOCKED = "privacy_blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str
    origin: ContentOrigin = ContentOrigin.UNKNOWN
    redacted: bool = False
    remote_eligible: bool = True
    source_origins: frozenset[ContentOrigin] = frozenset()

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("message content cannot be empty")
        if not isinstance(self.remote_eligible, bool):
            raise TypeError("remote_eligible must be a boolean")
        try:
            normalized_origins = frozenset(ContentOrigin(origin) for origin in self.source_origins)
        except (TypeError, ValueError):
            raise ValueError("source_origins contains an invalid provenance") from None
        object.__setattr__(self, "source_origins", normalized_origins)


@dataclass(frozen=True, slots=True)
class Timeouts:
    connect_seconds: float = 2.0
    first_token_seconds: float = 20.0
    read_seconds: float = 15.0
    total_seconds: float = 45.0

    def __post_init__(self) -> None:
        values = (
            self.connect_seconds,
            self.first_token_seconds,
            self.read_seconds,
            self.total_seconds,
        )
        if any(value <= 0 for value in values):
            raise ValueError("all timeout values must be greater than zero")
        if self.total_seconds < self.connect_seconds:
            raise ValueError("total timeout cannot be shorter than connect timeout")


@dataclass(frozen=True, slots=True)
class ChatRequest:
    model: str
    messages: tuple[ChatMessage, ...]
    mode: str
    language: str
    privacy: PrivacyLevel = PrivacyLevel.LOCAL_ONLY
    max_output_tokens: int | None = None
    timeouts: Timeouts = field(default_factory=Timeouts)
    required_features: frozenset[str] = frozenset()
    options: Mapping[str, Any] = field(default_factory=dict)
    remote_authorized: bool = False
    conversation_id: str | None = None
    conversation_turn: int | None = None

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model cannot be empty")
        if not self.messages:
            raise ValueError("at least one message is required")
        if self.mode not in {"talk", "think"}:
            raise ValueError(f"unsupported request mode: {self.mode!r}")
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be at least one")
        if self.conversation_id is not None and not self.conversation_id.strip():
            raise ValueError("conversation_id cannot be empty")
        if self.conversation_turn is not None and (
            isinstance(self.conversation_turn, bool)
            or not isinstance(self.conversation_turn, int)
            or self.conversation_turn < 1
        ):
            raise ValueError("conversation_turn must be a positive integer")


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    remaining_requests: int | None = None
    remaining_tokens: int | None = None
    reset_requests: str | None = None
    reset_tokens: str | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    name: str
    endpoint: str
    remote: bool


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    supports_system_messages: bool = True
    supports_streaming_usage: bool = False
    supports_reasoning: bool = False
    context_window: int | None = None
    max_output_tokens: int | None = None
    languages: frozenset[str] = frozenset()
    features: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CompletionMetadata:
    provider: str
    requested_model: str
    resolved_model: str | None = None
    finish_reason: FinishReason = FinishReason.UNKNOWN
    provider_finish_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    request_id: str | None = None
    rate_limits: RateLimitSnapshot | None = None


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True, slots=True)
class Completed:
    metadata: CompletionMetadata


@dataclass(frozen=True, slots=True)
class Refused:
    category: str
    safe_message: str | None
    metadata: CompletionMetadata


StreamEvent = Union[TextDelta, ReasoningDelta, Completed, Refused]


class CancellationToken(Protocol):
    @property
    def cancelled(self) -> bool: ...

    def raise_if_cancelled(self) -> None: ...


class ProviderError(RuntimeError):
    """Sanitized provider failure safe for application logs."""

    def __init__(
        self,
        category: ErrorCategory,
        safe_message: str,
        *,
        provider: str,
        model: str | None = None,
        retryable_same_provider: bool = False,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        request_id: str | None = None,
        transmitted: bool | None = None,
        attempts: int = 1,
    ) -> None:
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError("attempts must be a positive integer")
        super().__init__(safe_message)
        self.category = category
        self.provider = provider
        self.model = model
        self.retryable_same_provider = retryable_same_provider
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.request_id = request_id
        self.transmitted = transmitted
        self.attempts = attempts


class ChatProvider(Protocol):
    @property
    def identity(self) -> ProviderIdentity: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def stream(
        self,
        request: ChatRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> Iterable[StreamEvent]: ...

    def warm_up(self, model: str) -> None: ...

    def close(self) -> None: ...
