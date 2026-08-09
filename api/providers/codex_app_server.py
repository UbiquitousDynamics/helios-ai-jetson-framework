"""ChatGPT-subscription provider backed by the native Codex app-server.

This adapter deliberately does not accept an API key.  It launches the official
Codex runtime over stdio, verifies that the active account is a ChatGPT account,
and uses an isolated, read-only workspace with tool surfaces disabled.
"""

from __future__ import annotations

import math
import os
import queue
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, Protocol

from api.providers.contracts import (
    CancellationToken,
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
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    Usage,
)
from api.providers.codex_session import (
    CODEX_DISABLED_FEATURES,
    codex_child_environment,
    copy_chatgpt_auth,
    field_value,
)

_ALLOWED_OPTIONS = frozenset({"reasoning_effort", "service_tier"})
_ALLOWED_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
# Private aliases remain temporarily available for compatibility with existing
# integrations and tests. New callers should use ``codex_session`` directly.
_DISABLED_CODEX_FEATURES = CODEX_DISABLED_FEATURES
_codex_child_env = codex_child_environment
_copy_chatgpt_auth = copy_chatgpt_auth
_field = field_value
_BASE_INSTRUCTIONS = """\
You are the remote language-model backend for the Helios voice assistant.
Answer the user's request directly, in the requested language, as natural-language text.
Do not inspect files, run commands, call tools, browse, modify anything, or describe internal work.
Do not return markdown unless the user explicitly asks for it.
"""


class _Turn(Protocol):
    id: str
    thread_id: str

    def stream(self) -> Iterable[Any]: ...

    def interrupt(self) -> Any: ...


class _Runtime(Protocol):
    def account_kind(self) -> str | None: ...

    def start_turn(
        self,
        *,
        model: str,
        prompt: str,
        developer_instructions: str,
        effort: str | None,
        service_tier: str | None,
        thread_id: str | None = None,
    ) -> _Turn: ...

    def close(self) -> None: ...


class _OfficialCodexRuntime:
    def __init__(self) -> None:
        try:
            from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
        except ImportError as exc:
            raise RuntimeError("The optional openai-codex package is not installed") from exc

        self._approval_mode = ApprovalMode.deny_all
        self._sandbox = Sandbox.read_only
        self._workspace = tempfile.TemporaryDirectory(prefix="helios-codex-")
        root = Path(self._workspace.name)
        self._working_directory = root / "workspace"
        self._working_directory.mkdir(mode=0o700)
        self._codex_home = root / "codex-home"
        source_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
        copy_chatgpt_auth(source_home, self._codex_home)
        config = CodexConfig(
            cwd=str(self._working_directory),
            env=codex_child_environment(self._codex_home),
            config_overrides=CODEX_DISABLED_FEATURES,
            client_name="helios",
            client_title="Helios Voice Assistant",
        )
        self._client = Codex(config)

    def account_kind(self) -> str | None:
        response = self._client.account(refresh_token=False)
        account = field_value(response, "account")
        if account is None:
            return None
        account = field_value(account, "root", account)
        kind = field_value(account, "type")
        return kind if isinstance(kind, str) else None

    def start_turn(
        self,
        *,
        model: str,
        prompt: str,
        developer_instructions: str,
        effort: str | None,
        service_tier: str | None,
        thread_id: str | None = None,
    ) -> _Turn:
        if thread_id is None:
            thread = self._client.thread_start(
                approval_mode=self._approval_mode,
                cwd=str(self._working_directory),
                developer_instructions=developer_instructions,
                ephemeral=True,
                model=model,
                model_provider="openai",
                sandbox=self._sandbox,
            )
            return thread.turn(
                prompt,
                approval_mode=self._approval_mode,
                cwd=str(self._working_directory),
                effort=effort,
                sandbox=self._sandbox,
                service_tier=service_tier,
            )

        thread = self._client.thread_resume(
            thread_id,
            approval_mode=self._approval_mode,
            cwd=str(self._working_directory),
            developer_instructions=developer_instructions,
            model=model,
            model_provider="openai",
            sandbox=self._sandbox,
            service_tier=service_tier,
        )
        return thread.turn(
            prompt,
            approval_mode=self._approval_mode,
            cwd=str(self._working_directory),
            effort=effort,
            model=model,
            sandbox=self._sandbox,
            service_tier=service_tier,
        )

    def close(self) -> None:
        try:
            self._client.close()
        finally:
            self._workspace.cleanup()


