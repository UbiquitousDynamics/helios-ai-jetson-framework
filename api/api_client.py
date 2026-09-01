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
from api.conversation import ConversationSession, safe_conversation_identifier
from api.health import HealthTracker
from api.metrics import FanoutMetricSink, MetricEvent, SafeMetricsRecorder, record_safely
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
    RoutingDecision,
    RoutingPolicy,
)
from api.streaming import (
    CancellationController,
    ExecutionTarget,
    SpeechReplayUnsafeError,
    StreamingFailureContext,
    StreamingResponseCoordinator,
)
from api.target_compiler import TargetCompiler
from audio.speech_pipeline import SpeechPipeline

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
    allowed_fields = set(MetricEvent.__dataclass_fields__) - {"request_id", "attempt_id"}

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
        temporary = path.with_name(path.name + ".retention.tmp")
        try:
            with (
                path.open("rb") as source,
                temporary.open("w", encoding="utf-8", newline="\n") as destination,
            ):
                record = source.readline()
                while record:
                    next_record = source.readline()
                    is_final_record = not next_record
                    try:
                        line = record.decode("utf-8").rstrip("\r\n")
                        payload = json.loads(line)
                        if not isinstance(payload, Mapping):
                            raise ValueError("metric file record must be an object")
                        record_timestamp = parse_timestamp(payload.get("timestamp"))
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                        if is_final_record:
                            # The JSONL append and its trailing newline are
                            # separate writes. A hard kill can therefore leave
                            # only the final physical record incomplete. Drop
                            # that tail, while never masking corruption in an
                            # earlier durable record.
                            break
                        raise ValueError("metric file has a malformed interior record") from None
                    if record_timestamp >= cutoff:
                        destination.write(line)
                        destination.write("\n")
                    record = next_record
                destination.flush()
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        temporary.replace(path)

    def write(payload: Mapping[str, Any]) -> None:
        nonlocal last_pruned_date
        timestamp = parse_timestamp(payload.get("timestamp"))
        sanitized = {name: payload[name] for name in allowed_fields if name in payload}
        if not isinstance(sanitized.get("event"), str):
            raise ValueError("metric event is missing")
        path.parent.mkdir(parents=True, exist_ok=True)
        if last_pruned_date != timestamp.date():
            prune(timestamp)
            last_pruned_date = timestamp.date()
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    sanitized,
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
        kpi_settings: config.KPISettings | None = None,
        conversation_session: ConversationSession | None = None,
        # Escape hatch: forces the fully synchronous speech path if a device
        # turns out to misbehave with concurrent synthesis and playback.
        overlapped_speech: bool = True,
    ) -> None:
        if retry_attempts < 1:
            raise ValueError("retry_attempts must be at least one")
        if retry_wait < 0:
            raise ValueError("retry_wait cannot be negative")

        self.host = config.normalize_ollama_host(api_url)
        self.models = {"talk": model_talk, "think": model_think}
        self.language = language.strip().lower()
        self.llm_settings = config.LLM_SETTINGS if llm_settings is None else llm_settings
        self.conversation = conversation_session or ConversationSession(
            idle_timeout_seconds=self.llm_settings.context_idle_timeout_seconds,
            max_history_turns=self.llm_settings.context_max_turns,
        )
        self._conversation_request_lock = threading.Lock()
        self.kpi_settings = kpi_settings or config.KPISettings()
        self._kpi_service: Any | None = None
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
        self._speech_pipeline_lock = threading.Lock()
        self._speech_pipeline: SpeechPipeline | None = None
        self._overlapped_speech_enabled = overlapped_speech
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
        # Start background KPI resources only after static target compilation
        # succeeds, then unwind them if later active initialization fails.
        self._owns_metrics = metrics is None
        self.metrics = metrics or self._build_metrics()
        set_runtime_health_provider = getattr(
            self._kpi_service,
            "set_runtime_health_provider",
            None,
        )
        if callable(set_runtime_health_provider):
            try:
                set_runtime_health_provider(self._runtime_health_snapshot)
            except Exception:
                logger.warning("KPI runtime health registration is unavailable")
        set_health_observer = getattr(self.health, "set_transition_observer", None)
        if callable(set_health_observer):
            set_health_observer(self._record_health_transition)
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
            activity_tracker=getattr(self._kpi_service, "activity_tracker", None),
        )
        try:
            if self._network_monitor is not None:
                set_snapshot_observer = getattr(
                    self._network_monitor,
                    "set_snapshot_observer",
                    None,
                )
                if callable(set_snapshot_observer):
                    set_snapshot_observer(self._record_network_snapshot)
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
        except Exception:
            self.close()
            raise

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
        attach_conversation = getattr(provider, "attach_conversation_session", None)
        if callable(attach_conversation):
            attach_conversation(self.conversation)
        self._registry.register_instance(name, provider, owned=owned)
        self._registered_providers.add(name)

    def _register_configured_providers(self) -> None:
        for provider in self.llm_settings.providers:
            if not provider.enabled or provider.name in self._registered_providers:
                continue
            factory = configured_provider_factory(
                provider,
                allow_remote_context=self.llm_settings.privacy.allow_remote_context,
                context_idle_timeout_seconds=(self.llm_settings.context_idle_timeout_seconds),
                context_max_turns=self.llm_settings.context_max_turns,
                conversation_session=self.conversation,
            )
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
        legacy_sink = (
            _content_safe_jsonl_sink(
                settings.metrics_path,
                retention_days=settings.metrics_retention_days,
            )
            if settings.metrics_path is not None
            else None
        )
        if self.kpi_settings.enabled or self.kpi_settings.dashboard_enabled:
            try:
                from observability.service import ObservabilityService

                self._kpi_service = ObservabilityService(
                    self.kpi_settings,
                    additional_sinks=(
                        (legacy_sink,)
                        if legacy_sink is not None and settings.metrics_enabled
                        else ()
                    ),
                )
                return self._kpi_service.recorder
            except Exception:
                logger.warning(
                    "KPI subsystem could not start; language-model behavior is unchanged"
                )
        sink = FanoutMetricSink((legacy_sink,)) if legacy_sink is not None else None
        return SafeMetricsRecorder(
            enabled=settings.metrics_enabled,
            sink=sink,
            asynchronous=sink is not None,
        )

    @staticmethod
    def _network_quality_tier(score: float | None, state: str | None = None) -> str:
        if state == "offline":
            return "offline"
        if score is None:
            return "unknown"
        if score < 0.25:
            return "poor"
        if score < 0.5:
            return "fair"
        if score < 0.75:
            return "good"
        return "excellent"

    def _record_health_transition(self, previous: Any, current: Any) -> None:
        provider, separator, model = str(getattr(current, "key", "")).partition("/")
        record_safely(
            self.metrics,
            "provider_health_changed",
            provider=provider or "unknown",
            model=model if separator and model else None,
            previous_circuit_state=getattr(getattr(previous, "status", None), "value", None),
            circuit_state=getattr(getattr(current, "status", None), "value", None),
            outcome="transition",
        )

    def _runtime_health_snapshot(self) -> dict[str, object]:
        """Read configured circuit state from the in-memory health tracker only."""

        configured: dict[str, dict[str, Any]] = {}
        for mode, executions in self._execution_targets.items():
            for execution in executions:
                route = execution.route
                target = configured.setdefault(
                    route.health_key,
                    {
                        "provider": route.provider,
                        "model": route.model,
                        "locality": "remote" if route.remote else "local",
                        "enabled": False,
                        "modes": set(),
                        "routes": set(),
                    },
                )
                target["enabled"] = bool(target["enabled"] or route.enabled)
                target["modes"].add(mode)
                target["routes"].add(route.name)

        providers: list[dict[str, object]] = []
        for health_key, target in sorted(configured.items()):
            snapshot = self.health.snapshot(health_key)
            enabled = bool(target["enabled"])
            available = enabled and bool(snapshot.available)
            providers.append(
                {
                    "provider": str(target["provider"]),
                    "model": str(target["model"]),
                    "locality": str(target["locality"]),
                    "enabled": enabled,
                    "available": available,
                    "circuit_state": snapshot.status.value,
                    "retry_after_seconds": snapshot.retry_after_seconds,
                    "consecutive_failures": snapshot.consecutive_failures,
                    "successes": snapshot.successes,
                    "failures": snapshot.failures,
                    "latency_ewma_ms": (
                        snapshot.latency_ewma_seconds * 1_000
                        if snapshot.latency_ewma_seconds is not None
                        else None
                    ),
                    "modes": sorted(target["modes"]),
                    "routes": sorted(target["routes"]),
                }
            )

        local_available = any(
            provider["locality"] == "local" and provider["available"] is True
            for provider in providers
        )
        remote_available = any(
            provider["locality"] == "remote" and provider["available"] is True
            for provider in providers
        )
        return {
            "status": "healthy" if local_available or remote_available else "degraded",
            "local_available": local_available,
            "remote_available": remote_available,
            "providers": providers,
        }

    def _record_network_snapshot(self, previous: Any, current: Any) -> None:
        state = getattr(getattr(current, "connectivity", None), "value", None)
        previous_state = getattr(getattr(previous, "connectivity", None), "value", None)
        score = getattr(current, "quality_score", None)
        record_safely(
            self.metrics,
            ("network_state_changed" if previous_state != state else "network_probe_completed"),
            network_state=state,
            network_quality_score=score,
            network_quality_tier=self._network_quality_tier(score, state),
            network_reason=getattr(current, "reason", None),
            interface_available=getattr(current, "interface", None) is not None,
            interface_kind=getattr(current, "interface_kind", None),
            probe_success=state == "online",
            probe_success_ratio=(
                1.0 - getattr(current, "loss_ratio")
                if getattr(current, "loss_ratio", None) is not None
                else None
            ),
            dns_ms=getattr(current, "dns_ms", None),
            tcp_ms=getattr(current, "connect_ms", None),
            tls_ms=getattr(current, "tls_ms", None),
            ttfb_ms=getattr(current, "ttfb_ewma_ms", None),
            goodput_kbps=getattr(current, "goodput_ewma_kbps", None),
            success=state == "online",
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

    def _speech_callable(self) -> Callable[[str], Any]:
        """Prefer overlapped speech, then timing-aware, then legacy TTS.

        When the backend exposes the two-stage API, wrap it in a SpeechPipeline
        so synthesis of the next fragment runs during playback of the current
        one and provider events keep being read while audio plays. Backends
        without that API keep the fully synchronous behaviour.
        """

        with self._speech_pipeline_lock:
            if self._speech_pipeline is not None:
                return self._speech_pipeline
            synthesize = getattr(self.tts, "synthesize_fragment", None)
            play = getattr(self.tts, "play_fragment", None)
            if self._overlapped_speech_enabled and callable(synthesize) and callable(play):
                self._speech_pipeline = SpeechPipeline(synthesize=synthesize, play=play)
                logger.info("Overlapped speech pipeline enabled")
                return self._speech_pipeline

        speak_with_timing = getattr(self.tts, "speak_with_timing", None)
        return speak_with_timing if callable(speak_with_timing) else self.tts.speak

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
        history: tuple[ChatMessage, ...],
        conversation_id: str,
        conversation_turn: int,
        context: str | None,
        context_origin: ContentOrigin,
        message_redacted: bool,
        context_redacted: bool,
        privacy: PrivacyLevel | str | None,
        request_options: Mapping[str, Any] | None,
    ) -> ChatRequest:
        selected_privacy = PrivacyLevel(privacy or self.llm_settings.privacy.default)
        message_remote_eligible = selected_privacy is PrivacyLevel.REMOTE_ALLOWED or (
            selected_privacy is PrivacyLevel.REMOTE_REDACTED and message_redacted
        )
        context_remote_eligible = selected_privacy is PrivacyLevel.REMOTE_ALLOWED or (
            selected_privacy is PrivacyLevel.REMOTE_REDACTED and context_redacted
        )
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
                    remote_eligible=context_remote_eligible,
                )
            )
        messages.extend(history)
        messages.append(
            ChatMessage(
                Role.USER,
                message,
                origin=ContentOrigin.RAW_TRANSCRIPT,
                redacted=message_redacted,
                remote_eligible=message_remote_eligible,
            )
        )
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
            conversation_id=conversation_id,
            conversation_turn=conversation_turn,
        )

    def _plan(
        self,
        request: ChatRequest,
        *,
        connectivity: Connectivity | str | None,
    ) -> tuple[ExecutionTarget, ...]:
        planned, _decision, _snapshot, _connectivity = self._plan_detailed(
            request,
            connectivity=connectivity,
        )
        return planned

    def _plan_detailed(
        self,
        request: ChatRequest,
        *,
        connectivity: Connectivity | str | None,
    ) -> tuple[
        tuple[ExecutionTarget, ...],
        RoutingDecision,
        Any | None,
        Connectivity,
    ]:
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
        network_snapshot = self.network_snapshot if connectivity is None else None
        policy = RoutingPolicy(self.llm_settings.routing_policy)
        network_preferred_local = False
        if (
            selected_connectivity is Connectivity.UNKNOWN
            and self.llm_settings.unknown_connectivity == "prefer_local"
            and policy is RoutingPolicy.REMOTE_FIRST
        ):
            policy = RoutingPolicy.LOCAL_FIRST
            network_preferred_local = True

        planner = self._route_planners[request.mode]
        try:
            decision = planner.plan_detailed(
                request_for_planning,
                connectivity=selected_connectivity,
                policy=policy,
            )
        except NoRouteError as routing_error:
            rejected_decision = routing_error.decision
            if rejected_decision is not None:
                quality_score = getattr(network_snapshot, "quality_score", None)
                network_values = {
                    "network_state": selected_connectivity.value,
                    "network_quality_score": quality_score,
                    "network_quality_tier": self._network_quality_tier(
                        quality_score,
                        selected_connectivity.value,
                    ),
                    "network_reason": getattr(network_snapshot, "reason", None),
                    "network_forced_local": False,
                }
                for rejection in rejected_decision.rejections:
                    record_safely(
                        self.metrics,
                        "llm_route_candidate_rejected",
                        provider=rejection.target.provider,
                        model=rejection.target.model,
                        mode=request.mode,
                        language=request.language,
                        route=rejection.target.name,
                        locality="remote" if rejection.target.remote else "local",
                        model_tier=rejection.target.tier,
                        route_reason=rejected_decision.reason,
                        rejection_reason=rejection.reason,
                        outcome="rejected",
                        success=False,
                        complexity_score=rejected_decision.complexity_score,
                        **network_values,
                    )
            if privacy_error is not None:
                raise privacy_error from None
            raise ProviderError(
                category=ErrorCategory.PROVIDER_UNAVAILABLE,
                safe_message="No eligible language-model route is available",
                provider="router",
                model=request.model,
                transmitted=False,
            ) from None

        if network_preferred_local and decision.targets and not decision.targets[0].remote:
            decision = RoutingDecision(
                targets=decision.targets,
                complexity_score=decision.complexity_score,
                selected_tier=decision.selected_tier,
                reason="network.unknown.prefer_local",
                rejections=decision.rejections,
                network_forced_local=True,
            )

        by_name = self._execution_by_name[request.mode]
        planned = tuple(by_name[route.name] for route in decision.targets)
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
        return planned, decision, network_snapshot, selected_connectivity

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
        pipeline_started_at: float | None = None,
        before_first_speech: Callable[[], Any] | None = None,
    ) -> str:
        if not message or not message.strip():
            raise ValueError("message cannot be empty")
        if mode not in self.models:
            raise ValueError(f"Unknown model mode: {mode!r}")

        active_cancellation = cancellation or CancellationController()
        selected_privacy = PrivacyLevel(privacy or self.llm_settings.privacy.default)
        with self._conversation_request_lock:
            previous_session_id = self.conversation.session_id
            turn = self.conversation.begin_turn(
                message,
                privacy=selected_privacy,
                redacted=message_redacted,
            )
            history = self.conversation.history_before(turn)
            session_id = self.conversation.session_id
            if session_id != previous_session_id:
                self._forget_provider_conversations(
                    previous_session_id,
                    reason="idle_timeout",
                )
            try:
                self.conversation.mark_streaming(turn)
                response = self._stream_turn(
                    mode=mode,
                    message=turn.user.content,
                    history=history,
                    conversation_id=session_id,
                    conversation_turn=turn.number,
                    context=context,
                    speak=speak,
                    privacy=privacy,
                    context_origin=context_origin,
                    message_redacted=message_redacted,
                    context_redacted=context_redacted,
                    connectivity=connectivity,
                    request_options=request_options,
                    cancellation=active_cancellation,
                    pipeline_started_at=pipeline_started_at,
                    before_first_speech=before_first_speech,
                )
                source_origins: set[ContentOrigin] = {turn.user.origin}
                assistant_remote_eligible = turn.user.remote_eligible
                for historical in history:
                    source_origins.add(historical.origin)
                    source_origins.update(historical.source_origins)
                    assistant_remote_eligible = (
                        assistant_remote_eligible and historical.remote_eligible
                    )
                if context:
                    try:
                        source_origins.add(ContentOrigin(context_origin))
                    except (TypeError, ValueError):
                        source_origins.add(ContentOrigin.UNKNOWN)
                if source_origins & {
                    ContentOrigin.LOCAL_DOCUMENT,
                    ContentOrigin.LOCAL_DOCUMENT_DERIVATIVE,
                }:
                    source_origins.add(ContentOrigin.LOCAL_DOCUMENT_DERIVATIVE)
                if selected_privacy is PrivacyLevel.REMOTE_REDACTED:
                    # No response redactor attested this model output.
                    assistant_remote_eligible = False

                def commit_response() -> None:
                    self.conversation.complete_turn(
                        turn,
                        response,
                        # Model output has not passed through an attested redactor,
                        # even when its input was classified remote_redacted.
                        redacted=False,
                        remote_eligible=assistant_remote_eligible,
                        source_origins=frozenset(source_origins),
                    )

                commit_if_active = getattr(
                    active_cancellation,
                    "commit_if_not_cancelled",
                    None,
                )
                if callable(commit_if_active):
                    commit_if_active(commit_response)
                else:
                    active_cancellation.raise_if_cancelled()
                    commit_response()
                return response
            except BaseException:
                self.conversation.fail_turn(
                    turn,
                    interrupted=bool(active_cancellation.cancelled),
                )
                self._invalidate_provider_conversations(
                    session_id,
                    turn.number,
                    reason=(
                        "logical_turn_cancelled"
                        if active_cancellation.cancelled
                        else "logical_turn_failed"
                    ),
                )
                raise

    def _stream_turn(
        self,
        *,
        mode: str,
        message: str,
        history: tuple[ChatMessage, ...],
        conversation_id: str,
        conversation_turn: int,
        context: str | None,
        speak: bool,
        privacy: PrivacyLevel | str | None = None,
        context_origin: ContentOrigin = ContentOrigin.UNKNOWN,
        message_redacted: bool = False,
        context_redacted: bool = False,
        connectivity: Connectivity | str | None = None,
        request_options: Mapping[str, Any] | None = None,
        cancellation: CancellationToken | None = None,
        pipeline_started_at: float | None = None,
        before_first_speech: Callable[[], Any] | None = None,
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

        observed_start = time.monotonic()
        request_started_at = (
            float(pipeline_started_at)
            if isinstance(pipeline_started_at, (int, float))
            and not isinstance(pipeline_started_at, bool)
            and 0 <= pipeline_started_at <= observed_start
            else observed_start
        )
        decision: RoutingDecision | None = None
        network_values: dict[str, Any] = {
            "network_state": None,
            "network_quality_score": None,
            "network_quality_tier": "unknown",
            "network_reason": None,
            "network_forced_local": None,
        }
        routing_ms: float | None = None
        terminal_recorded = False

        def record_request_failure(
            category: ErrorCategory,
            *,
            provider: str | None = None,
            model: str | None = None,
            speech_committed: bool = False,
            attempts: int = 0,
            context: StreamingFailureContext | None = None,
        ) -> None:
            nonlocal terminal_recorded
            if terminal_recorded:
                return
            terminal_recorded = True
            selected = context.target if context is not None else None
            if selected is None and decision is not None:
                selected = next(
                    (
                        target
                        for target in decision.targets
                        if target.provider == provider and (model is None or target.model == model)
                    ),
                    decision.targets[0] if decision.targets else None,
                )
            observed_attempts = context.attempts if context is not None else attempts
            retry_count = (
                context.retry_count if context is not None else max(0, observed_attempts - 1)
            )
            record_safely(
                self.metrics,
                "llm_request_failed",
                provider=provider or (selected.provider if selected is not None else "router"),
                model=model or (selected.model if selected is not None else self.models[mode]),
                mode=mode,
                language=self.language,
                route=selected.name if selected is not None else None,
                locality=(
                    "remote"
                    if selected is not None and selected.remote
                    else "local"
                    if selected is not None
                    else None
                ),
                model_tier=selected.tier if selected is not None else None,
                route_reason=decision.reason if decision is not None else None,
                outcome="failed",
                success=False,
                error_category=category,
                timeout_category=(category.value if "timeout" in category.value else None),
                routing_ms=routing_ms,
                end_to_end_ms=(time.monotonic() - request_started_at) * 1_000,
                retry_count=retry_count,
                fallback_count=context.fallback_count if context is not None else 0,
                fallback_from=context.fallback_from if context is not None else None,
                fallback_to=context.fallback_to if context is not None else None,
                fallback_cause=context.fallback_cause if context is not None else None,
                cancellation_count=1 if category is ErrorCategory.CANCELLED else 0,
                interruption_count=1 if speech_committed else 0,
                speech_committed=(
                    context.speech_committed if context is not None else speech_committed
                ),
                streaming=True,
                **network_values,
            )

        try:
            request = self._request(
                mode=mode,
                message=message,
                history=history,
                conversation_id=conversation_id,
                conversation_turn=conversation_turn,
                context=context,
                context_origin=context_origin,
                message_redacted=message_redacted,
                context_redacted=context_redacted,
                privacy=privacy,
                request_options=request_options,
            )
            logger.info(
                "conversation_session=%s turn=%s event=llm_request_built "
                "history_messages=%s request_messages=%s",
                safe_conversation_identifier(conversation_id),
                conversation_turn,
                len(history),
                len(request.messages),
            )
            routing_started_at = time.monotonic()
            executions, decision, snapshot, selected_connectivity = self._plan_detailed(
                request,
                connectivity=connectivity,
            )
            routing_ms = (time.monotonic() - routing_started_at) * 1_000
            quality_score = getattr(snapshot, "quality_score", None)
            network_values = {
                "network_state": selected_connectivity.value,
                "network_quality_score": quality_score,
                "network_quality_tier": self._network_quality_tier(
                    quality_score,
                    selected_connectivity.value,
                ),
                "network_reason": getattr(snapshot, "reason", None),
                "network_forced_local": decision.network_forced_local,
            }
            selected_route = executions[0].route
            logger.info(
                "conversation_session=%s turn=%s event=llm_route_selected provider=%s route=%s",
                safe_conversation_identifier(conversation_id),
                conversation_turn,
                selected_route.provider,
                selected_route.name,
            )
            record_safely(
                self.metrics,
                "llm_route_decided",
                provider=selected_route.provider,
                model=selected_route.model,
                mode=mode,
                language=self.language,
                route=selected_route.name,
                locality="remote" if selected_route.remote else "local",
                model_tier=selected_route.tier or decision.selected_tier,
                route_reason=decision.reason,
                outcome="selected",
                success=True,
                complexity_score=decision.complexity_score,
                routing_ms=routing_ms,
                estimated_input_tokens=RoutePlanner.estimate_input_tokens(request),
                **network_values,
            )
            for rejection in decision.rejections:
                record_safely(
                    self.metrics,
                    "llm_route_candidate_rejected",
                    provider=rejection.target.provider,
                    model=rejection.target.model,
                    mode=mode,
                    language=self.language,
                    route=rejection.target.name,
                    locality="remote" if rejection.target.remote else "local",
                    model_tier=rejection.target.tier,
                    route_reason=decision.reason,
                    rejection_reason=rejection.reason,
                    outcome="rejected",
                    success=False,
                    complexity_score=decision.complexity_score,
                    **network_values,
                )
            logger.info(
                "Planning %s request with eligible routes in fallback order: %s",
                mode,
                ",".join(execution.route.name for execution in executions),
            )
            result = self._coordinator.run(
                request,
                executions,
                speak=self._speech_callable() if speak else None,
                before_first_speech=before_first_speech if speak else None,
                first_speech_min_chars=(self._mode_settings(mode).first_speech_min_chars),
                speech_chunk_max_chars=(self._mode_settings(mode).speech_chunk_max_chars),
                maximum_first_audio_seconds=(
                    self.llm_settings.health.maximum_talk_first_audio_ms / 1_000
                    if mode == "talk" and speak
                    else None
                ),
                cancellation=active_cancellation,
                route_reason=decision.reason,
                complexity_score=decision.complexity_score,
                **network_values,
            )
            # Close the narrow race where cancellation can arrive after the
            # coordinator consumes its terminal event but before the logical
            # conversation commits the assistant turn. A superseded response
            # must never become canonical history.
            if active_cancellation.cancelled:
                raise ProviderError(
                    ErrorCategory.CANCELLED,
                    "Language-model request was cancelled",
                    provider=result.target.provider,
                    model=result.target.model,
                    retryable_same_provider=False,
                    transmitted=True,
                )
            request_latency_ms = (time.monotonic() - request_started_at) * 1_000
            attempt_latency_ms = (
                result.attempt_latency_seconds * 1_000
                if result.attempt_latency_seconds is not None
                else None
            )

            def request_relative(value: float | None) -> float | None:
                if value is None or attempt_latency_ms is None:
                    return None
                return max(0.0, request_latency_ms - attempt_latency_ms + value * 1_000)

            usage = result.metadata.usage
            record_safely(
                self.metrics,
                "llm_request_succeeded",
                provider=result.target.provider,
                model=result.target.model,
                resolved_model=result.metadata.resolved_model,
                mode=mode,
                language=self.language,
                route=result.target.name,
                locality="remote" if result.target.remote else "local",
                model_tier=result.target.tier or decision.selected_tier,
                route_reason=decision.reason,
                outcome="succeeded",
                success=True,
                routing_ms=routing_ms,
                inference_ms=(
                    max(
                        0.0,
                        attempt_latency_ms
                        - result.tts_synthesis_seconds * 1_000
                        - result.audio_playback_seconds * 1_000,
                    )
                    if attempt_latency_ms is not None
                    else None
                ),
                first_token_ms=request_relative(result.first_token_seconds),
                first_audio_ms=request_relative(result.first_audio_seconds),
                speech_dispatch_ms=request_relative(result.first_audio_seconds),
                actual_first_audio_ms=request_relative(result.actual_first_audio_seconds),
                tts_synthesis_ms=result.tts_synthesis_seconds * 1_000 or None,
                audio_playback_ms=result.audio_playback_seconds * 1_000 or None,
                audio_duration_ms=result.audio_duration_seconds * 1_000 or None,
                end_to_end_ms=request_latency_ms,
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                total_tokens=usage.total_tokens,
                estimated_input_tokens=RoutePlanner.estimate_input_tokens(request),
                retry_count=result.retry_count,
                fallback_count=result.fallback_count,
                fallback_from=(
                    executions[result.fallback_count - 1].route.name
                    if result.fallback_count > 0
                    else None
                ),
                fallback_to=result.target.name if result.fallback_count > 0 else None,
                fallback_cause=result.fallback_cause,
                speech_committed=result.first_audio_seconds is not None,
                streaming=True,
                complexity_score=decision.complexity_score,
                **network_values,
            )
            terminal_recorded = True
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
        except SpeechReplayUnsafeError as wrapped:
            failure_context = getattr(wrapped.error, "streaming_context", None)
            record_request_failure(
                wrapped.error.category,
                provider=wrapped.error.provider,
                model=wrapped.error.model,
                speech_committed=True,
                attempts=wrapped.error.attempts,
                context=(
                    failure_context
                    if isinstance(failure_context, StreamingFailureContext)
                    else None
                ),
            )
            raise APIClientError(
                "Model stream was interrupted after speech output began; "
                "the request was not retried"
            ) from None
        except ProviderError as error:
            failure_context = getattr(error, "streaming_context", None)
            record_request_failure(
                error.category,
                provider=error.provider,
                model=error.model,
                attempts=error.attempts,
                context=(
                    failure_context
                    if isinstance(failure_context, StreamingFailureContext)
                    else None
                ),
            )
            attempts = error.attempts
            if attempts > 1:
                raise APIClientError(
                    f"Unable to stream a model response after {attempts} attempt(s)"
                ) from None
            raise APIClientError(
                f"Unable to stream a model response ({error.category.value})"
            ) from None
        except Exception as error:
            streaming_error = getattr(error, "streaming_error", None)
            failure_context = getattr(error, "streaming_context", None)
            if isinstance(streaming_error, ProviderError) and isinstance(
                failure_context, StreamingFailureContext
            ):
                record_request_failure(
                    streaming_error.category,
                    provider=streaming_error.provider,
                    model=streaming_error.model,
                    speech_committed=True,
                    attempts=streaming_error.attempts,
                    context=failure_context,
                )
            else:
                record_request_failure(ErrorCategory.UNKNOWN)
            raise
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
        pipeline_started_at: float | None = None,
        before_first_speech: Callable[[], Any] | None = None,
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
            pipeline_started_at=pipeline_started_at,
            before_first_speech=before_first_speech,
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
        pipeline_started_at: float | None = None,
        before_first_speech: Callable[[], Any] | None = None,
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
            pipeline_started_at=pipeline_started_at,
            before_first_speech=before_first_speech,
        )

    def reset_conversation(self, *, reason: str = "explicit") -> str:
        """End the current logical conversation and return the new session ID."""

        with self._conversation_request_lock:
            previous_id = self.conversation.session_id
            self._forget_provider_conversations(previous_id, reason=reason)
            return self.conversation.reset(reason=reason)

    def _forget_provider_conversations(self, conversation_id: str, *, reason: str) -> None:
        for provider in self._registry.instantiated():
            forget = getattr(provider, "forget_conversation", None)
            if not callable(forget):
                continue
            try:
                forget(conversation_id, reason=reason)
            except Exception:
                logger.warning(
                    "Unable to forget provider conversation (provider=%s, conversation_session=%s)",
                    provider.identity.name,
                    safe_conversation_identifier(conversation_id),
                    exc_info=True,
                )

    def _invalidate_provider_conversations(
        self,
        conversation_id: str,
        conversation_turn: int,
        *,
        reason: str,
    ) -> None:
        """Tell a contextful provider that its terminal turn was not committed."""

        for provider in self._registry.instantiated():
            provider_name = provider.identity.name
            invalidate = getattr(provider, "invalidate_conversation", None)
            if not callable(invalidate):
                continue
            try:
                invalidate(
                    conversation_id,
                    conversation_turn=conversation_turn,
                    reason=reason,
                )
            except Exception:
                logger.warning(
                    "Unable to invalidate provider conversation checkpoint "
                    "(provider=%s, conversation_session=%s, turn=%s)",
                    provider_name,
                    safe_conversation_identifier(conversation_id),
                    conversation_turn,
                    exc_info=True,
                )

    def cancel_current(self) -> None:
        """Request cancellation of all model streams currently owned by this client."""

        with self._cancellation_lock:
            active = tuple(self._active_cancellations)
        for token in active:
            cancel = getattr(token, "cancel", None)
            if callable(cancel):
                cancel()
        # Cancelling the model stream is not enough on its own: audio already
        # dispatched to the speech pipeline would otherwise keep playing after
        # a barge-in.
        with self._speech_pipeline_lock:
            pipeline = self._speech_pipeline
        if pipeline is not None:
            pipeline.cancel()

    def close(self) -> None:
        with self._cancellation_lock:
            if self._closed:
                return
            self._closed = True
        self.cancel_current()
        with self._speech_pipeline_lock:
            pipeline = self._speech_pipeline
            self._speech_pipeline = None
        if pipeline is not None:
            try:
                pipeline.close()
            except Exception:
                logger.warning("Unable to close the speech pipeline")
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
        if self._kpi_service is not None:
            try:
                self._kpi_service.close()
            except Exception:
                logger.warning("Unable to close the KPI subsystem")
        elif self._owns_metrics:
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
