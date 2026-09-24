from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import config
import pytest
from api.api_client import APIClient, APIClientError
from api.budget import BudgetLedger, BudgetLimits
from api.catalog import ModelPrice
from api.providers.contracts import (
    ChatMessage,
    ChatRequest,
    ContentOrigin,
    ErrorCategory,
    PrivacyLevel,
    ProviderCapabilities,
    ProviderError,
    ProviderIdentity,
    Role,
    TextDelta,
)
from api.routing import Connectivity, ProviderRegistry, ProviderTarget
from api.streaming import (
    CancellationController,
    ExecutionTarget,
    StreamingResponseCoordinator,
)


class FakeTTS:
    def speak(self, text: str) -> None:
        raise AssertionError(f"cancelled output must not be spoken: {text}")


class FakeOllamaClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def chat(self, **kwargs: object) -> Iterable[object]:
        self.calls.append(kwargs)
        return iter([{"message": {"content": "fallback"}, "done": True}])


class BlockingCancellationProvider:
    def __init__(self, name: str = "remote") -> None:
        self.name = name
        self.started = threading.Event()
        self.calls: list[ChatRequest] = []

    @property
    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            self.name,
            "https://provider.invalid/v1",
            remote=True,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    def stream(
        self,
        request: ChatRequest,
        *,
        cancellation: object | None = None,
    ) -> Iterable[object]:
        assert cancellation is not None
        self.calls.append(request)
        self.started.set()
        while not bool(getattr(cancellation, "cancelled")):
            time.sleep(0.005)
        raise ProviderError(
            ErrorCategory.CANCELLED,
            "request cancelled",
            provider=self.name,
            model=request.model,
            retryable_same_provider=True,
            transmitted=True,
        )

    def warm_up(self, model: str) -> None:
        raise AssertionError(f"remote warm-up is forbidden: {model}")

    def prepare(self) -> None:
        return None

    def close(self) -> None:
        return None


def _hybrid_settings(tmp_path: Path) -> config.LLMSettings:
    return config.LLMSettings(
        routing_file=tmp_path / "routing.toml",
        routing_policy="remote_first",
        remote_enabled=True,
        privacy=config.LLMPrivacySettings(
            default="remote_allowed",
            allow_remote_transcripts=True,
        ),
        budget=config.LLMBudgetSettings(enabled=False),
        talk=config.LLMModeSettings(
            candidates=("remote-talk", "local-talk"),
            max_output_tokens=32,
        ),
        providers=(
            config.LLMProviderSettings(
                name="remote",
                adapter="openai_chat_sse",
                endpoint="https://provider.invalid/v1",
                locality="remote",
                api_key_env="REMOTE_API_KEY",
            ),
        ),
        targets=(
            config.LLMTargetSettings(
                name="remote-talk",
                provider="remote",
                model="remote-model",
                retry_attempts=3,
            ),
            config.LLMTargetSettings(
                name="local-talk",
                provider="ollama",
                model="local-model",
            ),
        ),
    )


def _run_in_thread(call: Callable[[], object]) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            call()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


def test_cancel_current_cross_thread_is_prompt_and_terminal(tmp_path: Path) -> None:
    remote = BlockingCancellationProvider()
    local = FakeOllamaClient()
    client = APIClient(
        client=local,
        tts=FakeTTS(),
        llm_settings=_hybrid_settings(tmp_path),
        language="en",
        providers={"remote": remote},
        connectivity=Connectivity.ONLINE,
        retry_wait=0,
    )
    thread, errors = _run_in_thread(lambda: client.talk("Emilia, answer"))

    assert remote.started.wait(timeout=1)
    cancelled_at = time.monotonic()
    client.cancel_current()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert time.monotonic() - cancelled_at < 1
    assert len(errors) == 1
    assert isinstance(errors[0], APIClientError)
    assert "cancelled" in str(errors[0])
    assert len(remote.calls) == 1
    assert local.calls == []


def _request() -> ChatRequest:
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
        privacy=PrivacyLevel.REMOTE_ALLOWED,
        remote_authorized=True,
        max_output_tokens=32,
    )


def _target(name: str) -> ProviderTarget:
    return ProviderTarget(
        name=name,
        provider=name,
        model="model",
        remote=True,
        modes=frozenset({"talk"}),
    )


def _price(name: str) -> ModelPrice:
    return ModelPrice(
        id=f"{name}/model",
        provider=name,
        model="model",
        input_per_million_usd=Decimal("1"),
        output_per_million_usd=Decimal("2"),
        context_window=4096,
        max_output_tokens=64,
        free_tier=False,
    )


