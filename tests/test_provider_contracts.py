import pytest

from api.providers.contracts import ErrorCategory, ProviderError


def test_provider_error_has_an_explicit_attempt_count() -> None:
    error = ProviderError(
        ErrorCategory.CONNECTIVITY,
        "safe message",
        provider="provider",
        attempts=3,
    )

    assert error.attempts == 3


@pytest.mark.parametrize("attempts", [True, 0, -1, 1.5])
def test_provider_error_rejects_invalid_attempt_counts(attempts: object) -> None:
    with pytest.raises(ValueError, match="attempts"):
        ProviderError(
            ErrorCategory.UNKNOWN,
            "safe message",
            provider="provider",
            attempts=attempts,  # type: ignore[arg-type]
        )
