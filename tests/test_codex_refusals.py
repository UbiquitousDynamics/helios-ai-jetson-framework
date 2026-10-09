"""Sanitized issue #20 examples; never launch the Codex runtime."""

from types import SimpleNamespace

import pytest

from api.api_client import APIClient
from api.health import HealthStatus, HealthTracker
from api.metrics import SafeMetricsRecorder
from api.providers.codex_app_server import CodexAppServerAdapter, _classify_usage_snapshot
from api.providers.contracts import ErrorCategory, ProviderError
from api.routing import Connectivity
from test_assistant import FakeTTS
from test_cancellation_contract import FakeOllamaClient, _hybrid_settings
from test_codex_app_server import FakeRuntime, notification, request


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize(
    "message,code,category",
    [
        ("Request refused", "usageLimitExceeded", ErrorCategory.QUOTA_EXHAUSTED),
        ("Premium credit balance is empty", "usageLimitExceeded", ErrorCategory.CREDIT_EXHAUSTED),
        ("Rate limit reached", "usageLimitExceeded", ErrorCategory.RATE_LIMITED),
    ],
)
def test_real_turn_error_shape_is_classified_without_logging_content(
    typed, message, code, category, caplog
):
    error = {"message": message, "codexErrorInfo": code, "additionalDetails": "private-detail"}
    if typed:
        error = SimpleNamespace(
            message=message,
            codex_error_info=SimpleNamespace(root=SimpleNamespace(value=code)),
            additional_details="private-detail",
        )
    runtime = FakeRuntime(
        "chatgpt",
        [notification("turn/completed", {"turn": {"status": "failed", "error": error}})],
    )
    provider = CodexAppServerAdapter("remote", runtime=runtime)
    try:
        with pytest.raises(ProviderError) as captured:
            list(provider.stream(request()))
        assert captured.value.category is category
        assert captured.value.retryable_same_provider is False
        assert captured.value.transmitted is True
        assert f"category={category.value}" in caplog.text
        assert "private-detail" not in caplog.text
        assert message not in caplog.text
        if category is ErrorCategory.CREDIT_EXHAUSTED:
            assert "waiting will not restore" in str(captured.value)
    finally:
        provider.close()


def test_healthy_recorded_standard_windows_are_not_exhaustion():
    healthy = {
        "limit_id": "codex",
        "plan_type": "plus",
        "primary": {"resets_at": 1790088601, "used_percent": 58.0, "window_minutes": 300},
        "secondary": {"resets_at": 1790539837, "used_percent": 40.0, "window_minutes": 10080},
        "credits": {"balance": "0", "has_credits": False},
    }
    assert _classify_usage_snapshot(healthy) is None


@pytest.mark.parametrize("premium", [False, True])
def test_structured_rpc_error_preserves_credit_or_window_attribution(premium, monkeypatch, caplog):
    """JSON-RPC exceptions have data; this does not assert TurnError has it."""

    class Refusal(Exception):
        def __init__(self):
            super().__init__("Usage limit reached; private-detail")
            self.data = {
                "limit_id": "premium" if premium else "codex",
                "primary": None if premium else {"resets_at": 1790088601, "used_percent": 100},
                "secondary": None,
                "credits": {"balance": "0", "has_credits": False, "unlimited": False},
            }

    runtime = FakeRuntime("chatgpt", [])

    def failed_stream():
        raise Refusal()

    runtime.turn.stream = failed_stream
    monkeypatch.setattr("api.providers.codex_app_server.time.time", lambda: 1790088501)
    provider = CodexAppServerAdapter("remote", runtime=runtime)
    try:
        with pytest.raises(ProviderError) as captured:
            list(provider.stream(request()))
        error = captured.value
        assert error.category is (
            ErrorCategory.CREDIT_EXHAUSTED if premium else ErrorCategory.RATE_LIMITED
        )
        assert error.retry_after_seconds == (None if premium else 100)
        assert not error.retryable_same_provider
        assert f"category={error.category.value}" in caplog.text
        assert "private-detail" not in caplog.text
        now = [0.0]
        health = HealthTracker(clock=lambda: now[0])
        health.record_failure("remote/model", error)
        assert not health.is_available("remote/model")
        now[0] = 100.0
        assert health.is_available("remote/model") is (not premium)
    finally:
        provider.close()


@pytest.mark.parametrize("message", ["Premium credit balance is empty", "Usage limit reached"])
def test_credit_or_ambiguous_quota_refusal_blocks_subsequent_turns(tmp_path, message):
    class CountingRuntime(FakeRuntime):
        starts = 0

        def start_turn(self, **kwargs):
            self.starts += 1
            return super().start_turn(**kwargs)

    runtime = CountingRuntime(
        "chatgpt",
        [
            notification(
                "turn/completed",
                {
                    "turn": {
                        "status": "failed",
                        "error": {
                            "message": message,
                            "codexErrorInfo": "usageLimitExceeded",
                        },
                    }
                },
            )
        ],
    )
    now = [0.0]
    health = HealthTracker(clock=lambda: now[0])
    metrics = SafeMetricsRecorder()
    local = FakeOllamaClient()
    provider = CodexAppServerAdapter("remote", runtime=runtime)
    client = APIClient(
        client=local,
        tts=FakeTTS(),
        llm_settings=_hybrid_settings(tmp_path),
        language="en",
        providers={"remote": provider},
        connectivity=Connectivity.ONLINE,
        health_tracker=health,
        metrics=metrics,
        retry_wait=0,
    )
    try:
        assert client.talk("first synthetic request") == "fallback"
        assert runtime.starts == 1  # Even though the configured target allows three retries.
        key = "remote/remote-model"
        assert health.snapshot(key).status is HealthStatus.QUOTA_BLOCKED
        now[0] = 604800.0
        assert client.talk("second synthetic request") == "fallback"
        assert runtime.starts == 1
        assert len(local.calls) == 2
        expected = "credit_exhausted" if "Premium" in message else "quota_exhausted"
        assert any(e.error_category == expected for e in metrics.snapshot())
    finally:
        client.close()
