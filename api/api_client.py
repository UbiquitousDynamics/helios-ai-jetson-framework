"""Backward-compatible public LLM client over the hybrid provider subsystem."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import config
from api.budget import BudgetError, BudgetLedger, BudgetLimits
from api.catalog import CatalogError, ModelCatalog
from api.health import HealthTracker
from api.metrics import SafeMetricsRecorder
from api.privacy import PrivacyGuard, PrivacyPolicy
from api.provider_factory import configured_provider_factory
from api.providers.contracts import (
    ChatMessage,
    ChatProvider,
    ChatRequest,
    CancellationToken,
    ContentOrigin,
    ErrorCategory,
    PrivacyLevel,
    ProviderError,
    Role,
    Timeouts,
)
from api.providers.ollama import OllamaAdapter
from api.routing import (
    Connectivity,
    NoRouteError,
    ProviderRegistry,
    RoutePlanner,
    RoutingPolicy,
)
from api.streaming import (
    CancellationController,
    ExecutionTarget,
    SpeechReplayUnsafeError,
    StreamingResponseCoordinator,
)
from api.target_compiler import TargetCompiler

logger = logging.getLogger(__name__)

_HYBRID_SYSTEM_INSTRUCTIONS = {
    "it": (
        "Sei Emilia, il veicolo solare dotato di intelligenza artificiale. "
        "Rispondi sempre in italiano, direttamente e con precisione. "
        "Non usare Markdown nella conversazione vocale. "
        "Quando puoi fare una scelta ragionevole, falla invece di chiedere chiarimenti."
    ),
    "en": (
        "You are Emilia, the solar vehicle with artificial intelligence. "
        "Always answer in English, directly and precisely. "
        "Do not use Markdown in voice conversation. "
        "When you can make a reasonable choice, make it instead of asking for clarification."
    ),
}


class TextToSpeech(Protocol):
    def speak(self, text: str) -> Any: ...


class APIClientError(RuntimeError):
    """A sanitized model-routing failure recoverable by ``VoiceAssistant``."""


def _content_safe_jsonl_sink(
    path: Path,
    *,
    retention_days: int,
) -> Callable[[Mapping[str, Any]], None]:
    """Return a lazy, daily-pruned sink for the content-free metric schema."""

    last_pruned_date = None

    def parse_timestamp(value: Any) -> datetime:
        if not isinstance(value, str):
            raise ValueError("metric timestamp is missing")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("metric timestamp is invalid") from None
        if parsed.tzinfo is None:
            raise ValueError("metric timestamp must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    def prune(now: datetime) -> None:
        if not path.exists():
            return
        cutoff = now - timedelta(days=retention_days)
        retained: list[str] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.endswith("\n"):
                    raise ValueError("metric file has a truncated record")
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    raise ValueError("metric file has a malformed record") from None
                if not isinstance(payload, Mapping):
                    raise ValueError("metric file record must be an object")
                if parse_timestamp(payload.get("timestamp")) >= cutoff:
                    retained.append(line)
        temporary = path.with_name(path.name + ".retention.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(retained)
            handle.flush()
        temporary.replace(path)

    def write(payload: Mapping[str, Any]) -> None:
        nonlocal last_pruned_date
        timestamp = parse_timestamp(payload.get("timestamp"))
        path.parent.mkdir(parents=True, exist_ok=True)
        if last_pruned_date != timestamp.date():
            prune(timestamp)
            last_pruned_date = timestamp.date()
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    dict(payload),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            handle.write("\n")

    return write


class APIClient:
    """Provider-neutral implementation of the original ``talk``/``think`` API.

    With no hybrid routing file, behavior stays Ollama-only and keeps the exact
    legacy SDK payload, retry count, streaming sentence boundaries, and lazy
    construction. Remote providers require validated configuration, explicit
    privacy permission, and (by default) a fresh priced catalog plus ledger.
    """

    def __init__(
        self,
        api_url: str = config.OLLAMA_API_URL,
        model_talk: str = config.MODEL_TALK,
        model_think: str = config.MODEL_THINK,
        tts: TextToSpeech | None = None,
        *,
        client: Any | None = None,
        client_factory: Callable[[str], Any] | None = None,
        warm_up: bool = False,
        retry_attempts: int = 3,
        retry_wait: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        language: str = config.LANGUAGE,
        llm_settings: config.LLMSettings | None = None,
        provider_registry: ProviderRegistry | None = None,
        providers: Mapping[str, ChatProvider] | None = None,
        privacy_guard: PrivacyGuard | None = None,
        health_tracker: HealthTracker | None = None,
        model_catalog: ModelCatalog | None = None,
        budget_ledger: BudgetLedger | None = None,
        metrics: SafeMetricsRecorder | None = None,
        connectivity: (Connectivity | str | Callable[[], Connectivity | str] | None) = None,
        network_monitor: Any | None = None,
    ) -> None:
        if retry_attempts < 1:
            raise ValueError("retry_attempts must be at least one")
        if retry_wait < 0:
            raise ValueError("retry_wait cannot be negative")

        self.host = config.normalize_ollama_host(api_url)
        self.models = {"talk": model_talk, "think": model_think}
        self.language = language.strip().lower()
        self.llm_settings = config.LLM_SETTINGS if llm_settings is None else llm_settings
        self._mode_settings_by_name = {
            "talk": self.llm_settings.talk,
            "think": self.llm_settings.think,
        }
        self._timeouts_by_mode = {
            mode: self._compile_timeouts(settings)
            for mode, settings in self._mode_settings_by_name.items()
        }
        if self.llm_settings.routing_file is not None:
            instruction = _HYBRID_SYSTEM_INSTRUCTIONS.get(
                self.language,
                _HYBRID_SYSTEM_INSTRUCTIONS["en"],
            )
            self._hybrid_system_message: ChatMessage | None = ChatMessage(
                Role.SYSTEM,
                instruction,
                origin=ContentOrigin.STATIC_INSTRUCTION,
            )
        else:
            self._hybrid_system_message = None
        self.retry_attempts = retry_attempts
        self.retry_wait = retry_wait
        self._sleep = sleep
        self._tts = tts
        self._owns_tts = False
        self._closed = False
        self._cancellation_lock = threading.Lock()
        self._active_cancellations: list[CancellationToken] = []
        self._remote_prepare_lock = threading.Lock()
        self._remote_prepare_thread: threading.Thread | None = None
        self._remote_prepared = False
        if connectivity is not None and network_monitor is not None:
            raise ValueError("pass either connectivity or network_monitor, not both")
        self._network_monitor = network_monitor
        self._owns_network_monitor = False
        if (
            connectivity is None
            and network_monitor is None
            and self.llm_settings.remote_enabled
            and self.llm_settings.network.enabled
        ):
            from api.connectivity import ConnectivityMonitor

            self._network_monitor = ConnectivityMonitor(self.llm_settings.network)
            self._owns_network_monitor = True
        if self._network_monitor is not None:
            self._connectivity_source = self._network_monitor.connectivity
        else:
            self._connectivity_source = (
                Connectivity.UNKNOWN if connectivity is None else connectivity
            )

        self._ollama = OllamaAdapter(
            self.host,
            client=client,
            client_factory=client_factory,
        )
        self._registry = provider_registry or ProviderRegistry()
        self._owns_registry = provider_registry is None
        self._registered_providers: set[str] = set()
        self._register_provider(
            "ollama",
            self._ollama,
            owned=self._owns_registry,
        )
        for provider_name, provider in (providers or {}).items():
            if provider_name == "ollama":
                continue
            self._register_provider(provider_name, provider, owned=False)
        self._register_configured_providers()

        health = health_tracker or HealthTracker(
            failures_to_open=self.llm_settings.health.failures_to_open,
            cooldown_seconds=self.llm_settings.health.cooldown_seconds,
            maximum_cooldown_seconds=(self.llm_settings.health.maximum_cooldown_seconds),
        )
        self.health = health
        self.privacy = privacy_guard or PrivacyGuard(
            PrivacyPolicy(
                remote_enabled=(
                    self.llm_settings.remote_enabled and not self.llm_settings.emergency_local_only
                ),
                allow_remote_transcripts=(self.llm_settings.privacy.allow_remote_transcripts),
                allow_remote_context=self.llm_settings.privacy.allow_remote_context,
                allow_remote_rag_context=(self.llm_settings.privacy.allow_remote_rag_context),
            )
        )
        self.catalog = model_catalog or self._load_catalog()
        self.budget = budget_ledger or self._load_budget()
        self._owns_metrics = metrics is None
        self.metrics = metrics or self._build_metrics()
        self._coordinator = StreamingResponseCoordinator(
            self._registry,
            privacy=self.privacy,
            health=self.health,
            budget=self.budget,
            metrics=self.metrics,
            require_priced_remote=self.llm_settings.budget.enabled,
            retry_wait=retry_wait,
            maximum_retry_delay=max(5.0, retry_wait),
            sleep=sleep,
        )
        ollama_remote, ollama_enabled = self._ollama_classification()
        self._execution_targets = TargetCompiler(
            self.llm_settings,
            models=self.models,
            language=self.language,
            default_retry_attempts=self.retry_attempts,
            registered_providers=self._registered_providers,
            ollama_remote=ollama_remote,
            ollama_enabled=ollama_enabled,
            catalog=self.catalog,
        ).compile_all()
        self._execution_by_name = {
            mode: {execution.route.name: execution for execution in executions}
            for mode, executions in self._execution_targets.items()
        }
        self._route_planners = {
            mode: self._compile_route_planner(mode, executions)
            for mode, executions in self._execution_targets.items()
        }
        if self._network_monitor is not None:
            set_online_callback = getattr(
                self._network_monitor,
                "set_online_callback",
                None,
            )
            if callable(set_online_callback):
                set_online_callback(self.prepare_remote_async)
            start_monitor = getattr(self._network_monitor, "start", None)
            if callable(start_monitor):
                start_monitor()

        if warm_up:
            self.warm_up()

    @property
    def client(self) -> Any:
        """Expose the raw Ollama client for backward compatibility."""

        return self._ollama.client

    @client.setter
    def client(self, value: Any) -> None:
        self._ollama.client = value

    @property
    def configured_tts(self) -> TextToSpeech | None:
        """Return injected TTS without triggering the lazy Piper model."""

        return self._tts

    @property
    def tts(self) -> TextToSpeech:
        if self._tts is None:
            from audio.tts import PiperTTS

            self._tts = PiperTTS()
            self._owns_tts = True
        return self._tts

    @tts.setter
    def tts(self, value: TextToSpeech) -> None:
        self._tts = value
        self._owns_tts = False

    @property
    def connectivity(self) -> Connectivity:
        source = self._connectivity_source
        value = source() if callable(source) else source
        return Connectivity(value)

    @connectivity.setter
    def connectivity(self, value: Connectivity | str) -> None:
        self._connectivity_source = Connectivity(value)

    @property
    def network_snapshot(self) -> Any | None:
        monitor = self._network_monitor
        if monitor is None:
            return None
        snapshot = getattr(monitor, "snapshot", None)
        return snapshot() if callable(snapshot) else None

    def _register_provider(
        self,
        name: str,
        provider: ChatProvider,
        *,
        owned: bool,
    ) -> None:
        self._registry.register_instance(name, provider, owned=owned)
        self._registered_providers.add(name)

    def _register_configured_providers(self) -> None:
        for provider in self.llm_settings.providers:
            if not provider.enabled or provider.name in self._registered_providers:
                continue
            factory = configured_provider_factory(provider)
            if factory is None:
                logger.error(
                    "Provider %s uses an unsupported or incomplete adapter configuration",
                    provider.name,
                )
                continue

            self._registry.register(provider.name, factory, owned=True)
            self._registered_providers.add(provider.name)

    def _load_catalog(self) -> ModelCatalog | None:
        path = self.llm_settings.budget.catalog_path
        if path is None:
            return None
        try:
            catalog = ModelCatalog.load(path)
            catalog.require_fresh()
            return catalog
        except CatalogError:
            logger.error("Remote model catalog is unavailable or stale")
            return None

    def _load_budget(self) -> BudgetLedger | None:
        settings = self.llm_settings.budget
        if not settings.enabled or settings.ledger_path is None:
            return None
        limits = BudgetLimits(
            per_request_usd=settings.per_request_usd,
            daily_usd=settings.daily_usd,
            monthly_usd=settings.monthly_usd,
            zero_cost_only=settings.zero_cost_only,
        )
        try:
            return BudgetLedger(settings.ledger_path, limits)
        except (BudgetError, OSError, ValueError):
            logger.error("Remote budget ledger is unavailable; remote routing will fail closed")
            return None

    def _build_metrics(self) -> SafeMetricsRecorder:
        settings = self.llm_settings.observability
        sink = (
            _content_safe_jsonl_sink(
                settings.metrics_path,
                retention_days=settings.metrics_retention_days,
            )
            if settings.metrics_path is not None
            else None
        )
        return SafeMetricsRecorder(
            enabled=settings.metrics_enabled,
            sink=sink,
            asynchronous=sink is not None,
        )

    def _ollama_classification(self) -> tuple[bool, bool]:
        provider = next(
            (candidate for candidate in self.llm_settings.providers if candidate.name == "ollama"),
            None,
        )
        if provider is None:
            return self._ollama.identity.remote, True
        matches = (
            provider.adapter == "ollama"
            and config.normalize_ollama_host(provider.endpoint) == self.host
        )
        if not matches:
            return self._ollama.identity.remote, False
        return provider.locality == "remote", provider.enabled

    def _mode_settings(self, mode: str) -> config.LLMModeSettings:
        try:
            return self._mode_settings_by_name[mode]
        except KeyError:
            raise ValueError(f"Unknown model mode: {mode!r}") from None

    def _compile_timeouts(self, mode_settings: config.LLMModeSettings) -> Timeouts:
        values = self.llm_settings.timeouts
        mode_first_token = mode_settings.first_visible_token_seconds
        return Timeouts(
            connect_seconds=values.connect_seconds,
            first_token_seconds=(
                values.first_token_seconds if mode_first_token is None else float(mode_first_token)
            ),
            read_seconds=values.read_seconds,
            total_seconds=values.total_seconds,
        )

    def _timeouts(self, mode: str) -> Timeouts:
        try:
            return self._timeouts_by_mode[mode]
        except KeyError:
            raise ValueError(f"Unknown model mode: {mode!r}") from None

    def _compile_route_planner(
        self,
        mode: str,
        executions: tuple[ExecutionTarget, ...],
    ) -> RoutePlanner:
        mode_settings = self._mode_settings(mode)
        emergency = self.llm_settings.emergency_local_only
        return RoutePlanner(
            (execution.route for execution in executions),
            allowlist=() if emergency else self.llm_settings.allowlist,
            denylist=() if emergency else self.llm_settings.denylist,
            health=self.health,
            auto_complexity_threshold=mode_settings.complexity_threshold,
            allow_remote_when_connectivity_unknown=(
                self.llm_settings.unknown_connectivity == "allow_remote"
            ),
        )

    def _request(
        self,
        *,
        mode: str,
        message: str,
        context: str | None,
        context_origin: ContentOrigin,
        message_redacted: bool,
        context_redacted: bool,
        privacy: PrivacyLevel | str | None,
        request_options: Mapping[str, Any] | None,
    ) -> ChatRequest:
        messages: list[ChatMessage] = (
            [self._hybrid_system_message] if self._hybrid_system_message is not None else []
        )
        if context:
            messages.append(
                ChatMessage(
                    Role.SYSTEM,
                    context,
                    origin=context_origin,
                    redacted=context_redacted,
                )
            )
        messages.append(
            ChatMessage(
                Role.USER,
                message,
                origin=ContentOrigin.RAW_TRANSCRIPT,
                redacted=message_redacted,
            )
        )
        selected_privacy = PrivacyLevel(privacy or self.llm_settings.privacy.default)
        return ChatRequest(
            model=self.models[mode],
            messages=tuple(messages),
            mode=mode,
            language=self.language,
            privacy=selected_privacy,
            max_output_tokens=(
                self._mode_settings(mode).max_output_tokens
                if self._hybrid_system_message is not None
                else None
            ),
            timeouts=self._timeouts(mode),
            options=dict(request_options or {}),
        )

    def _plan(
        self,
        request: ChatRequest,
        *,
        connectivity: Connectivity | str | None,
    ) -> tuple[ExecutionTarget, ...]:
        executions = self._execution_targets[request.mode]
        request_for_planning = request
        privacy_error: ProviderError | None = None
        if any(execution.route.remote for execution in executions):
            try:
                request_for_planning = self.privacy.authorize_remote(request)
            except ProviderError as error:
                privacy_error = error

        selected_connectivity = (
            self.connectivity if connectivity is None else Connectivity(connectivity)
        )
        policy = RoutingPolicy(self.llm_settings.routing_policy)
        if (
            selected_connectivity is Connectivity.UNKNOWN
            and self.llm_settings.unknown_connectivity == "prefer_local"
            and policy is RoutingPolicy.REMOTE_FIRST
        ):
            policy = RoutingPolicy.LOCAL_FIRST

        planner = self._route_planners[request.mode]
        try:
            routes = planner.plan(
                request_for_planning,
                connectivity=selected_connectivity,
                policy=policy,
            )
        except NoRouteError:
            if privacy_error is not None:
                raise privacy_error from None
            raise ProviderError(
                category=ErrorCategory.PROVIDER_UNAVAILABLE,
                safe_message="No eligible language-model route is available",
                provider="router",
                model=request.model,
                transmitted=False,
            ) from None

        by_name = self._execution_by_name[request.mode]
        planned = tuple(by_name[route.name] for route in routes)
        planned_names = {execution.route.name for execution in planned}
        for execution in executions:
            if execution.route.name in planned_names:
                continue
            snapshot = self.health.snapshot(execution.route.health_key)
            if not snapshot.available:
                logger.warning(
                    "Route %s excluded by provider health (status=%s, retry_after_seconds=%s)",
                    execution.route.name,
                    snapshot.status.value,
                    (
                        round(snapshot.retry_after_seconds, 1)
                        if snapshot.retry_after_seconds is not None
                        else None
                    ),
                )
        return planned

    def warm_up(self, mode: str = "talk") -> None:
        """Explicitly load the local Ollama model; remote warm-up is forbidden."""

        if mode not in self.models:
            raise ValueError(f"Unknown model mode: {mode!r}")
        local_target = next(
            (
                execution
                for execution in self._execution_targets[mode]
                if execution.route.provider == "ollama"
                and not execution.route.remote
                and execution.route.enabled
            ),
            None,
        )
        if local_target is None:
            raise APIClientError("Remote or disabled Ollama targets cannot be warmed up")
        self._call_with_retry(
            lambda: self._ollama.warm_up(local_target.route.model),
            operation_name=f"warm up the {mode} model",
        )

    def prepare_remote_async(self) -> threading.Thread | None:
        """Start the non-inference Codex startup/authentication path in background."""

        if not self.llm_settings.remote_enabled or self.llm_settings.emergency_local_only:
            return None
        if self._network_monitor is not None and self.connectivity is not Connectivity.ONLINE:
            logger.info("Skipping remote preparation while the network gate is not online")
            return None
        provider_names = tuple(
            provider.name
            for provider in self.llm_settings.providers
            if provider.enabled
            and provider.adapter == "codex_app_server"
            and any(
                execution.route.enabled
                and execution.route.remote
                and execution.route.provider == provider.name
                for executions in self._execution_targets.values()
                for execution in executions
            )
        )
        if not provider_names:
            return None

        with self._remote_prepare_lock:
            if self._closed or self._remote_prepared:
                return self._remote_prepare_thread
            if self._remote_prepare_thread is not None and self._remote_prepare_thread.is_alive():
                return self._remote_prepare_thread

            def prepare() -> None:
                succeeded = True
                for provider_name in provider_names:
                    started_at = time.monotonic()
                    try:
                        provider = self._registry.get(provider_name)
                        operation = getattr(provider, "prepare", None)
                        if callable(operation):
                            operation()
                            logger.info(
                                "Remote provider prepared without inference "
                                "(provider=%s, startup_ms=%s)",
                                provider_name,
                                round((time.monotonic() - started_at) * 1_000),
                            )
                    except ProviderError as error:
                        succeeded = False
                        logger.warning(
                            "Remote provider preparation failed "
                            "(provider=%s, category=%s); normal fallback remains active",
                            provider_name,
                            error.category.value,
                        )
                    except Exception:
                        succeeded = False
                        logger.warning(
                            "Remote provider preparation failed "
                            "(provider=%s); normal fallback remains active",
                            provider_name,
                        )
                with self._remote_prepare_lock:
                    self._remote_prepared = succeeded

            thread = threading.Thread(
                target=prepare,
                name="helios-remote-prepare",
                daemon=True,
            )
            self._remote_prepare_thread = thread
            thread.start()
            return thread

    def _call_with_retry(
        self,
        operation: Callable[[], Any],
        *,
        operation_name: str = "contact Ollama",
    ) -> Any:
        last_error: ProviderError | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return operation()
            except ProviderError as error:
                last_error = error
                if not error.retryable_same_provider:
                    raise APIClientError(f"Unable to {operation_name}") from None
                if attempt >= self.retry_attempts:
                    break
                logger.warning(
                    "Unable to %s (attempt %s/%s; category=%s)",
                    operation_name,
                    attempt,
                    self.retry_attempts,
                    error.category.value,
                )
                if self.retry_wait:
                    self._sleep(self.retry_wait)
            except Exception:
                raise APIClientError(f"Unable to {operation_name}") from None

        assert last_error is not None
        raise APIClientError(
            f"Unable to {operation_name} after {self.retry_attempts} attempt(s)"
        ) from None

    def _stream(
        self,
        *,
        mode: str,
        message: str,
        context: str | None,
        speak: bool,
        privacy: PrivacyLevel | str | None = None,
        context_origin: ContentOrigin = ContentOrigin.UNKNOWN,
        message_redacted: bool = False,
        context_redacted: bool = False,
        connectivity: Connectivity | str | None = None,
        request_options: Mapping[str, Any] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        if not message or not message.strip():
            raise ValueError("message cannot be empty")
        if mode not in self.models:
            raise ValueError(f"Unknown model mode: {mode!r}")

        active_cancellation = cancellation or CancellationController()
        with self._cancellation_lock:
            if self._closed:
                raise APIClientError("Language-model client is closed")
            self._active_cancellations.append(active_cancellation)

        try:
            request = self._request(
                mode=mode,
                message=message,
                context=context,
                context_origin=context_origin,
                message_redacted=message_redacted,
                context_redacted=context_redacted,
                privacy=privacy,
                request_options=request_options,
            )
            executions = self._plan(request, connectivity=connectivity)
            logger.info(
                "Planning %s request with eligible routes in fallback order: %s",
                mode,
                ",".join(execution.route.name for execution in executions),
            )
            result = self._coordinator.run(
                request,
                executions,
                speak=self.tts.speak if speak else None,
                first_speech_min_chars=(self._mode_settings(mode).first_speech_min_chars),
                speech_chunk_max_chars=(self._mode_settings(mode).speech_chunk_max_chars),
                maximum_first_audio_seconds=(
                    self.llm_settings.health.maximum_talk_first_audio_ms / 1_000
                    if mode == "talk" and speak
                    else None
                ),
                cancellation=active_cancellation,
                route_reason=self.llm_settings.routing_policy,
            )
            logger.info(
                "Completed %s request using route %s "
                "(provider=%s, requested_model=%s, resolved_model=%s, "
                "attempts=%s, first_text_ms=%s, first_speech_ms=%s)",
                mode,
                result.target.name,
                result.target.provider,
                result.target.model,
                result.metadata.resolved_model or result.target.model,
                result.attempts,
                (
                    round(result.first_token_seconds * 1_000)
                    if result.first_token_seconds is not None
                    else None
                ),
                (
                    round(result.first_audio_seconds * 1_000)
                    if result.first_audio_seconds is not None
                    else None
                ),
            )
            return result.text
        except SpeechReplayUnsafeError:
            raise APIClientError(
                "Model stream was interrupted after speech output began; "
                "the request was not retried"
            ) from None
        except ProviderError as error:
            attempts = error.attempts
            if attempts > 1:
                raise APIClientError(
                    f"Unable to stream a model response after {attempts} attempt(s)"
                ) from None
            raise APIClientError(
                f"Unable to stream a model response ({error.category.value})"
            ) from None
        finally:
            with self._cancellation_lock:
                self._active_cancellations = [
                    token
                    for token in self._active_cancellations
                    if token is not active_cancellation
                ]

    def talk(
        self,
        message: str,
        context: str | None = None,
        *,
        privacy: PrivacyLevel | str | None = None,
        context_origin: ContentOrigin = ContentOrigin.UNKNOWN,
        message_redacted: bool = False,
        context_redacted: bool = False,
        connectivity: Connectivity | str | None = None,
        request_options: Mapping[str, Any] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """Stream a conversational response and speak sentences as they arrive."""

        return self._stream(
            mode="talk",
            message=message,
            context=context,
            speak=True,
            privacy=privacy,
            context_origin=context_origin,
            message_redacted=message_redacted,
            context_redacted=context_redacted,
            connectivity=connectivity,
            request_options=request_options,
            cancellation=cancellation,
        )

    def think(
        self,
        message: str,
        context: str | None = None,
        tts: bool = False,
        *,
        privacy: PrivacyLevel | str | None = None,
        context_origin: ContentOrigin = ContentOrigin.UNKNOWN,
        message_redacted: bool = False,
        context_redacted: bool = False,
        connectivity: Connectivity | str | None = None,
        request_options: Mapping[str, Any] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """Stream a reasoning response and optionally speak it."""

        return self._stream(
            mode="think",
            message=message,
            context=context,
            speak=tts,
            privacy=privacy,
            context_origin=context_origin,
            message_redacted=message_redacted,
            context_redacted=context_redacted,
            connectivity=connectivity,
            request_options=request_options,
            cancellation=cancellation,
        )

    def cancel_current(self) -> None:
        """Request cancellation of all model streams currently owned by this client."""

        with self._cancellation_lock:
            active = tuple(self._active_cancellations)
        for token in active:
            cancel = getattr(token, "cancel", None)
            if callable(cancel):
                cancel()

    def close(self) -> None:
        with self._cancellation_lock:
            if self._closed:
                return
            self._closed = True
        self.cancel_current()
        if self._owns_network_monitor and self._network_monitor is not None:
            close_monitor = getattr(self._network_monitor, "close", None)
            if callable(close_monitor):
                close_monitor()
        try:
            if self._owns_registry:
                self._registry.close()
            else:
                self._ollama.close()
        except Exception:
            logger.warning("Unable to close a language-model provider")
        if self._owns_metrics:
            try:
                self.metrics.close()
            except Exception:
                logger.warning("Unable to flush language-model metrics")
        if self._owns_tts and self._tts is not None:
            close = getattr(self._tts, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.warning("Unable to close language-model speech output")

    def __enter__(self) -> APIClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
