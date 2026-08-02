from dataclasses import replace

import pytest

from api.privacy import PrivacyGuard, PrivacyPolicy
from api.providers.contracts import (
    ChatMessage,
    ChatRequest,
    ContentOrigin,
    ErrorCategory,
    PrivacyLevel,
    ProviderError,
    Role,
)


def request_for(
    origin: ContentOrigin,
    *,
    privacy: PrivacyLevel = PrivacyLevel.REMOTE_ALLOWED,
    redacted: bool = False,
) -> ChatRequest:
    return ChatRequest(
        model="model",
        messages=(ChatMessage(Role.USER, "hello", origin, redacted),),
        mode="talk",
        language="en",
        privacy=privacy,
    )


@pytest.mark.parametrize(
    ("origin", "policy_field"),
    [
        (ContentOrigin.RAW_TRANSCRIPT, "allow_remote_transcripts"),
        (ContentOrigin.CONVERSATION_HISTORY, "allow_remote_context"),
        (ContentOrigin.TOOL_RESULT, "allow_remote_context"),
        (ContentOrigin.LOCAL_DOCUMENT, "allow_remote_rag_context"),
        (ContentOrigin.LOCAL_DOCUMENT_DERIVATIVE, "allow_remote_rag_context"),
    ],
)
def test_independent_origin_gates(origin, policy_field):
    guard = PrivacyGuard(PrivacyPolicy(remote_enabled=True))

    with pytest.raises(ProviderError) as exc_info:
        guard.authorize_remote(request_for(origin))

    assert exc_info.value.category is ErrorCategory.PRIVACY_BLOCKED
    assert exc_info.value.transmitted is False

    enabled_policy = replace(
        PrivacyPolicy(remote_enabled=True),
        **{policy_field: True},
    )
    authorized = PrivacyGuard(enabled_policy).authorize_remote(request_for(origin))
    assert authorized.remote_authorized is True


def test_unknown_and_local_only_are_never_authorized():
    guard = PrivacyGuard(
        PrivacyPolicy(
            remote_enabled=True,
            allow_remote_transcripts=True,
            allow_remote_context=True,
            allow_remote_rag_context=True,
        )
    )

    with pytest.raises(ProviderError):
        guard.authorize_remote(request_for(ContentOrigin.UNKNOWN))
    with pytest.raises(ProviderError):
        guard.authorize_remote(
            request_for(
                ContentOrigin.STATIC_INSTRUCTION,
                privacy=PrivacyLevel.LOCAL_ONLY,
            )
        )


def test_remote_redacted_requires_non_static_messages_to_be_marked_redacted():
    guard = PrivacyGuard(PrivacyPolicy(remote_enabled=True, allow_remote_transcripts=True))
    request = request_for(
        ContentOrigin.RAW_TRANSCRIPT,
        privacy=PrivacyLevel.REMOTE_REDACTED,
    )

    with pytest.raises(ProviderError):
        guard.authorize_remote(request)

    authorized = guard.authorize_remote(
        replace(request, messages=(replace(request.messages[0], redacted=True),))
    )
    assert authorized.remote_authorized


def test_remote_capability_is_a_replacement_and_can_be_removed():
    request = request_for(ContentOrigin.STATIC_INSTRUCTION)
    guard = PrivacyGuard(PrivacyPolicy(remote_enabled=True))

    authorized = guard.authorize_remote(request)

    assert authorized is not request
    assert not request.remote_authorized
    guard.require_remote_authorized(authorized)
    assert guard.for_local(authorized).remote_authorized is False
    with pytest.raises(ProviderError):
        guard.require_remote_authorized(request)


def test_authorization_reuses_already_canonical_immutable_messages():
    request = request_for(ContentOrigin.STATIC_INSTRUCTION)
    guard = PrivacyGuard(PrivacyPolicy(remote_enabled=True))

    authorized = guard.authorize_remote(request)

    assert authorized.messages is request.messages
    assert authorized.messages[0] is request.messages[0]


def test_authorization_replaces_noncanonical_runtime_origin():
    request = request_for(ContentOrigin.STATIC_INSTRUCTION)
    runtime_value = replace(request.messages[0], origin="static_instruction")
    guard = PrivacyGuard(PrivacyPolicy(remote_enabled=True))

    authorized = guard.authorize_remote(replace(request, messages=(runtime_value,)))

    assert authorized.messages[0] is not runtime_value
    assert authorized.messages[0].origin is ContentOrigin.STATIC_INSTRUCTION


def test_runtime_string_classifications_are_canonicalized_and_cannot_bypass_gates():
    guard = PrivacyGuard(PrivacyPolicy(remote_enabled=True))
    transcript = replace(
        request_for(ContentOrigin.STATIC_INSTRUCTION),
        privacy="remote_allowed",
        messages=(
            replace(
                request_for(ContentOrigin.STATIC_INSTRUCTION).messages[0],
                origin="raw_transcript",
            ),
        ),
    )

    with pytest.raises(ProviderError, match="transcript"):
        guard.authorize_remote(transcript)
    with pytest.raises(ProviderError, match="local"):
        guard.authorize_remote(replace(transcript, privacy="local_only"))
    with pytest.raises(ProviderError, match="invalid"):
        guard.authorize_remote(replace(transcript, privacy="invented"))


def test_adapter_recheck_rejects_authorization_retained_after_sensitive_content_change():
    guard = PrivacyGuard(PrivacyPolicy(remote_enabled=True))
    authorized = guard.authorize_remote(request_for(ContentOrigin.STATIC_INSTRUCTION))
    stale = replace(
        authorized,
        messages=(replace(authorized.messages[0], origin=ContentOrigin.LOCAL_DOCUMENT),),
    )

    with pytest.raises(ProviderError, match="document"):
        guard.require_remote_authorized(stale)


def test_policy_flags_must_be_real_booleans():
    with pytest.raises(TypeError, match="boolean"):
        PrivacyPolicy(remote_enabled="false")