def _notification_parts(notification: Any) -> tuple[str | None, Any]:
    root = field_value(notification, "root", notification)
    method = field_value(root, "method")
    payload = field_value(root, "payload")
    if payload is None:
        payload = field_value(root, "params")
    return (method if isinstance(method, str) else None), payload


def _usage(payload: Any) -> Usage:
    token_usage = field_value(payload, "token_usage")
    if token_usage is None:
        token_usage = field_value(payload, "tokenUsage")
    latest = field_value(token_usage, "last", token_usage)
    if latest is None:
        return Usage()

    def integer(*names: str) -> int | None:
        for name in names:
            value = field_value(latest, name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    return Usage(
        input_tokens=integer("input_tokens", "inputTokens"),
        cached_input_tokens=integer("cached_input_tokens", "cachedInputTokens"),
        output_tokens=integer("output_tokens", "outputTokens"),
        reasoning_tokens=integer("reasoning_output_tokens", "reasoningOutputTokens"),
        total_tokens=integer("total_tokens", "totalTokens"),
    )


def _safe_error_message(category: ErrorCategory) -> str:
    messages = {
        ErrorCategory.AUTHENTICATION: (
            "Codex is not signed in with a ChatGPT account; local fallback is required"
        ),
        ErrorCategory.QUOTA_EXHAUSTED: "The ChatGPT Codex usage allowance is exhausted",
        ErrorCategory.CONTEXT_OVERFLOW: "The request exceeds the Codex model context limit",
        ErrorCategory.CONNECTIVITY: "Could not connect through the Codex app-server",
        ErrorCategory.CONNECT_TIMEOUT: "Timed out while starting the Codex app-server",
        ErrorCategory.FIRST_TOKEN_TIMEOUT: "Timed out waiting for the first Codex response event",
        ErrorCategory.READ_TIMEOUT: "Timed out while reading the Codex response",
        ErrorCategory.PROVIDER_UNAVAILABLE: "The Codex app-server is unavailable",
        ErrorCategory.PRIVACY_BLOCKED: "Remote transmission is not authorized for this request",
        ErrorCategory.UNSUPPORTED_FEATURE: "The Codex provider does not support this request",
        ErrorCategory.EMPTY_COMPLETION: "Codex returned no visible response",
        ErrorCategory.CANCELLED: "The Codex request was cancelled",
    }
    return messages.get(category, "The Codex request failed")


def _classify_exception(error: BaseException) -> tuple[ErrorCategory, bool]:
    marker = f"{type(error).__module__}.{type(error).__name__} {error}".lower()
    if any(
        word in marker
        for word in ("usage limit", "usage_limit", "usagelimit", "quota", "billing", "credit")
    ):
        return ErrorCategory.QUOTA_EXHAUSTED, False
    if any(word in marker for word in ("unauthorized", "authentication", "not logged", "login")):
        return ErrorCategory.AUTHENTICATION, False
    if any(word in marker for word in ("context window", "context_length", "too many tokens")):
        return ErrorCategory.CONTEXT_OVERFLOW, False
    if any(word in marker for word in ("serverbusy", "overload", "server busy", "unavailable")):
        return ErrorCategory.PROVIDER_UNAVAILABLE, True
    if any(word in marker for word in ("connection", "transportclosed", "network")):
        return ErrorCategory.CONNECTIVITY, True
    if isinstance(error, (FileNotFoundError, ImportError)):
        return ErrorCategory.PROVIDER_UNAVAILABLE, False
    return ErrorCategory.UNKNOWN, False


def _prompt(request: ChatRequest) -> tuple[str, str]:
    systems = [message.content for message in request.messages if message.role.value == "system"]
    developer = _BASE_INSTRUCTIONS
    if systems:
        developer += "\nHelios instructions:\n" + "\n".join(systems)
    if request.max_output_tokens is not None:
        developer += (
            f"\nKeep the answer concise; target at most about "
            f"{request.max_output_tokens} output tokens."
        )
    developer += f"\nAnswer language: {request.language}."

    conversation = [
        f"[{message.role.value}]\n{message.content}"
        for message in request.messages
        if message.role.value != "system"
    ]
    return developer, "\n\n".join(conversation)


class CodexAppServerAdapter:
    """One-attempt remote adapter using the user's ChatGPT Codex session."""

    def __init__(
        self,
        provider: str,
        endpoint: str = "stdio://codex",
        *,
        runtime: _Runtime | None = None,
        runtime_factory: Callable[[], _Runtime] | None = None,
        clock: Callable[[], float] = time.monotonic,
        allow_remote_context: bool = False,
        context_idle_timeout_seconds: float = 900.0,
        context_max_turns: int = 20,
    ) -> None:
        if not provider or any(character.isspace() for character in provider):
            raise ValueError("provider must be a non-empty identifier")
        if endpoint != "stdio://codex":
            raise ValueError("Codex app-server endpoint must be 'stdio://codex'")
        if runtime is not None and runtime_factory is not None:
            raise ValueError("pass either runtime or runtime_factory, not both")
        if not isinstance(allow_remote_context, bool):
            raise TypeError("allow_remote_context must be a boolean")
        if (
            isinstance(context_idle_timeout_seconds, bool)
            or not isinstance(context_idle_timeout_seconds, (int, float))
            or not math.isfinite(float(context_idle_timeout_seconds))
            or context_idle_timeout_seconds <= 0
        ):
            raise ValueError("context_idle_timeout_seconds must be positive and finite")
        if (
            isinstance(context_max_turns, bool)
            or not isinstance(context_max_turns, int)
            or context_max_turns < 1
        ):
            raise ValueError("context_max_turns must be a positive integer")
        self._identity = ProviderIdentity(provider, endpoint, remote=True)
        self._capabilities = ProviderCapabilities(
            supports_system_messages=True,
            supports_streaming_usage=True,
            supports_reasoning=True,
            features=frozenset({"reasoning", "streaming", "streaming_usage", "system_messages"}),
        )
        self._runtime = runtime
        self._runtime_factory = runtime_factory or _OfficialCodexRuntime
        self._runtime_lock = threading.Lock()
        self._account_lock = threading.Lock()
        self._chatgpt_account_verified = False
        self._clock = clock
        self._allow_remote_context = allow_remote_context
        self._context_idle_timeout_seconds = float(context_idle_timeout_seconds)
        self._context_max_turns = context_max_turns
        self._context_turn_lock = threading.Lock()
        self._context_state_lock = threading.Lock()
        self._context_thread_id: str | None = None
        self._context_turn_count = 0
        self._context_last_activity_at: float | None = None
        self._closed = False

    @property
    def identity(self) -> ProviderIdentity:
        return self._identity

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    def _error(
        self,
        category: ErrorCategory,
        *,
        model: str | None,
        retryable: bool = False,
        transmitted: bool | None,
        request_id: str | None = None,
    ) -> ProviderError:
        return ProviderError(
            category,
            _safe_error_message(category),
            provider=self.identity.name,
            model=model,
            retryable_same_provider=retryable,
            request_id=request_id,
            transmitted=transmitted,
        )

    def _get_runtime(self) -> _Runtime:
        if self._closed:
            raise self._error(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                model=None,
                transmitted=False,
            )
        with self._runtime_lock:
            if self._runtime is None:
                try:
                    self._runtime = self._runtime_factory()
                except Exception as exc:
                    category, retryable = _classify_exception(exc)
                    raise self._error(
                        category,
                        model=None,
                        retryable=retryable,
                        transmitted=False,
                    ) from None
            return self._runtime

    def _get_authenticated_runtime(self) -> _Runtime:
        with self._account_lock:
            runtime = self._get_runtime()
            if self._chatgpt_account_verified:
                return runtime
            try:
                account_kind = runtime.account_kind()
            except Exception as exc:
                category, retryable = _classify_exception(exc)
                raise self._error(
                    category,
                    model=None,
                    retryable=retryable,
                    transmitted=False,
                ) from None
            if self._closed:
                raise self._error(
                    ErrorCategory.PROVIDER_UNAVAILABLE,
                    model=None,
                    transmitted=False,
                )
            if account_kind != "chatgpt":
                raise self._error(
                    ErrorCategory.AUTHENTICATION,
                    model=None,
                    transmitted=False,
                )
            self._chatgpt_account_verified = True
            return runtime

    def prepare(self) -> None:
        """Start Codex and validate ChatGPT auth without starting an inference turn."""

        self._get_authenticated_runtime()

    def _preflight(self, request: ChatRequest) -> None:
        try:
            privacy = PrivacyLevel(request.privacy)
            origins = tuple(ContentOrigin(message.origin) for message in request.messages)
        except (TypeError, ValueError):
            privacy = PrivacyLevel.LOCAL_ONLY
            origins = ()
        if privacy is PrivacyLevel.LOCAL_ONLY or request.remote_authorized is not True:
            raise self._error(
                ErrorCategory.PRIVACY_BLOCKED,
                model=request.model,
                transmitted=False,
            )
        if privacy is PrivacyLevel.REMOTE_REDACTED and any(
            origin is not ContentOrigin.STATIC_INSTRUCTION and not message.redacted
            for message, origin in zip(request.messages, origins)
        ):
            raise self._error(
                ErrorCategory.PRIVACY_BLOCKED,
                model=request.model,
                transmitted=False,
            )
        supported = set(self.capabilities.features)
        if not request.required_features.issubset(supported):
            raise self._error(
                ErrorCategory.UNSUPPORTED_FEATURE,
                model=request.model,
                transmitted=False,
            )
        unknown = set(request.options).difference(_ALLOWED_OPTIONS)
        effort = request.options.get("reasoning_effort")
        service_tier = request.options.get("service_tier")
        if (
            unknown
            or (effort is not None and effort not in _ALLOWED_EFFORTS)
            or (service_tier is not None and not isinstance(service_tier, str))
        ):
            raise self._error(
                ErrorCategory.UNSUPPORTED_FEATURE,
                model=request.model,
                transmitted=False,
            )

    @staticmethod
    def _cancelled(cancellation: CancellationToken | None) -> bool:
        if cancellation is None:
            return False
        try:
            cancellation.raise_if_cancelled()
            return bool(cancellation.cancelled)
        except Exception:
            return True

    @staticmethod
    def _interrupt(turn: _Turn | None) -> None:
        if turn is not None:
            try:
                turn.interrupt()
            except Exception:
                pass

    def _reset_context_locked(self) -> None:
        self._context_thread_id = None
        self._context_turn_count = 0
        self._context_last_activity_at = None

    def _begin_context_attempt(self) -> tuple[str | None, int]:
        now = self._clock()
        with self._context_state_lock:
            thread_id = self._context_thread_id
            turn_count = self._context_turn_count
            last_activity = self._context_last_activity_at
            if thread_id is not None and (
                turn_count >= self._context_max_turns
                or last_activity is None
                or now - last_activity >= self._context_idle_timeout_seconds
            ):
                thread_id = None
                turn_count = 0
            # A failed, cancelled, or only partially consumed attempt must not
            # leave a thread containing an incomplete turn available for reuse.
            self._reset_context_locked()
            return thread_id, turn_count

    def _finish_context_attempt(
        self,
        attempt: dict[str, Any],
        *,
        previous_turn_count: int,
    ) -> None:
        thread_id = attempt.get("thread_id")
        completed = attempt.get("completed") is True
        with self._context_state_lock:
            if completed and isinstance(thread_id, str) and bool(thread_id) and not self._closed:
                self._context_thread_id = thread_id
                self._context_turn_count = previous_turn_count + 1
                self._context_last_activity_at = self._clock()
            else:
                self._reset_context_locked()

    def stream(
        self,
        request: ChatRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> Iterator[StreamEvent]:
        self._preflight(request)
        if not self._allow_remote_context:
            yield from self._stream_attempt(request, cancellation=cancellation)
            return

        # The app-server permits only one active turn on a thread. Serialize
        # context-enabled calls so concurrent API users cannot append competing
        # turns to the same conversation or race the shared lifecycle counters.
        with self._context_turn_lock:
            thread_id, previous_turn_count = self._begin_context_attempt()
            attempt: dict[str, Any] = {}
            try:
                yield from self._stream_attempt(
                    request,
                    cancellation=cancellation,
                    resume_thread_id=thread_id,
                    context_attempt=attempt,
                )
            finally:
                self._finish_context_attempt(
                    attempt,
                    previous_turn_count=previous_turn_count,
                )

    def _stream_attempt(
        self,
        request: ChatRequest,
        *,
        cancellation: CancellationToken | None = None,
        resume_thread_id: str | None = None,
        context_attempt: dict[str, Any] | None = None,
    ) -> Iterator[StreamEvent]:
        developer, prompt = _prompt(request)
        mailbox: queue.Queue[tuple[str, Any]] = queue.Queue()
        holder: dict[str, _Turn] = {}
        stop_requested = threading.Event()

        def worker() -> None:
            try:
                runtime = self._get_authenticated_runtime()
                if stop_requested.is_set():
                    return
                turn_kwargs = {
                    "model": request.model,
                    "prompt": prompt,
                    "developer_instructions": developer,
                    "effort": request.options.get("reasoning_effort"),
                    "service_tier": request.options.get("service_tier"),
                }
                if resume_thread_id is not None:
                    turn_kwargs["thread_id"] = resume_thread_id
                turn = runtime.start_turn(**turn_kwargs)
                holder["turn"] = turn
                if context_attempt is not None:
                    candidate = field_value(turn, "thread_id")
                    if candidate is None:
                        candidate = field_value(turn, "threadId")
                    if not isinstance(candidate, str) or not candidate:
                        candidate = resume_thread_id
                    if isinstance(candidate, str) and candidate:
                        context_attempt["thread_id"] = candidate
                if stop_requested.is_set():
                    self._interrupt(turn)
                    return
                mailbox.put(("turn", turn))
                for notification in turn.stream():
                    if stop_requested.is_set():
                        self._interrupt(turn)
                        return
                    mailbox.put(("event", notification))
                mailbox.put(("eof", None))
            except BaseException as exc:
                mailbox.put(("error", exc))

        threading.Thread(
            target=worker,
            name="helios-codex-stream",
            daemon=True,
        ).start()

        began = self._clock()
        last_event = began
        saw_visible_text = False
        saw_completed = False
        usage = Usage()
        resolved_model = request.model
        request_id: str | None = None

        while True:
            if self._cancelled(cancellation):
                stop_requested.set()
                self._interrupt(holder.get("turn"))
                raise self._error(
                    ErrorCategory.CANCELLED,
                    model=request.model,
                    transmitted="turn" in holder,
                    request_id=request_id,
                )
            now = self._clock()
            total_remaining = request.timeouts.total_seconds - (now - began)
            if "turn" not in holder:
                stage_limit = min(
                    request.timeouts.connect_seconds,
                    request.timeouts.first_token_seconds,
                )
            elif saw_visible_text:
                stage_limit = request.timeouts.read_seconds
            else:
                stage_limit = request.timeouts.first_token_seconds
            stage_remaining = stage_limit - (now - last_event)
            wait_seconds = min(0.1, total_remaining, stage_remaining)
            if wait_seconds <= 0:
                stop_requested.set()
                self._interrupt(holder.get("turn"))
                if "turn" not in holder:
                    category = ErrorCategory.CONNECT_TIMEOUT
                elif saw_visible_text:
                    category = ErrorCategory.READ_TIMEOUT
                else:
                    category = ErrorCategory.FIRST_TOKEN_TIMEOUT
                raise self._error(
                    category,
                    model=request.model,
                    retryable=True,
                    transmitted="turn" in holder,
                    request_id=request_id,
                )
            try:
                kind, value = mailbox.get(timeout=wait_seconds)
            except queue.Empty:
                continue

            if kind == "turn":
                candidate = field_value(value, "id")
                request_id = candidate if isinstance(candidate, str) else None
                continue
            if kind == "auth":
                stop_requested.set()
                raise self._error(
                    ErrorCategory.AUTHENTICATION,
                    model=request.model,
                    transmitted=False,
                )
            if kind == "error":
                stop_requested.set()
                if isinstance(value, ProviderError):
                    raise value
                category, retryable = _classify_exception(value)
                raise self._error(
                    category,
                    model=request.model,
                    retryable=retryable,
                    transmitted="turn" in holder,
                    request_id=request_id,
                ) from None
            if kind == "eof":
                if not saw_completed:
                    stop_requested.set()
                    raise self._error(
                        ErrorCategory.EMPTY_COMPLETION,
                        model=request.model,
                        transmitted="turn" in holder,
                        request_id=request_id,
                    )
                if context_attempt is not None:
                    context_attempt["completed"] = True
                return

            method, payload = _notification_parts(value)
            if method in {
                "item/agentMessage/delta",
                "item/reasoning/textDelta",
                "item/reasoning/summaryTextDelta",
            }:
                delta = field_value(payload, "delta")
                if not isinstance(delta, str) or not delta:
                    continue
                visible_delta = method == "item/agentMessage/delta"
                paused_at = self._clock()
                if visible_delta:
                    saw_visible_text = True
                    event: StreamEvent = TextDelta(delta)
                else:
                    event = ReasoningDelta(delta)
                try:
                    yield event
                except GeneratorExit:
                    stop_requested.set()
                    self._interrupt(holder.get("turn"))
                    raise
                finally:
                    resumed = self._clock()
                    paused_seconds = max(0.0, resumed - paused_at)
                    began += paused_seconds
                    if visible_delta:
                        last_event = resumed
                    else:
                        # Reasoning is intentionally hidden from speech. It must
                        # not reset the deadline for the first visible token.
                        last_event += paused_seconds
                continue
            if method == "thread/tokenUsage/updated":
                usage = _usage(payload)
                continue
            if method == "model/rerouted":
                candidate = field_value(payload, "to_model")
                if candidate is None:
                    candidate = field_value(payload, "toModel")
                if isinstance(candidate, str) and candidate:
                    resolved_model = candidate
                continue
            if method != "turn/completed":
                continue

            turn_payload = field_value(payload, "turn", payload)
            status = field_value(turn_payload, "status")
            if hasattr(status, "value"):
                status = status.value
            if status != "completed":
                stop_requested.set()
                error = field_value(turn_payload, "error")
                category, retryable = _classify_exception(
                    RuntimeError(str(field_value(error, "message", status)))
                )
                if status == "interrupted":
                    category = ErrorCategory.CANCELLED
                    retryable = False
                raise self._error(
                    category,
                    model=request.model,
                    retryable=retryable,
                    transmitted=True,
                    request_id=request_id,
                )
            if not saw_visible_text:
                stop_requested.set()
                raise self._error(
                    ErrorCategory.EMPTY_COMPLETION,
                    model=request.model,
                    transmitted=True,
                    request_id=request_id,
                )
            saw_completed = True
            yield Completed(
                CompletionMetadata(
                    provider=self.identity.name,
                    requested_model=request.model,
                    resolved_model=resolved_model,
                    finish_reason=FinishReason.STOP,
                    provider_finish_reason="completed",
                    usage=usage,
                    request_id=request_id,
                )
            )
            last_event = self._clock()

    def warm_up(self, model: str) -> None:
        if not model.strip():
            raise ValueError("model cannot be empty")

    def close(self) -> None:
        # Do not wait for account_kind(): closing the runtime is what can
        # release a blocked app-server RPC during shutdown.
        with self._runtime_lock:
            if self._closed:
                return
            self._closed = True
            self._chatgpt_account_verified = False
            runtime = self._runtime
            self._runtime = None
        with self._context_state_lock:
            self._reset_context_locked()
        if runtime is not None:
            try:
                runtime.close()
            except Exception:
                pass
