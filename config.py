"""Application configuration with validated, project-rooted paths.

The module-level constants are retained for compatibility with the original
application.  New code should prefer :data:`SETTINGS`, which keeps related
values together and validates the selected language.
"""

from __future__ import annotations

import logging
import math
import os
import re
import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parent
# Suggested hybrid profile, referenced by documentation and tooling. It is NOT
# loaded implicitly: remote routing requires HELIOS_LLM_CONFIG to name a file.
SUGGESTED_LLM_CONFIG = Path("examples/llm-routing.codex-subscription.toml")
# Deprecated alias retained for callers that imported the old name.
DEFAULT_LLM_CONFIG = SUGGESTED_LLM_CONFIG
logger = logging.getLogger(__name__)


class ConfigurationError(ValueError):
    """Raised when application settings are internally inconsistent."""


_ROUTING_POLICIES = {"local_only", "remote_only", "local_first", "remote_first", "auto"}
_PRIVACY_LEVELS = {"local_only", "remote_allowed", "remote_redacted"}
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_LOG_LEVELS = {
    "NOTSET": logging.NOTSET,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _is_loopback_host(host: str) -> bool:
    """Return whether a dashboard bind host is restricted to this machine."""

    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class KPISettings:
    """Optional content-free KPI storage and dashboard configuration."""

    enabled: bool = False
    storage_path: Path = Path("logs/helios-kpi.sqlite3")
    queue_size: int = 2_048
    batch_size: int = 64
    flush_interval_seconds: float = 0.5
    raw_retention_days: int = 14
    rollup_retention_days: int = 90
    maximum_database_mb: int = 256
    rollup_interval_seconds: int = 300
    resource_sample_interval_seconds: float = 5.0
    dashboard_enabled: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8_765
    dashboard_allow_lan: bool = False
    dashboard_auth_token_env: str | None = None
    export_enabled: bool = True
    maximum_export_rows: int = 10_000
    maximum_query_days: int = 31
    maximum_query_points: int = 1_000

    def __post_init__(self) -> None:
        boolean_values = (
            self.enabled,
            self.dashboard_enabled,
            self.dashboard_allow_lan,
            self.export_enabled,
        )
        if any(not isinstance(value, bool) for value in boolean_values):
            raise ConfigurationError("KPI switches must be booleans")
        if not isinstance(self.storage_path, Path):
            object.__setattr__(self, "storage_path", Path(self.storage_path))

        integer_values = (
            ("queue_size", self.queue_size),
            ("batch_size", self.batch_size),
            ("raw_retention_days", self.raw_retention_days),
            ("rollup_retention_days", self.rollup_retention_days),
            ("maximum_database_mb", self.maximum_database_mb),
            ("rollup_interval_seconds", self.rollup_interval_seconds),
            ("maximum_export_rows", self.maximum_export_rows),
            ("maximum_query_days", self.maximum_query_days),
            ("maximum_query_points", self.maximum_query_points),
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for _name, value in integer_values
        ):
            raise ConfigurationError("KPI integer limits must be positive integers")
        if self.batch_size > self.queue_size:
            raise ConfigurationError("KPI batch_size cannot exceed queue_size")
        if self.rollup_retention_days < self.raw_retention_days:
            raise ConfigurationError(
                "KPI rollup retention cannot be shorter than raw-event retention"
            )
        if self.maximum_export_rows > 100_000:
            raise ConfigurationError("KPI maximum_export_rows cannot exceed 100000")
        if self.maximum_query_days > 90:
            raise ConfigurationError("KPI maximum_query_days cannot exceed 90")
        if self.maximum_query_points > 1_000:
            raise ConfigurationError("KPI maximum_query_points cannot exceed 1000")
        if (
            isinstance(self.flush_interval_seconds, bool)
            or not isinstance(self.flush_interval_seconds, (int, float))
            or not math.isfinite(float(self.flush_interval_seconds))
            or self.flush_interval_seconds <= 0
        ):
            raise ConfigurationError("KPI flush interval must be finite and positive")
        if (
            isinstance(self.resource_sample_interval_seconds, bool)
            or not isinstance(self.resource_sample_interval_seconds, (int, float))
            or not math.isfinite(float(self.resource_sample_interval_seconds))
            or self.resource_sample_interval_seconds <= 0
        ):
            raise ConfigurationError("KPI resource interval must be finite and positive")
        if (
            isinstance(self.dashboard_port, bool)
            or not isinstance(self.dashboard_port, int)
            or not 1 <= self.dashboard_port <= 65_535
        ):
            raise ConfigurationError("KPI dashboard port must be between 1 and 65535")
        host = self.dashboard_host.strip()
        if not host or len(host) > 253 or any(character.isspace() for character in host):
            raise ConfigurationError("KPI dashboard host is invalid")
        object.__setattr__(self, "dashboard_host", host)
        token_env = self.dashboard_auth_token_env
        if token_env is not None:
            token_env = token_env.strip()
            if not _ENV_NAME.fullmatch(token_env):
                raise ConfigurationError("KPI dashboard auth-token environment name is invalid")
            object.__setattr__(self, "dashboard_auth_token_env", token_env)
        if self.dashboard_enabled and not _is_loopback_host(host):
            if not self.dashboard_allow_lan:
                raise ConfigurationError(
                    "non-loopback KPI dashboard binding requires dashboard_allow_lan"
                )
            if token_env is None:
                raise ConfigurationError(
                    "non-loopback KPI dashboard binding requires authentication"
                )


@dataclass(frozen=True)
class LLMTimeoutSettings:
    connect_seconds: float = 2.0
    # Loading a local model after boot can take more than ten seconds on a
    # memory-constrained Jetson.  Four seconds caused a request to be retried
    # while Ollama was still loading the first one.
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
            raise ConfigurationError("LLM timeout values must be greater than zero")
        if self.total_seconds < self.connect_seconds:
            raise ConfigurationError("LLM total timeout cannot be shorter than connect timeout")


@dataclass(frozen=True)
class LLMPrivacySettings:
    default: str = "local_only"
    # Every field fails closed. A routing file that opts into remote inference
    # can re-enable context sharing explicitly; the in-code default must not,
    # or enabling remote routing via the environment alone would silently start
    # forwarding prior conversation turns.
    allow_remote_transcripts: bool = False
    allow_remote_context: bool = False
    allow_remote_rag_context: bool = False
    redaction_failure: str = "local_only"

    def __post_init__(self) -> None:
        if self.default not in _PRIVACY_LEVELS:
            raise ConfigurationError(f"Unsupported LLM privacy level: {self.default!r}")
        if self.redaction_failure != "local_only":
            raise ConfigurationError("redaction failure policy must be 'local_only'")


@dataclass(frozen=True)
class LLMHealthSettings:
    failures_to_open: int = 3
    cooldown_seconds: float = 60.0
    maximum_cooldown_seconds: float = 900.0
    maximum_talk_first_audio_ms: int = 1_500

    def __post_init__(self) -> None:
        if self.failures_to_open < 1:
            raise ConfigurationError("failures_to_open must be at least one")
        if self.cooldown_seconds <= 0 or self.maximum_cooldown_seconds <= 0:
            raise ConfigurationError("health cooldowns must be greater than zero")
        if self.maximum_cooldown_seconds < self.cooldown_seconds:
            raise ConfigurationError("maximum cooldown cannot be shorter than base cooldown")
        if self.maximum_talk_first_audio_ms < 1:
            raise ConfigurationError("maximum talk first-audio latency must be positive")


@dataclass(frozen=True)
class LLMBudgetSettings:
    enabled: bool = True
    catalog_path: Path | None = None
    ledger_path: Path | None = None
    per_request_usd: Decimal = Decimal("0")
    daily_usd: Decimal = Decimal("0")
    monthly_usd: Decimal = Decimal("0")
    zero_cost_only: bool = True
    missing_usage: str = "settle_reserved"

    def __post_init__(self) -> None:
        for name, value in (
            ("per_request_usd", self.per_request_usd),
            ("daily_usd", self.daily_usd),
            ("monthly_usd", self.monthly_usd),
        ):
            if value < 0:
                raise ConfigurationError(f"{name} cannot be negative")
        if self.missing_usage != "settle_reserved":
            raise ConfigurationError("missing usage policy must be 'settle_reserved'")


@dataclass(frozen=True)
class LLMObservabilitySettings:
    metrics_enabled: bool = True
    metrics_path: Path | None = None
    log_content: bool = False
    log_headers: bool = False
    metrics_retention_days: int = 14

    def __post_init__(self) -> None:
        if self.metrics_retention_days < 1:
            raise ConfigurationError("metrics_retention_days must be at least one")


@dataclass(frozen=True)
class LLMNetworkSettings:
    enabled: bool = False
    probe_url: str = "https://chatgpt.com/"
    probe_interval_seconds: float = 3.0
    result_max_age_seconds: float = 6.0
    probe_timeout_seconds: float = 1.2
    probe_bytes: int = 32_768
    goodput_probe_interval_seconds: float = 60.0
    minimum_quality_score: float = 0.50
    quality_hysteresis: float = 0.05
    target_ttfb_ms: float = 1_200.0
    target_jitter_ms: float = 300.0
    minimum_goodput_kbps: float = 128.0
    history_size: int = 8
    require_wifi: bool = False
    interface_allowlist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(self.require_wifi, bool):
            raise ConfigurationError("network enabled and require_wifi must be booleans")
        parsed = urlsplit(self.probe_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ConfigurationError(
                "network probe_url must be an HTTPS URL without credentials or fragment"
            )
        positive_values = (
            ("probe_interval_seconds", self.probe_interval_seconds),
            ("result_max_age_seconds", self.result_max_age_seconds),
            ("probe_timeout_seconds", self.probe_timeout_seconds),
            (
                "goodput_probe_interval_seconds",
                self.goodput_probe_interval_seconds,
            ),
            ("target_ttfb_ms", self.target_ttfb_ms),
            ("target_jitter_ms", self.target_jitter_ms),
            ("minimum_goodput_kbps", self.minimum_goodput_kbps),
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
            for _name, value in positive_values
        ):
            raise ConfigurationError("network timing and quality targets must be positive")
        if self.result_max_age_seconds < self.probe_interval_seconds:
            raise ConfigurationError(
                "network result_max_age_seconds cannot be shorter than probe interval"
            )
        if self.goodput_probe_interval_seconds < self.probe_interval_seconds:
            raise ConfigurationError(
                "network goodput probe interval cannot be shorter than probe interval"
            )
        if (
            isinstance(self.probe_bytes, bool)
            or not isinstance(self.probe_bytes, int)
            or not 1_024 <= self.probe_bytes <= 1_048_576
        ):
            raise ConfigurationError("network probe_bytes must be between 1024 and 1048576")
        if (
            isinstance(self.minimum_quality_score, bool)
            or not isinstance(self.minimum_quality_score, (int, float))
            or not 0 <= self.minimum_quality_score <= 1
        ):
            raise ConfigurationError("network minimum_quality_score must be between zero and one")
        if (
            isinstance(self.quality_hysteresis, bool)
            or not isinstance(self.quality_hysteresis, (int, float))
            or not 0 <= self.quality_hysteresis <= 0.25
        ):
            raise ConfigurationError("network quality_hysteresis must be between zero and 0.25")
        if (
            self.minimum_quality_score - self.quality_hysteresis < 0
            or self.minimum_quality_score + self.quality_hysteresis > 1
        ):
            raise ConfigurationError("network quality hysteresis crosses the zero-to-one range")
        if (
            isinstance(self.history_size, bool)
            or not isinstance(self.history_size, int)
            or not 2 <= self.history_size <= 64
        ):
            raise ConfigurationError("network history_size must be between 2 and 64")
        if len(set(self.interface_allowlist)) != len(self.interface_allowlist):
            raise ConfigurationError("network interface_allowlist entries must be unique")
        if any(
            not interface or len(interface) > 64 or re.search(r"[^A-Za-z0-9_.:-]", interface)
            for interface in self.interface_allowlist
        ):
            raise ConfigurationError("network interface_allowlist contains an invalid name")


@dataclass(frozen=True)
class LLMModeSettings:
    candidates: tuple[str, ...] = ()
    max_output_tokens: int | None = None
    complexity_threshold: int = 2
    first_speech_min_chars: int = 0
    speech_chunk_max_chars: int = 0
    first_visible_token_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ConfigurationError("mode max_output_tokens must be at least one")
        if self.complexity_threshold < 0:
            raise ConfigurationError("complexity_threshold cannot be negative")
        if self.first_speech_min_chars < 0:
            raise ConfigurationError("first_speech_min_chars cannot be negative")
        if self.speech_chunk_max_chars < 0:
            raise ConfigurationError("speech_chunk_max_chars cannot be negative")
        if self.first_visible_token_seconds is not None and (
            isinstance(self.first_visible_token_seconds, bool)
            or not isinstance(self.first_visible_token_seconds, (int, float))
            or not math.isfinite(float(self.first_visible_token_seconds))
            or self.first_visible_token_seconds <= 0
        ):
            raise ConfigurationError("first_visible_token_seconds must be positive")
        if len(set(self.candidates)) != len(self.candidates):
            raise ConfigurationError("mode candidate names must be unique")


@dataclass(frozen=True)
class LLMProviderSettings:
    name: str
    adapter: str
    endpoint: str
    locality: str
    api_key_env: str | None = None
    enabled: bool = True
    internal_retries: int = 0

    def __post_init__(self) -> None:
        if not self.name or not self.adapter or not self.endpoint:
            raise ConfigurationError("provider name, adapter, and endpoint are required")
        if self.adapter not in {"ollama", "openai_chat_sse", "codex_app_server"}:
            raise ConfigurationError(f"Unsupported provider adapter: {self.adapter!r}")
        if self.adapter == "ollama" and self.name != "ollama":
            raise ConfigurationError("the Ollama adapter must use provider name 'ollama'")
        if self.name == "ollama" and self.adapter != "ollama":
            raise ConfigurationError("provider name 'ollama' is reserved for the Ollama adapter")
        if self.adapter in {"openai_chat_sse", "codex_app_server"} and self.locality != "remote":
            raise ConfigurationError("OpenAI and Codex providers must be remote")
        if self.locality not in {"device", "trusted_lan", "remote"}:
            raise ConfigurationError(f"Unsupported provider locality: {self.locality!r}")
        if self.api_key_env is not None and not _ENV_NAME.fullmatch(self.api_key_env):
            raise ConfigurationError(f"Invalid API-key environment name: {self.api_key_env!r}")
        if self.adapter == "ollama" and self.api_key_env is not None:
            raise ConfigurationError("Ollama providers do not accept an API-key setting")
        if self.adapter == "codex_app_server" and self.api_key_env is not None:
            raise ConfigurationError(
                "Codex app-server uses the local ChatGPT sign-in, not an API-key setting"
            )
        if (
            self.locality == "remote"
            and self.adapter == "openai_chat_sse"
            and self.api_key_env is None
        ):
            raise ConfigurationError("remote providers require an API-key environment name")
        if self.internal_retries != 0:
            raise ConfigurationError("provider-internal retries must be disabled")

        parsed = urlsplit(self.endpoint)
        if not parsed.hostname:
            raise ConfigurationError(f"Invalid provider endpoint: {self.endpoint!r}")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigurationError(
                "provider endpoints cannot contain credentials, query, or fragment"
            )
        if self.adapter == "codex_app_server":
            if self.endpoint != "stdio://codex":
                raise ConfigurationError(
                    "Codex app-server endpoint must be exactly 'stdio://codex'"
                )
            return
        if (
            self.adapter == "ollama"
            and self.locality == "device"
            and (parsed.hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ConfigurationError("device-local Ollama endpoints must use a loopback host")
        if self.locality == "remote" and parsed.scheme != "https":
            raise ConfigurationError("remote provider endpoints must use HTTPS")


@dataclass(frozen=True)
class LLMTargetSettings:
    name: str
    provider: str
    model: str | None = None
    model_by_language: tuple[tuple[str, str], ...] = ()
    catalog_id: str | None = None
    languages: tuple[str, ...] = ("it", "en")
    context_window: int | None = None
    max_output_tokens: int | None = None
    max_output_words: int | None = None
    min_complexity_score: int | None = None
    retry_attempts: int = 1
    options: tuple[tuple[str, Any], ...] = ()
    tier: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.provider:
            raise ConfigurationError("target name and provider are required")
        if self.tier is not None and (
            not self.tier or len(self.tier) > 64 or re.search(r"[^A-Za-z0-9_.:-]", self.tier)
        ):
            raise ConfigurationError("target tier must be a machine-readable label")
        if not self.model and not self.model_by_language:
            raise ConfigurationError(f"Target {self.name!r} has no model")
        if self.context_window is not None and self.context_window < 1:
            raise ConfigurationError("target context_window must be positive")
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ConfigurationError("target max_output_tokens must be positive")
        if self.max_output_words is not None and (
            isinstance(self.max_output_words, bool)
            or not isinstance(self.max_output_words, int)
            or self.max_output_words < 1
        ):
            raise ConfigurationError("target max_output_words must be a positive integer")
        if self.min_complexity_score is not None and (
            isinstance(self.min_complexity_score, bool)
            or not isinstance(self.min_complexity_score, int)
            or self.min_complexity_score < 0
        ):
            raise ConfigurationError("target min_complexity_score must be non-negative")
        if self.retry_attempts < 1:
            raise ConfigurationError("target retry_attempts must be at least one")
        if len({code for code, _model in self.model_by_language}) != len(self.model_by_language):
            raise ConfigurationError("model_by_language contains duplicate languages")

    def model_for_language(self, language: str) -> str:
        models = dict(self.model_by_language)
        selected = models.get(language, self.model)
        if not selected:
            raise ConfigurationError(
                f"Target {self.name!r} has no model configured for language {language!r}"
            )
        return selected


@dataclass(frozen=True)
class LLMSettings:
    routing_file: Path | None = None
    routing_policy: str = "local_only"
    remote_enabled: bool = False
    emergency_local_only: bool = False
    allowlist: tuple[str, ...] = ()
    denylist: tuple[str, ...] = ()
    unknown_connectivity: str = "prefer_local"
    privacy: LLMPrivacySettings = field(default_factory=LLMPrivacySettings)
    context_idle_timeout_seconds: float = 900.0
    context_max_turns: int = 20
    timeouts: LLMTimeoutSettings = field(default_factory=LLMTimeoutSettings)
    health: LLMHealthSettings = field(default_factory=LLMHealthSettings)
    budget: LLMBudgetSettings = field(default_factory=LLMBudgetSettings)
    observability: LLMObservabilitySettings = field(default_factory=LLMObservabilitySettings)
    network: LLMNetworkSettings = field(default_factory=LLMNetworkSettings)
    talk: LLMModeSettings = field(
        default_factory=lambda: LLMModeSettings(max_output_tokens=64, complexity_threshold=3)
    )
    think: LLMModeSettings = field(
        default_factory=lambda: LLMModeSettings(max_output_tokens=256, complexity_threshold=2)
    )
    providers: tuple[LLMProviderSettings, ...] = ()
    targets: tuple[LLMTargetSettings, ...] = ()

    def __post_init__(self) -> None:
        if self.routing_policy not in _ROUTING_POLICIES:
            raise ConfigurationError(f"Unsupported LLM routing policy: {self.routing_policy!r}")
        if self.unknown_connectivity not in {"prefer_local", "allow_remote"}:
            raise ConfigurationError(
                f"Unsupported unknown-connectivity policy: {self.unknown_connectivity!r}"
            )
        if (
            isinstance(self.context_idle_timeout_seconds, bool)
            or not isinstance(self.context_idle_timeout_seconds, (int, float))
            or not math.isfinite(float(self.context_idle_timeout_seconds))
            or self.context_idle_timeout_seconds <= 0
        ):
            raise ConfigurationError("LLM remote-context idle timeout must be positive and finite")
        if (
            isinstance(self.context_max_turns, bool)
            or not isinstance(self.context_max_turns, int)
            or self.context_max_turns < 1
        ):
            raise ConfigurationError("LLM remote-context turn cap must be a positive integer")
        if len(set(self.allowlist)) != len(self.allowlist):
            raise ConfigurationError("provider allowlist entries must be unique")
        if len(set(self.denylist)) != len(self.denylist):
            raise ConfigurationError("provider denylist entries must be unique")
        if len({provider.name for provider in self.providers}) != len(self.providers):
            raise ConfigurationError("provider names must be unique")
        if len({target.name for target in self.targets}) != len(self.targets):
            raise ConfigurationError("target names must be unique")

        providers = {provider.name for provider in self.providers}
        for target in self.targets:
            if target.provider not in providers and target.provider != "ollama":
                raise ConfigurationError(
                    f"Target {target.name!r} refers to unknown provider {target.provider!r}"
                )
        targets = {target.name for target in self.targets}
        known_route_names = providers | targets | {"ollama"}
        unknown_allowed = set(self.allowlist) - known_route_names
        unknown_denied = set(self.denylist) - known_route_names
        if unknown_allowed:
            raise ConfigurationError(
                "provider allowlist contains unknown entries: " + ", ".join(sorted(unknown_allowed))
            )
        if unknown_denied:
            raise ConfigurationError(
                "provider denylist contains unknown entries: " + ", ".join(sorted(unknown_denied))
            )
        for mode_name, mode in (("talk", self.talk), ("think", self.think)):
            missing = [candidate for candidate in mode.candidates if candidate not in targets]
            if missing:
                raise ConfigurationError(
                    f"{mode_name} route refers to unknown target(s): {', '.join(missing)}"
                )

        if self.emergency_local_only:
            object.__setattr__(self, "remote_enabled", False)
            object.__setattr__(self, "routing_policy", "local_only")


@dataclass(frozen=True)
class LanguageProfile:
    code: str
    vosk_model: Path
    tts_model: Path
    wake_word: str
    wake_word_aliases: tuple[str, ...]
    think_words: tuple[str, ...]
    rag_word: str
    presentation_questions: tuple[str, str, str]
    presentation_answers: tuple[str, str, str]
    talk_model: str
    welcome_message: str
    backchannel_phrases: tuple[str, ...]
    rag_result_prefix: str
    model_error_message: str


def _profile_paths(root: Path) -> Mapping[str, LanguageProfile]:
    return {
        "en": LanguageProfile(
            code="en",
            vosk_model=root / "recognizer/models/vosk-model-small-en-us-0.15",
            tts_model=root / "audio/models/en_GB-alba-medium.onnx",
            wake_word="emilia",
            wake_word_aliases=("emilia", "amelia", "hello"),
            think_words=("think", "reason"),
            rag_word="regulation",
            presentation_questions=(
                "what's your name",
                "who are you",
                "introduce yourself",
            ),
            presentation_answers=(
                "Hi, I'm Emilia five point nine, your solar-powered AI vehicle, "
                "ready to support the crew across the Australian desert!",
                "Greetings, this is Emilia five point nine, your intelligent solar "
                "companion. How can I assist you today on this desert mission?",
                "Hi, Emilia five point nine here. Good morning crew! How can I help "
                "you today in crossing the Australian desert?",
            ),
            talk_model="emilia-en-gemma3:1b",
            welcome_message=(
                "Hi! I just woke up and I'm ready to help. Just remember to call "
                "me '{wake_word}' when you talk to me."
            ),
            backchannel_phrases=("Sure.", "One moment.", "Let's see."),
            rag_result_prefix="Here's what I found: ",
            model_error_message="I could not contact the language model.",
        ),
        "it": LanguageProfile(
            code="it",
            vosk_model=root / "recognizer/models/vosk-model-small-it-0.22",
            tts_model=root / "audio/models/it_IT-paola-medium.onnx",
            wake_word="emilia",
            wake_word_aliases=("emilia", "amelia", "hello"),
            think_words=("pensa", "ragiona"),
            rag_word="regolamento",
            presentation_questions=("come ti chiami", "chi sei", "presentati a"),
            presentation_answers=(
                "Sono Emilia, un'auto solare dotata di intelligenza artificiale.",
                "Io sono Emilia, un'auto solare dotata di intelligenza artificiale.",
                "Piacere di conoscerti! Sono Emilia, un'auto solare dotata di "
                "intelligenza artificiale.",
            ),
            talk_model="emilia-gemma3:1b",
            welcome_message=(
                "Ciao! Mi sono appena svegliata e sono pronta ad aiutarti. "
                "Ricordati solo di chiamarmi '{wake_word}' quando mi parli."
            ),
            backchannel_phrases=("Certo.", "Un momento.", "Vediamo."),
            rag_result_prefix="Ecco cosa ho trovato: ",
            model_error_message="Non riesco a contattare il modello linguistico.",
        ),
    }


def normalize_ollama_host(value: str) -> str:
    """Return an Ollama SDK host from either a host or legacy endpoint URL."""

    candidate = value.strip()
    if not candidate:
        raise ConfigurationError("Ollama host cannot be empty")
    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parsed = urlsplit(candidate)
    if not parsed.hostname:
        raise ConfigurationError(f"Invalid Ollama host: {value!r}")

    path = parsed.path.rstrip("/")
    for endpoint in ("/api/generate", "/api/chat", "/api/embeddings", "/api/embed"):
        if path.endswith(endpoint):
            path = path[: -len(endpoint)]
            break

    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))


def _table(value: Any, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{name} must be a table")
    return value


def _reject_unknown(table: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigurationError(f"Unknown {name} setting(s): {', '.join(unknown)}")


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a decimal number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a decimal number") from exc
    if not parsed.is_finite():
        raise ConfigurationError(f"{name} must be finite")
    return parsed


def _toml_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be a TOML boolean")
    return value


def _toml_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be a TOML integer")
    return value


def _toml_float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a TOML number")
    try:
        result = float(value)
    except OverflowError:
        raise ConfigurationError(f"{name} must be finite") from None
    if not math.isfinite(result):
        raise ConfigurationError(f"{name} must be finite")
    return result


def _toml_string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ConfigurationError(f"{name} must be a TOML string")
    return value


def _bool_from_env(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean value")


def _int_from_env(value: str, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name} must be an integer") from None


def _float_from_env(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name} must be a number") from None
    if not math.isfinite(parsed):
        raise ConfigurationError(f"{name} must be finite")
    return parsed


def _audio_device_from_env(value: str | None, name: str) -> int | str | None:
    """Parse an optional PyAudio/sounddevice device selector.

    A numeric selector remains stable for fixed installations; a non-empty
    name is resolved by the recognizer or passed to sounddevice unchanged.
    Empty values deliberately mean "use the platform default".
    """

    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if re.fullmatch(r"[+-]?\d+", normalized):
        parsed = _int_from_env(normalized, name)
        if parsed < 0:
            raise ConfigurationError(f"{name} must be a non-negative device index")
        return parsed
    if len(normalized) > 256:
        raise ConfigurationError(f"{name} is not a valid device selector")
    return normalized


def _kpi_from_env(
    env: Mapping[str, str],
    *,
    project_root: Path,
) -> KPISettings:
    """Build optional KPI settings without reading the authentication secret."""

    defaults = KPISettings()
    storage_value = env.get("HELIOS_KPI_STORAGE_PATH", str(defaults.storage_path)).strip()
    if not storage_value:
        raise ConfigurationError("HELIOS_KPI_STORAGE_PATH cannot be empty")
    storage_path = Path(storage_value).expanduser()
    if not storage_path.is_absolute():
        storage_path = project_root / storage_path

    token_env = env.get("HELIOS_KPI_DASHBOARD_AUTH_TOKEN_ENV", "").strip() or None
    return KPISettings(
        enabled=_bool_from_env(
            env.get("HELIOS_KPI_ENABLED", str(defaults.enabled)),
            "HELIOS_KPI_ENABLED",
        ),
        storage_path=storage_path.resolve(),
        queue_size=_int_from_env(
            env.get("HELIOS_KPI_QUEUE_SIZE", str(defaults.queue_size)),
            "HELIOS_KPI_QUEUE_SIZE",
        ),
        batch_size=_int_from_env(
            env.get("HELIOS_KPI_BATCH_SIZE", str(defaults.batch_size)),
            "HELIOS_KPI_BATCH_SIZE",
        ),
        flush_interval_seconds=_float_from_env(
            env.get(
                "HELIOS_KPI_FLUSH_INTERVAL_SECONDS",
                str(defaults.flush_interval_seconds),
            ),
            "HELIOS_KPI_FLUSH_INTERVAL_SECONDS",
        ),
        raw_retention_days=_int_from_env(
            env.get("HELIOS_KPI_RAW_RETENTION_DAYS", str(defaults.raw_retention_days)),
            "HELIOS_KPI_RAW_RETENTION_DAYS",
        ),
        rollup_retention_days=_int_from_env(
            env.get(
                "HELIOS_KPI_ROLLUP_RETENTION_DAYS",
                str(defaults.rollup_retention_days),
            ),
            "HELIOS_KPI_ROLLUP_RETENTION_DAYS",
        ),
        maximum_database_mb=_int_from_env(
            env.get("HELIOS_KPI_MAX_DATABASE_MB", str(defaults.maximum_database_mb)),
            "HELIOS_KPI_MAX_DATABASE_MB",
        ),
        rollup_interval_seconds=_int_from_env(
            env.get(
                "HELIOS_KPI_ROLLUP_INTERVAL_SECONDS",
                str(defaults.rollup_interval_seconds),
            ),
            "HELIOS_KPI_ROLLUP_INTERVAL_SECONDS",
        ),
        resource_sample_interval_seconds=_float_from_env(
            env.get(
                "HELIOS_KPI_RESOURCE_INTERVAL_SECONDS",
                str(defaults.resource_sample_interval_seconds),
            ),
            "HELIOS_KPI_RESOURCE_INTERVAL_SECONDS",
        ),
        dashboard_enabled=_bool_from_env(
            env.get(
                "HELIOS_KPI_DASHBOARD_ENABLED",
                str(defaults.dashboard_enabled),
            ),
            "HELIOS_KPI_DASHBOARD_ENABLED",
        ),
        dashboard_host=env.get(
            "HELIOS_KPI_DASHBOARD_HOST",
            defaults.dashboard_host,
        ),
        dashboard_port=_int_from_env(
            env.get("HELIOS_KPI_DASHBOARD_PORT", str(defaults.dashboard_port)),
            "HELIOS_KPI_DASHBOARD_PORT",
        ),
        dashboard_allow_lan=_bool_from_env(
            env.get(
                "HELIOS_KPI_DASHBOARD_ALLOW_LAN",
                str(defaults.dashboard_allow_lan),
            ),
            "HELIOS_KPI_DASHBOARD_ALLOW_LAN",
        ),
        dashboard_auth_token_env=token_env,
        export_enabled=_bool_from_env(
            env.get("HELIOS_KPI_EXPORT_ENABLED", str(defaults.export_enabled)),
            "HELIOS_KPI_EXPORT_ENABLED",
        ),
        maximum_export_rows=_int_from_env(
            env.get("HELIOS_KPI_MAX_EXPORT_ROWS", str(defaults.maximum_export_rows)),
            "HELIOS_KPI_MAX_EXPORT_ROWS",
        ),
        maximum_query_days=_int_from_env(
            env.get("HELIOS_KPI_MAX_QUERY_DAYS", str(defaults.maximum_query_days)),
            "HELIOS_KPI_MAX_QUERY_DAYS",
        ),
        maximum_query_points=_int_from_env(
            env.get("HELIOS_KPI_MAX_QUERY_POINTS", str(defaults.maximum_query_points)),
            "HELIOS_KPI_MAX_QUERY_POINTS",
        ),
    )


def _log_level_from_env(value: str, name: str = "HELIOS_LOG_LEVEL") -> int:
    normalized = value.strip().upper()
    try:
        return _LOG_LEVELS[normalized]
    except KeyError:
        supported = ", ".join(_LOG_LEVELS)
        raise ConfigurationError(f"{name} must be one of: {supported}") from None


def _log_file_from_env(value: str | None, default: str | None = "app.log") -> str | None:
    if value is None:
        return default
    normalized = value.strip()
    return None if normalized in {"", "-"} else normalized


def _path_from_config(value: Any, *, base: Path, name: str) -> Path | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigurationError(f"{name} must be a path string")
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{name} must be an array of strings")
    result = tuple(item.strip() for item in value)
    if any(not item for item in result):
        raise ConfigurationError(f"{name} cannot contain empty values")
    return result


def _contains_secret_option(value: Any) -> bool:
    forbidden = {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "password",
        "secret",
        "token",
    }
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in forbidden or _contains_secret_option(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_option(item) for item in value)
    return False


def _load_toml(path: Path) -> Mapping[str, Any]:
    try:
        try:
            import tomllib
        except ImportError:  # pragma: no cover - exercised only on Python 3.10
            import tomli as tomllib  # type: ignore[no-redef]
    except ImportError as exc:  # pragma: no cover - depends on Python/deployment extras
        raise ConfigurationError(
            "Python 3.10 requires the 'tomli' package to load HELIOS_LLM_CONFIG"
        ) from exc

    try:
        with path.open("rb") as handle:
            parsed = tomllib.load(handle)
    except (OSError, ValueError):
        raise ConfigurationError(f"Unable to load LLM routing file '{path}'") from None
    if not isinstance(parsed, Mapping):
        raise ConfigurationError("LLM routing file must contain a TOML table")
    return parsed


def load_llm_settings(path: str | Path) -> LLMSettings:
    """Load and strictly validate non-secret hybrid LLM routing configuration."""

    routing_path = Path(path).expanduser().resolve()
    data = _load_toml(routing_path)
    _reject_unknown(
        data,
        {
            "schema_version",
            "router",
            "privacy",
            "timeouts",
            "health",
            "budget",
            "observability",
            "network",
            "modes",
            "providers",
            "targets",
        },
        "top-level",
    )
    schema_version = data.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise ConfigurationError("LLM routing schema_version must be 1")

    router = _table(data.get("router"), "router")
    _reject_unknown(
        router,
        {
            "policy",
            "remote_enabled",
            "allowlist",
            "denylist",
            "unknown_connectivity",
        },
        "router",
    )

    privacy_table = _table(data.get("privacy"), "privacy")
    _reject_unknown(
        privacy_table,
        {
            "default",
            "allow_remote_transcripts",
            "allow_remote_context",
            "allow_remote_rag_context",
            "redaction_failure",
        },
        "privacy",
    )
    privacy = LLMPrivacySettings(
        default=_toml_string(
            privacy_table.get("default", "local_only"),
            "privacy.default",
        ),
        allow_remote_transcripts=_toml_bool(
            privacy_table.get("allow_remote_transcripts", False),
            "privacy.allow_remote_transcripts",
        ),
        allow_remote_context=_toml_bool(
            privacy_table.get("allow_remote_context", True),
            "privacy.allow_remote_context",
        ),
        allow_remote_rag_context=_toml_bool(
            privacy_table.get("allow_remote_rag_context", False),
            "privacy.allow_remote_rag_context",
        ),
        redaction_failure=_toml_string(
            privacy_table.get("redaction_failure", "local_only"),
            "privacy.redaction_failure",
        ),
    )

    timeout_table = _table(data.get("timeouts"), "timeouts")
    _reject_unknown(
        timeout_table,
        {
            "connect_seconds",
            "first_token_seconds",
            "read_seconds",
            "total_seconds",
        },
        "timeouts",
    )
    timeouts = LLMTimeoutSettings(
        connect_seconds=_toml_float(
            timeout_table.get("connect_seconds", 2.0),
            "timeouts.connect_seconds",
        ),
        first_token_seconds=_toml_float(
            timeout_table.get("first_token_seconds", 20.0),
            "timeouts.first_token_seconds",
        ),
        read_seconds=_toml_float(
            timeout_table.get("read_seconds", 15.0),
            "timeouts.read_seconds",
        ),
        total_seconds=_toml_float(
            timeout_table.get("total_seconds", 45.0),
            "timeouts.total_seconds",
        ),
    )

    health_table = _table(data.get("health"), "health")
    _reject_unknown(
        health_table,
        {
            "failures_to_open",
            "cooldown_seconds",
            "maximum_cooldown_seconds",
            "maximum_talk_first_audio_ms",
        },
        "health",
    )
    health = LLMHealthSettings(
        failures_to_open=_toml_int(
            health_table.get("failures_to_open", 3),
            "health.failures_to_open",
        ),
        cooldown_seconds=_toml_float(
            health_table.get("cooldown_seconds", 60.0),
            "health.cooldown_seconds",
        ),
        maximum_cooldown_seconds=_toml_float(
            health_table.get("maximum_cooldown_seconds", 900.0),
            "health.maximum_cooldown_seconds",
        ),
        maximum_talk_first_audio_ms=_toml_int(
            health_table.get("maximum_talk_first_audio_ms", 1_500),
            "health.maximum_talk_first_audio_ms",
        ),
    )

    base = routing_path.parent
    budget_table = _table(data.get("budget"), "budget")
    _reject_unknown(
        budget_table,
        {
            "enabled",
            "catalog_path",
            "ledger_path",
            "per_request_usd",
            "daily_usd",
            "monthly_usd",
            "zero_cost_only",
            "missing_usage",
        },
        "budget",
    )
    budget = LLMBudgetSettings(
        enabled=_toml_bool(budget_table.get("enabled", True), "budget.enabled"),
        catalog_path=_path_from_config(
            budget_table.get("catalog_path"), base=base, name="budget.catalog_path"
        ),
        ledger_path=_path_from_config(
            budget_table.get("ledger_path"), base=base, name="budget.ledger_path"
        ),
        per_request_usd=_decimal(
            budget_table.get("per_request_usd", "0"), "budget.per_request_usd"
        ),
        daily_usd=_decimal(budget_table.get("daily_usd", "0"), "budget.daily_usd"),
        monthly_usd=_decimal(budget_table.get("monthly_usd", "0"), "budget.monthly_usd"),
        zero_cost_only=_toml_bool(
            budget_table.get("zero_cost_only", True),
            "budget.zero_cost_only",
        ),
        missing_usage=_toml_string(
            budget_table.get("missing_usage", "settle_reserved"),
            "budget.missing_usage",
        ),
    )

    observability_table = _table(data.get("observability"), "observability")
    _reject_unknown(
        observability_table,
        {
            "metrics_enabled",
            "metrics_path",
            "log_content",
            "log_headers",
            "metrics_retention_days",
        },
        "observability",
    )
    observability = LLMObservabilitySettings(
        metrics_enabled=_toml_bool(
            observability_table.get("metrics_enabled", True),
            "observability.metrics_enabled",
        ),
        metrics_path=_path_from_config(
            observability_table.get("metrics_path"),
            base=base,
            name="observability.metrics_path",
        ),
        log_content=_toml_bool(
            observability_table.get("log_content", False),
            "observability.log_content",
        ),
        log_headers=_toml_bool(
            observability_table.get("log_headers", False),
            "observability.log_headers",
        ),
        metrics_retention_days=_toml_int(
            observability_table.get("metrics_retention_days", 14),
            "observability.metrics_retention_days",
        ),
    )

    network_table = _table(data.get("network"), "network")
    _reject_unknown(
        network_table,
        {
            "enabled",
            "probe_url",
            "probe_interval_seconds",
            "result_max_age_seconds",
            "probe_timeout_seconds",
            "probe_bytes",
            "goodput_probe_interval_seconds",
            "minimum_quality_score",
            "quality_hysteresis",
            "target_ttfb_ms",
            "target_jitter_ms",
            "minimum_goodput_kbps",
            "history_size",
            "require_wifi",
            "interface_allowlist",
        },
        "network",
    )
    network = LLMNetworkSettings(
        enabled=_toml_bool(network_table.get("enabled", False), "network.enabled"),
        probe_url=_toml_string(
            network_table.get("probe_url", "https://chatgpt.com/"),
            "network.probe_url",
        ),
        probe_interval_seconds=_toml_float(
            network_table.get("probe_interval_seconds", 3.0),
            "network.probe_interval_seconds",
        ),
        result_max_age_seconds=_toml_float(
            network_table.get("result_max_age_seconds", 6.0),
            "network.result_max_age_seconds",
        ),
        probe_timeout_seconds=_toml_float(
            network_table.get("probe_timeout_seconds", 1.2),
            "network.probe_timeout_seconds",
        ),
        probe_bytes=_toml_int(
            network_table.get("probe_bytes", 32_768),
            "network.probe_bytes",
        ),
        goodput_probe_interval_seconds=_toml_float(
            network_table.get("goodput_probe_interval_seconds", 60.0),
            "network.goodput_probe_interval_seconds",
        ),
        minimum_quality_score=_toml_float(
            network_table.get("minimum_quality_score", 0.50),
            "network.minimum_quality_score",
        ),
        quality_hysteresis=_toml_float(
            network_table.get("quality_hysteresis", 0.05),
            "network.quality_hysteresis",
        ),
        target_ttfb_ms=_toml_float(
            network_table.get("target_ttfb_ms", 1_200.0),
            "network.target_ttfb_ms",
        ),
        target_jitter_ms=_toml_float(
            network_table.get("target_jitter_ms", 300.0),
            "network.target_jitter_ms",
        ),
        minimum_goodput_kbps=_toml_float(
            network_table.get("minimum_goodput_kbps", 128.0),
            "network.minimum_goodput_kbps",
        ),
        history_size=_toml_int(
            network_table.get("history_size", 8),
            "network.history_size",
        ),
        require_wifi=_toml_bool(
            network_table.get("require_wifi", False),
            "network.require_wifi",
        ),
        interface_allowlist=_string_tuple(
            network_table.get("interface_allowlist"),
            "network.interface_allowlist",
        ),
    )

    modes_table = _table(data.get("modes"), "modes")
    _reject_unknown(modes_table, {"talk", "think"}, "modes")

    def parse_mode(name: str, *, default_output: int, default_complexity: int) -> LLMModeSettings:
        mode = _table(modes_table.get(name), f"modes.{name}")
        _reject_unknown(
            mode,
            {
                "candidates",
                "max_output_tokens",
                "complexity_threshold",
                "first_speech_min_chars",
                "speech_chunk_max_chars",
                "first_visible_token_seconds",
            },
            f"modes.{name}",
        )
        return LLMModeSettings(
            candidates=_string_tuple(mode.get("candidates"), f"modes.{name}.candidates"),
            max_output_tokens=_toml_int(
                mode.get("max_output_tokens", default_output),
                f"modes.{name}.max_output_tokens",
            ),
            complexity_threshold=_toml_int(
                mode.get("complexity_threshold", default_complexity),
                f"modes.{name}.complexity_threshold",
            ),
            first_speech_min_chars=_toml_int(
                mode.get("first_speech_min_chars", 0),
                f"modes.{name}.first_speech_min_chars",
            ),
            speech_chunk_max_chars=_toml_int(
                mode.get("speech_chunk_max_chars", 0),
                f"modes.{name}.speech_chunk_max_chars",
            ),
            first_visible_token_seconds=(
                _toml_float(
                    mode["first_visible_token_seconds"],
                    f"modes.{name}.first_visible_token_seconds",
                )
                if "first_visible_token_seconds" in mode
                else None
            ),
        )

    providers_table = _table(data.get("providers"), "providers")
    providers: list[LLMProviderSettings] = []
    for name, raw_provider in providers_table.items():
        if not isinstance(name, str):
            raise ConfigurationError("provider names must be strings")
        provider = _table(raw_provider, f"providers.{name}")
        _reject_unknown(
            provider,
            {
                "adapter",
                "endpoint",
                "locality",
                "api_key_env",
                "enabled",
                "internal_retries",
            },
            f"providers.{name}",
        )
        providers.append(
            LLMProviderSettings(
                name=name,
                adapter=_toml_string(
                    provider.get("adapter", ""),
                    f"providers.{name}.adapter",
                ),
                endpoint=_toml_string(
                    provider.get("endpoint", ""),
                    f"providers.{name}.endpoint",
                ),
                locality=_toml_string(
                    provider.get("locality", "remote"),
                    f"providers.{name}.locality",
                ),
                api_key_env=(
                    _toml_string(
                        provider["api_key_env"],
                        f"providers.{name}.api_key_env",
                    )
                    if "api_key_env" in provider
                    else None
                ),
                enabled=_toml_bool(
                    provider.get("enabled", True),
                    f"providers.{name}.enabled",
                ),
                internal_retries=_toml_int(
                    provider.get("internal_retries", 0),
                    f"providers.{name}.internal_retries",
                ),
            )
        )

    targets_table = _table(data.get("targets"), "targets")
    targets: list[LLMTargetSettings] = []
    for name, raw_target in targets_table.items():
        if not isinstance(name, str):
            raise ConfigurationError("target names must be strings")
        target = _table(raw_target, f"targets.{name}")
        _reject_unknown(
            target,
            {
                "provider",
                "model",
                "model_by_language",
                "catalog_id",
                "tier",
                "languages",
                "context_window",
                "max_output_tokens",
                "max_output_words",
                "min_complexity_score",
                "retry_attempts",
                "options",
            },
            f"targets.{name}",
        )
        language_models = _table(
            target.get("model_by_language"), f"targets.{name}.model_by_language"
        )
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in language_models.items()
        ):
            raise ConfigurationError(
                f"targets.{name}.model_by_language must map strings to strings"
            )
        options = _table(target.get("options"), f"targets.{name}.options")
        if _contains_secret_option(options):
            raise ConfigurationError(f"targets.{name}.options cannot contain secrets")
        targets.append(
            LLMTargetSettings(
                name=name,
                provider=_toml_string(
                    target.get("provider", ""),
                    f"targets.{name}.provider",
                ),
                model=(
                    _toml_string(target["model"], f"targets.{name}.model")
                    if "model" in target
                    else None
                ),
                model_by_language=tuple(
                    sorted((str(key), str(value)) for key, value in language_models.items())
                ),
                catalog_id=(
                    _toml_string(
                        target["catalog_id"],
                        f"targets.{name}.catalog_id",
                    )
                    if "catalog_id" in target
                    else None
                ),
                tier=(
                    _toml_string(target["tier"], f"targets.{name}.tier")
                    if "tier" in target
                    else None
                ),
                languages=_string_tuple(
                    target.get("languages", ["it", "en"]),
                    f"targets.{name}.languages",
                ),
                context_window=(
                    _toml_int(
                        target["context_window"],
                        f"targets.{name}.context_window",
                    )
                    if "context_window" in target
                    else None
                ),
                max_output_tokens=(
                    _toml_int(
                        target["max_output_tokens"],
                        f"targets.{name}.max_output_tokens",
                    )
                    if "max_output_tokens" in target
                    else None
                ),
                max_output_words=(
                    _toml_int(
                        target["max_output_words"],
                        f"targets.{name}.max_output_words",
                    )
                    if "max_output_words" in target
                    else None
                ),
                min_complexity_score=(
                    _toml_int(
                        target["min_complexity_score"],
                        f"targets.{name}.min_complexity_score",
                    )
                    if "min_complexity_score" in target
                    else None
                ),
                retry_attempts=_toml_int(
                    target.get("retry_attempts", 1),
                    f"targets.{name}.retry_attempts",
                ),
                options=tuple(sorted(options.items())),
            )
        )

    return LLMSettings(
        routing_file=routing_path,
        routing_policy=_toml_string(
            router.get("policy", "local_only"),
            "router.policy",
        ),
        remote_enabled=_toml_bool(
            router.get("remote_enabled", False),
            "router.remote_enabled",
        ),
        allowlist=_string_tuple(router.get("allowlist"), "router.allowlist"),
        denylist=_string_tuple(router.get("denylist"), "router.denylist"),
        unknown_connectivity=_toml_string(
            router.get("unknown_connectivity", "prefer_local"),
            "router.unknown_connectivity",
        ),
        privacy=privacy,
        timeouts=timeouts,
        health=health,
        budget=budget,
        observability=observability,
        network=network,
        talk=parse_mode("talk", default_output=64, default_complexity=3),
        think=parse_mode("think", default_output=256, default_complexity=2),
        providers=tuple(providers),
        targets=tuple(targets),
    )


def _llm_from_env(
    base: LLMSettings,
    environ: Mapping[str, str],
    *,
    project_root: Path,
) -> LLMSettings:
    emergency = _bool_from_env(
        environ.get("HELIOS_LLM_EMERGENCY_LOCAL_ONLY", "false"),
        "HELIOS_LLM_EMERGENCY_LOCAL_ONLY",
    )

    requested_remote = _bool_from_env(
        environ.get("HELIOS_LLM_REMOTE_ENABLED", str(base.remote_enabled)),
        "HELIOS_LLM_REMOTE_ENABLED",
    )
    # Remote enablement requires a validated routing file. The file defines its
    # default, while the environment can explicitly disable or re-enable it.
    remote_enabled = requested_remote and base.routing_file is not None and not emergency

    requested_policy = environ.get("HELIOS_LLM_POLICY", base.routing_policy).strip()
    if requested_policy not in _ROUTING_POLICIES:
        raise ConfigurationError(f"Unsupported LLM routing policy: {requested_policy!r}")
    routing_policy = (
        requested_policy if remote_enabled or requested_policy == "local_only" else "local_only"
    )

    privacy = replace(
        base.privacy,
        allow_remote_transcripts=_bool_from_env(
            environ.get(
                "HELIOS_LLM_ALLOW_REMOTE_TRANSCRIPTS",
                str(base.privacy.allow_remote_transcripts),
            ),
            "HELIOS_LLM_ALLOW_REMOTE_TRANSCRIPTS",
        ),
        allow_remote_context=_bool_from_env(
            environ.get(
                "HELIOS_LLM_ALLOW_REMOTE_CONTEXT",
                str(base.privacy.allow_remote_context),
            ),
            "HELIOS_LLM_ALLOW_REMOTE_CONTEXT",
        ),
        allow_remote_rag_context=_bool_from_env(
            environ.get(
                "HELIOS_LLM_ALLOW_REMOTE_RAG",
                str(base.privacy.allow_remote_rag_context),
            ),
            "HELIOS_LLM_ALLOW_REMOTE_RAG",
        ),
    )

    budget = replace(
        base.budget,
        catalog_path=(
            (
                Path(environ["HELIOS_LLM_CATALOG"]).expanduser()
                if Path(environ["HELIOS_LLM_CATALOG"]).expanduser().is_absolute()
                else project_root / Path(environ["HELIOS_LLM_CATALOG"]).expanduser()
            ).resolve()
            if environ.get("HELIOS_LLM_CATALOG")
            else base.budget.catalog_path
        ),
        daily_usd=_decimal(
            environ.get("HELIOS_LLM_DAILY_BUDGET_USD", base.budget.daily_usd),
            "HELIOS_LLM_DAILY_BUDGET_USD",
        ),
        monthly_usd=_decimal(
            environ.get("HELIOS_LLM_MONTHLY_BUDGET_USD", base.budget.monthly_usd),
            "HELIOS_LLM_MONTHLY_BUDGET_USD",
        ),
        zero_cost_only=_bool_from_env(
            environ.get("HELIOS_LLM_ZERO_COST_ONLY", str(base.budget.zero_cost_only)),
            "HELIOS_LLM_ZERO_COST_ONLY",
        ),
    )
    observability = replace(
        base.observability,
        metrics_enabled=_bool_from_env(
            environ.get(
                "HELIOS_LLM_METRICS_ENABLED",
                str(base.observability.metrics_enabled),
            ),
            "HELIOS_LLM_METRICS_ENABLED",
        ),
        log_content=_bool_from_env(
            environ.get("HELIOS_LLM_LOG_CONTENT", str(base.observability.log_content)),
            "HELIOS_LLM_LOG_CONTENT",
        ),
    )

    return replace(
        base,
        routing_policy=routing_policy,
        remote_enabled=remote_enabled,
        emergency_local_only=emergency,
        privacy=privacy,
        context_idle_timeout_seconds=_float_from_env(
            environ.get(
                "HELIOS_LLM_CONTEXT_IDLE_TIMEOUT_SECONDS",
                str(base.context_idle_timeout_seconds),
            ),
            "HELIOS_LLM_CONTEXT_IDLE_TIMEOUT_SECONDS",
        ),
        context_max_turns=_int_from_env(
            environ.get(
                "HELIOS_LLM_CONTEXT_MAX_TURNS",
                str(base.context_max_turns),
            ),
            "HELIOS_LLM_CONTEXT_MAX_TURNS",
        ),
        budget=budget,
        observability=observability,
    )


@dataclass(frozen=True)
class Settings:
    """Validated runtime settings.

    ``project_root`` can be replaced in tests or deployments while all derived
    paths remain anchored to it.
    """

    project_root: Path = PROJECT_ROOT
    language: str = "it"
    name: str = "emilia"
    listen_timeout: float = 6.5
    barge_in_enabled: bool = True
    barge_in_event_energy: float = 0.08
    barge_in_expected_echo_energy: float = 0.04
    barge_in_minimum_interrupt_energy: float = 0.06
    backchannel_delay_seconds: float = 0.7
    log_level: int = logging.INFO
    log_format: str = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
    log_file_name: str | None = "app.log"
    audio_input_device: int | str | None = None
    audio_output_device: int | str | None = None
    audio_output_latency: str = "high"
    ollama_host: str = "http://localhost:11434"
    think_model: str = "qwen3:0.6b"
    top_k: int = 4
    llm: LLMSettings = field(default_factory=LLMSettings)
    kpi: KPISettings = field(default_factory=KPISettings)

    def __post_init__(self) -> None:
        root = Path(self.project_root).expanduser().resolve()
        language = self.language.strip().lower()
        if language not in _profile_paths(root):
            supported = ", ".join(sorted(_profile_paths(root)))
            raise ConfigurationError(
                f"Unsupported language {self.language!r}; expected one of: {supported}"
            )
        if self.listen_timeout <= 0:
            raise ConfigurationError("listen_timeout must be greater than zero")
        for name, value in (
            ("barge_in_event_energy", self.barge_in_event_energy),
            ("barge_in_expected_echo_energy", self.barge_in_expected_echo_energy),
            ("barge_in_minimum_interrupt_energy", self.barge_in_minimum_interrupt_energy),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 <= float(value) <= 1
            ):
                raise ConfigurationError(f"{name} must be finite and between zero and one")
        if (
            isinstance(self.backchannel_delay_seconds, bool)
            or not isinstance(self.backchannel_delay_seconds, (int, float))
            or not math.isfinite(float(self.backchannel_delay_seconds))
            or self.backchannel_delay_seconds <= 0
        ):
            raise ConfigurationError("backchannel_delay_seconds must be positive")
        if self.top_k < 1:
            raise ConfigurationError("top_k must be at least one")

        for name, value in (
            ("audio_input_device", self.audio_input_device),
            ("audio_output_device", self.audio_output_device),
        ):
            if value is None:
                continue
            if isinstance(value, bool):
                raise ConfigurationError(f"{name} must be a device index or name")
            if isinstance(value, int):
                if value < 0:
                    raise ConfigurationError(f"{name} must be a non-negative device index")
                continue
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 256:
                raise ConfigurationError(f"{name} must be a device index or name")
            object.__setattr__(self, name, value.strip())

        latency = self.audio_output_latency.strip().lower()
        if latency not in {"low", "high"}:
            raise ConfigurationError("audio_output_latency must be 'low' or 'high'")

        object.__setattr__(self, "project_root", root)
        object.__setattr__(self, "language", language)
        object.__setattr__(self, "audio_output_latency", latency)
        object.__setattr__(self, "ollama_host", normalize_ollama_host(self.ollama_host))
        kpi_path = self.kpi.storage_path.expanduser()
        if not kpi_path.is_absolute():
            kpi_path = root / kpi_path
        object.__setattr__(self, "kpi", replace(self.kpi, storage_path=kpi_path.resolve()))

    @classmethod
    def from_env(
        cls,
        project_root: Path = PROJECT_ROOT,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> Settings:
        """Build settings from deployment overrides.

        The bundled Codex-subscription profile is selected by default when it is
        present. A missing explicit or invalid hybrid-routing file always fails
        closed to local inference. Secret values are never accepted from it.
        """

        env = os.environ if environ is None else environ
        root = Path(project_root).expanduser().resolve()
        llm = LLMSettings()
        routing_override = env.get("HELIOS_LLM_CONFIG")
        # Remote routing is opt-in. Files under examples/ are documentation, not
        # active configuration: auto-selecting one meant that cloning the
        # repository and running it sent voice transcripts to a remote provider
        # without the operator ever choosing to. A clean checkout now stays on
        # the local_only defaults of LLMSettings() until HELIOS_LLM_CONFIG names
        # a routing file explicitly.
        routing_value = "" if routing_override is None else routing_override.strip()
        configuration_valid = True
        if routing_value:
            routing_path = Path(routing_value).expanduser()
            if not routing_path.is_absolute():
                routing_path = root / routing_path
            try:
                llm = load_llm_settings(routing_path)
            except ConfigurationError:
                configuration_valid = False
                logger.error("Invalid HELIOS_LLM_CONFIG; remote language-model routing is disabled")

        if configuration_valid:
            try:
                llm = _llm_from_env(llm, env, project_root=root)
            except ConfigurationError:
                logger.error("Invalid hybrid LLM environment override; remote routing is disabled")
                llm = LLMSettings(emergency_local_only=True)
        else:
            llm = LLMSettings(emergency_local_only=True)

        # Transcript egress is the single most consequential setting in an
        # in-vehicle voice deployment, so make it visible at startup instead of
        # leaving it implicit in a routing file.
        if llm.remote_enabled and llm.privacy.allow_remote_transcripts:
            logger.warning(
                "Remote routing is enabled and voice transcripts may be sent off-device "
                "(routing file: %s). Set HELIOS_LLM_ALLOW_REMOTE_TRANSCRIPTS=false to keep "
                "recognized speech local.",
                llm.routing_file,
            )

        try:
            kpi = _kpi_from_env(env, project_root=root)
        except ConfigurationError:
            logger.error("Invalid KPI configuration; KPI collection and dashboard are disabled")
            kpi = KPISettings(storage_path=(root / "logs/helios-kpi.sqlite3").resolve())

        return cls(
            project_root=root,
            language=env.get("HELIOS_LANGUAGE", "it"),
            barge_in_enabled=_bool_from_env(
                env.get("HELIOS_BARGE_IN_ENABLED", "true"),
                "HELIOS_BARGE_IN_ENABLED",
            ),
            barge_in_event_energy=_float_from_env(
                env.get("HELIOS_BARGE_IN_EVENT_ENERGY", "0.08"),
                "HELIOS_BARGE_IN_EVENT_ENERGY",
            ),
            barge_in_expected_echo_energy=_float_from_env(
                env.get("HELIOS_BARGE_IN_EXPECTED_ECHO_ENERGY", "0.04"),
                "HELIOS_BARGE_IN_EXPECTED_ECHO_ENERGY",
            ),
            barge_in_minimum_interrupt_energy=_float_from_env(
                env.get("HELIOS_BARGE_IN_MINIMUM_INTERRUPT_ENERGY", "0.06"),
                "HELIOS_BARGE_IN_MINIMUM_INTERRUPT_ENERGY",
            ),
            backchannel_delay_seconds=_float_from_env(
                env.get("HELIOS_BACKCHANNEL_DELAY_SECONDS", "0.7"),
                "HELIOS_BACKCHANNEL_DELAY_SECONDS",
            ),
            log_level=_log_level_from_env(env.get("HELIOS_LOG_LEVEL", "INFO")),
            log_file_name=_log_file_from_env(env.get("HELIOS_LOG_FILE")),
            audio_input_device=_audio_device_from_env(
                env.get("HELIOS_AUDIO_INPUT_DEVICE"),
                "HELIOS_AUDIO_INPUT_DEVICE",
            ),
            audio_output_device=_audio_device_from_env(
                env.get("HELIOS_AUDIO_OUTPUT_DEVICE"),
                "HELIOS_AUDIO_OUTPUT_DEVICE",
            ),
            audio_output_latency=env.get("HELIOS_AUDIO_OUTPUT_LATENCY", "high"),
            ollama_host=env.get("HELIOS_OLLAMA_HOST", "http://localhost:11434"),
            llm=llm,
            kpi=kpi,
        )

    @property
    def profile(self) -> LanguageProfile:
        return _profile_paths(self.project_root)[self.language]

    @property
    def log_file(self) -> Path | None:
        return self.project_root / self.log_file_name if self.log_file_name else None

    @property
    def upload_folder(self) -> Path:
        return self.project_root / "uploads"

    @property
    def wake_sound(self) -> Path:
        return self.project_root / "sounds/wake_up.wav"

    @property
    def stop_sound(self) -> Path:
        return self.project_root / "sounds/stop.wav"

    @property
    def embeddings_file(self) -> Path:
        return self.project_root / "embeddings.npz"

    @property
    def sentence_transformer_model(self) -> Path:
        return self.project_root / "models/all-MiniLM-L6-v2"


SETTINGS = Settings.from_env()
PROFILE = SETTINGS.profile
LLM_SETTINGS = SETTINGS.llm
KPI_SETTINGS = SETTINGS.kpi

# Backward-compatible constants.
LOG_LEVEL = SETTINGS.log_level
LOG_FORMAT = SETTINGS.log_format
LOG_FILE = str(SETTINGS.log_file) if SETTINGS.log_file else None

NAME = SETTINGS.name
LANGUAGE = SETTINGS.language
TTS_MODEL = str(PROFILE.tts_model)

WAKE_WORD = PROFILE.wake_word
WAKE_WORD_ALIASES = PROFILE.wake_word_aliases
RAG_WORD = PROFILE.rag_word
LISTEN_TIMEOUT = SETTINGS.listen_timeout
WAKE_SOUND = str(SETTINGS.wake_sound)
STOP_SOUND = str(SETTINGS.stop_sound)
TIMEOUT_SOUND = STOP_SOUND

UPLOAD_FOLDER = str(SETTINGS.upload_folder)

VOSK_MODEL_PATH = str(PROFILE.vosk_model)
MODEL_TALK = PROFILE.talk_model
MODEL_THINK = SETTINGS.think_model

PRES_Q_1, PRES_Q_2, PRES_Q_3 = PROFILE.presentation_questions
PRES_A_1, PRES_A_2, PRES_A_3 = PROFILE.presentation_answers
PRES_A_SWITCH = {1: PRES_A_1, 2: PRES_A_2, 3: PRES_A_3}

OLLAMA_HOST = SETTINGS.ollama_host
# Legacy callers may still pass the generate endpoint; APIClient normalizes it.
OLLAMA_API_URL = f"{OLLAMA_HOST}/api/generate"
TOP_K = SETTINGS.top_k
