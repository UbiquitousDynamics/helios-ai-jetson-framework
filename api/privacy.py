"""Provider-neutral privacy authorization for remote inference."""

from __future__ import annotations

from dataclasses import dataclass, replace

from api.providers.contracts import (
    ChatMessage,
    ChatRequest,
    ContentOrigin,
    ErrorCategory,
    PrivacyLevel,
    ProviderError,
)


@dataclass(frozen=True, slots=True)
class PrivacyPolicy:
    """Explicit remote-egress switches.

    Unknown provenance is intentionally not configurable: it is always local-only.
    Local-document data has its own gate and is not covered by the broader context
    switch.
    """

    remote_enabled: bool = False
    allow_remote_transcripts: bool = False
    allow_remote_context: bool = False
    allow_remote_rag_context: bool = False

    def __post_init__(self) -> None:
        for name in (
            "remote_enabled",
            "allow_remote_transcripts",
            "allow_remote_context",
            "allow_remote_rag_context",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")


class PrivacyGuard:
    """Authorizes a request for remote dispatch without mutating the input."""

    def __init__(self, policy: PrivacyPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> PrivacyPolicy:
        return self._policy

    def authorize_remote(self, request: ChatRequest) -> ChatRequest:
        """Return a copy carrying the adapter-enforced authorization capability.

        A rejected request raises a sanitized ``ProviderError`` and is known not to
        have transmitted any bytes.
        """

        if not self._policy.remote_enabled:
            self._deny(request, "remote inference is disabled")
        try:
            privacy = PrivacyLevel(request.privacy)
        except (TypeError, ValueError):
            self._deny(request, "request has an invalid privacy classification")
        if privacy is PrivacyLevel.LOCAL_ONLY:
            self._deny(request, "request privacy policy requires local inference")

        canonical_messages: list[ChatMessage] | None = None
        for index, message in enumerate(request.messages):
            if message.remote_eligible is not True:
                self._deny(request, "conversation contains local-only content")
            try:
                origin = ContentOrigin(message.origin)
            except (TypeError, ValueError):
                self._deny(request, "message provenance is invalid")
            effective_origins = {origin, *message.source_origins}
            for effective_origin in effective_origins:
                if effective_origin is ContentOrigin.UNKNOWN:
                    self._deny(request, "message provenance is unknown")
                if (
                    effective_origin is ContentOrigin.RAW_TRANSCRIPT
                    and not self._policy.allow_remote_transcripts
                ):
                    self._deny(request, "remote transcript processing is disabled")
                if (
                    effective_origin
                    in {
                        ContentOrigin.CONVERSATION_HISTORY,
                        ContentOrigin.TOOL_RESULT,
                    }
                    and not self._policy.allow_remote_context
                ):
                    self._deny(request, "remote context processing is disabled")
                if (
                    effective_origin
                    in {
                        ContentOrigin.LOCAL_DOCUMENT,
                        ContentOrigin.LOCAL_DOCUMENT_DERIVATIVE,
                    }
                    and not self._policy.allow_remote_rag_context
                ):
                    self._deny(request, "remote local-document processing is disabled")
            if (
                privacy is PrivacyLevel.REMOTE_REDACTED
                and origin is not ContentOrigin.STATIC_INSTRUCTION
                and message.redacted is not True
            ):
                self._deny(request, "remote request requires redacted content")
            if not isinstance(message.origin, ContentOrigin):
                if canonical_messages is None:
                    canonical_messages = list(request.messages[:index])
                canonical_messages.append(replace(message, origin=origin))
            elif canonical_messages is not None:
                canonical_messages.append(message)

        messages = request.messages if canonical_messages is None else tuple(canonical_messages)

        return replace(
            request,
            messages=messages,
            privacy=privacy,
            remote_authorized=True,
        )

    def require_remote_authorized(self, request: ChatRequest) -> None:
        """Adapter-side defense in depth for an already-routed request."""

        if request.remote_authorized is not True:
            self._deny(request, "remote request was not privacy-authorized")
        # Re-evaluate the complete request so dataclasses.replace() cannot retain
        # authorization after adding content with a more restrictive provenance.
        self.authorize_remote(request)

    @staticmethod
    def for_local(request: ChatRequest) -> ChatRequest:
        """Remove a stale remote capability when a request is routed locally."""

        if not request.remote_authorized:
            return request
        return replace(request, remote_authorized=False)

    @staticmethod
    def _deny(request: ChatRequest, message: str) -> None:
        raise ProviderError(
            ErrorCategory.PRIVACY_BLOCKED,
            message,
            provider="privacy_guard",
            model=request.model,
            retryable_same_provider=False,
            transmitted=False,
        )


__all__ = ["PrivacyGuard", "PrivacyPolicy"]