def test_cancelled_request_without_usage_settles_full_reservation(
    tmp_path: Path,
) -> None:
    provider = BlockingCancellationProvider("first")
    fallback = BlockingCancellationProvider("fallback")
    registry = ProviderRegistry()
    registry.register_instance("first", provider)
    registry.register_instance("fallback", fallback)
    ledger_path = tmp_path / "usage.jsonl"
    ledger = BudgetLedger(
        ledger_path,
        BudgetLimits(
            per_request_usd=Decimal("1"),
            daily_usd=Decimal("1"),
            monthly_usd=Decimal("1"),
        ),
    )
    coordinator = StreamingResponseCoordinator(
        registry,
        budget=ledger,
        require_priced_remote=True,
        retry_wait=0,
    )
    cancellation = CancellationController()
    executions = (
        ExecutionTarget(
            _target("first"),
            retry_attempts=3,
            max_output_tokens=32,
            price=_price("first"),
        ),
        ExecutionTarget(
            _target("fallback"),
            max_output_tokens=32,
            price=_price("fallback"),
        ),
    )
    thread, errors = _run_in_thread(
        lambda: coordinator.run(
            replace(_request(), model="model"),
            executions,
            cancellation=cancellation,
        )
    )

    assert provider.started.wait(timeout=1)
    cancellation.cancel()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ProviderError)
    assert errors[0].category is ErrorCategory.CANCELLED
    assert len(provider.calls) == 1
    assert fallback.calls == []

    records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    reservation = next(record for record in records if record["event"] == "reserve")
    settlement = next(record for record in records if record["event"] == "settle")
    assert settlement["conservative"] is True
    assert settlement["charged_usd"] == reservation["reserved_usd"]
    snapshot = ledger.snapshot()
    assert snapshot.outstanding_usd == 0
    assert snapshot.daily_usd == Decimal(reservation["reserved_usd"])


def test_coordinator_checkpoint_marks_an_in_stream_cancel_as_transmitted(
    tmp_path: Path,
) -> None:
    cancellation = CancellationController()

    class CancellingProvider(BlockingCancellationProvider):
        def stream(
            self,
            request: ChatRequest,
            *,
            cancellation: object | None = None,
        ) -> Iterable[object]:
            assert cancellation is not None
            self.calls.append(request)
            getattr(cancellation, "cancel")()
            yield TextDelta("must stay unspoken")

    provider = CancellingProvider("first")
    registry = ProviderRegistry()
    registry.register_instance("first", provider)
    ledger_path = tmp_path / "usage.jsonl"
    ledger = BudgetLedger(
        ledger_path,
        BudgetLimits(
            per_request_usd=Decimal("1"),
            daily_usd=Decimal("1"),
            monthly_usd=Decimal("1"),
        ),
    )
    coordinator = StreamingResponseCoordinator(
        registry,
        budget=ledger,
        require_priced_remote=True,
        retry_wait=0,
    )

    with pytest.raises(ProviderError) as captured:
        coordinator.run(
            replace(_request(), model="model"),
            (
                ExecutionTarget(
                    _target("first"),
                    max_output_tokens=32,
                    price=_price("first"),
                ),
            ),
            cancellation=cancellation,
        )

    assert captured.value.category is ErrorCategory.CANCELLED
    assert captured.value.transmitted is True
    records = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    reservation = next(record for record in records if record["event"] == "reserve")
    settlement = next(record for record in records if record["event"] == "settle")
    assert settlement["conservative"] is True
    assert settlement["charged_usd"] == reservation["reserved_usd"]


def test_silent_provider_cancel_at_eof_is_terminal_without_retry_or_fallback() -> None:
    cancellation = CancellationController()

    class SilentCancellingProvider(BlockingCancellationProvider):
        def stream(
            self,
            request: ChatRequest,
            *,
            cancellation: object | None = None,
        ) -> Iterable[object]:
            assert cancellation is not None
            self.calls.append(request)
            getattr(cancellation, "cancel")()
            if False:  # pragma: no cover - retain the generator contract
                yield TextDelta("unreachable")

    provider = SilentCancellingProvider("first")
    fallback = BlockingCancellationProvider("fallback")
    registry = ProviderRegistry()
    registry.register_instance("first", provider)
    registry.register_instance("fallback", fallback)
    coordinator = StreamingResponseCoordinator(registry, retry_wait=0)

    with pytest.raises(ProviderError) as captured:
        coordinator.run(
            replace(_request(), model="model"),
            (
                ExecutionTarget(_target("first"), retry_attempts=3),
                ExecutionTarget(_target("fallback")),
            ),
            cancellation=cancellation,
        )

    assert captured.value.category is ErrorCategory.CANCELLED
    assert captured.value.transmitted is True
    assert len(provider.calls) == 1
    assert fallback.calls == []
