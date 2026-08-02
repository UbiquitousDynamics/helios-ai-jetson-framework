"""Deterministic provider selection and lazy provider ownership."""

from __future__ import annotations

import logging
import math
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from api.health import HealthTracker
from api.providers.contracts import ChatProvider, ChatRequest, ContentOrigin

logger = logging.getLogger(__name__)


class RoutingPolicy(str, Enum):
    LOCAL_ONLY = "local_only"
    REMOTE_ONLY = "remote_only"
    LOCAL_FIRST = "local_first"
    REMOTE_FIRST = "remote_first"
    AUTO = "auto"


class Connectivity(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class NoRouteError(RuntimeError):
    """No provider target satisfies the deterministic eligibility rules."""


@dataclass(frozen=True, slots=True)
class ProviderTarget:
    name: str
    provider: str
    model: str
    remote: bool
    modes: frozenset[str] = frozenset({"talk", "think"})
    languages: frozenset[str] = frozenset()
    context_window: int | None = None
    max_output_tokens: int | None = None
    min_complexity_score: int | None = None
    features: frozenset[str] = frozenset()
    priority: int = 100
    enabled: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not isinstance(self.provider, str)
            or not isinstance(self.model, str)
            or not self.name.strip()
            or not self.provider.strip()
            or not self.model.strip()
        ):
            raise ValueError("target name, provider, and model cannot be empty")
        modes = frozenset({self.modes} if isinstance(self.modes, str) else self.modes)
        features = frozenset({self.features} if isinstance(self.features, str) else self.features)
        language_values = {self.languages} if isinstance(self.languages, str) else self.languages
        languages = frozenset(language.lower() for language in language_values)
        if not modes or not modes.issubset({"talk", "think"}):
            raise ValueError("target modes must contain only talk and/or think")
        if not isinstance(self.remote, bool) or not isinstance(self.enabled, bool):
            raise ValueError("target remote and enabled flags must be booleans")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValueError("target priority must be an integer")
        if self.context_window is not None and (
            isinstance(self.context_window, bool)
            or not isinstance(self.context_window, int)
            or self.context_window < 1
        ):
            raise ValueError("context_window must be a positive integer")
        if self.max_output_tokens is not None and (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens < 1
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        if self.min_complexity_score is not None and (
            isinstance(self.min_complexity_score, bool)
            or not isinstance(self.min_complexity_score, int)
            or self.min_complexity_score < 0
        ):
            raise ValueError("min_complexity_score must be a non-negative integer")
        object.__setattr__(self, "modes", modes)
        object.__setattr__(self, "features", features)
        object.__setattr__(self, "languages", languages)

    @property
    def health_key(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(slots=True)
class _ProviderRegistration:
    factory: Callable[[], ChatProvider]
    owned: bool
    instance: ChatProvider | None = None


class ProviderRegistry:
    """Constructs providers only on first selection and closes owned instances."""

    def __init__(self) -> None:
        self._registrations: dict[str, _ProviderRegistration] = {}
        self._closed = False
        self._lock = threading.RLock()

    def register(
        self,
        name: str,
        factory: Callable[[], ChatProvider],
        *,
        owned: bool = True,
    ) -> None:
        if not name.strip():
            raise ValueError("provider name cannot be empty")
        if not callable(factory):
            raise TypeError("provider factory must be callable")
        with self._lock:
            if self._closed:
                raise RuntimeError("provider registry is closed")
            if name in self._registrations:
                raise ValueError(f"provider is already registered: {name}")
            self._registrations[name] = _ProviderRegistration(factory, owned)

    def register_instance(
        self,
        name: str,
        provider: ChatProvider,
        *,
        owned: bool = False,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("provider name cannot be empty")
        with self._lock:
            if self._closed:
                raise RuntimeError("provider registry is closed")
            if name in self._registrations:
                raise ValueError(f"provider is already registered: {name}")
            self._registrations[name] = _ProviderRegistration(
                lambda: provider,
                owned,
                provider,
            )

    def get(self, name: str) -> ChatProvider:
        with self._lock:
            if self._closed:
                raise RuntimeError("provider registry is closed")
            try:
                registration = self._registrations[name]
            except KeyError as exc:
                raise KeyError(f"unknown provider: {name}") from exc
            if registration.instance is None:
                registration.instance = registration.factory()
            return registration.instance

    def close(self) -> None:
        first_error: BaseException | None = None
        with self._lock:
            if self._closed:
                return
            self._closed = True
            registrations = tuple(self._registrations.values())
        for registration in reversed(registrations):
            if registration.owned and registration.instance is not None:
                try:
                    registration.instance.close()
                except BaseException as exc:  # cleanup must continue
                    if first_error is None:
                        first_error = exc
        if first_error is not None:
            raise first_error

    def __enter__(self) -> ProviderRegistry:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


_REASONING_CUES = frozenset(
    {
        "analyze",
        "analyse",
        "calculate",
        "compare",
        "explain",
        "plan",
        "reason",
        "analizza",
        "calcola",
        "confronta",
        "perch\u00e9",
        "perche",
        "pianifica",
        "spiega",
    }
)
_CONNECTOR_PATTERN = re.compile(
    r"(?:[?;\n]+|\b(?:and|then|also|but|e|poi|anche|ma)\b)",
    re.IGNORECASE,
)


class RoutePlanner:
    """Filters and orders targets without probing or constructing providers."""

    def __init__(
        self,
        targets: Iterable[ProviderTarget],
        *,
        policy: RoutingPolicy | str = RoutingPolicy.LOCAL_ONLY,
        allowlist: Iterable[str] = (),
        denylist: Iterable[str] = (),
        health: HealthTracker | None = None,
        auto_complexity_threshold: int = 3,
        mode_candidates: Mapping[str, Iterable[str]] | None = None,
        allow_remote_when_connectivity_unknown: bool = False,
    ) -> None:
        self._targets = tuple(targets)
        names = [target.name for target in self._targets]
        if len(names) != len(set(names)):
            raise ValueError("provider target names must be unique")
        self.policy = RoutingPolicy(policy)
        self.allowlist = frozenset({allowlist} if isinstance(allowlist, str) else allowlist)
        self.denylist = frozenset({denylist} if isinstance(denylist, str) else denylist)
        self.health = health
        if auto_complexity_threshold < 0:
            raise ValueError("auto_complexity_threshold cannot be negative")
        self.auto_complexity_threshold = auto_complexity_threshold
        if not isinstance(allow_remote_when_connectivity_unknown, bool):
            raise TypeError("allow_remote_when_connectivity_unknown must be a boolean")
        self.allow_remote_when_connectivity_unknown = allow_remote_when_connectivity_unknown
        self._mode_ranks: dict[str, dict[str, int]] = {}
        if mode_candidates is not None:
            known_names = set(names)
            for mode, candidates in mode_candidates.items():
                if mode not in {"talk", "think"}:
                    raise ValueError(f"unsupported candidate-chain mode: {mode}")
                chain = tuple(candidates)
                if len(chain) != len(set(chain)):
                    raise ValueError(f"{mode} candidate chain contains duplicates")
                missing = set(chain) - known_names
                if missing:
                    raise ValueError(
                        f"{mode} candidate chain contains unknown targets: "
                        f"{', '.join(sorted(missing))}"
                    )
                self._mode_ranks[mode] = {
                    target_name: rank for rank, target_name in enumerate(chain)
                }

    def plan(
        self,
        request: ChatRequest,
        *,
        connectivity: Connectivity | str = Connectivity.UNKNOWN,
        estimated_input_tokens: int | None = None,
        policy: RoutingPolicy | str | None = None,
        complexity_threshold: int | None = None,
    ) -> tuple[ProviderTarget, ...]:
        selected_policy = self.policy if policy is None else RoutingPolicy(policy)
        connectivity = Connectivity(connectivity)
        input_tokens = (
            self.estimate_input_tokens(request)
            if estimated_input_tokens is None
            else estimated_input_tokens
        )
        if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
            raise ValueError("estimated_input_tokens must be a non-negative integer")

        mode_ranks = self._mode_ranks.get(request.mode)
        eligible = [
            (index, target)
            for index, target in enumerate(self._targets)
            if self._eligible(
                target,
                request,
                connectivity=connectivity,
                estimated_input_tokens=input_tokens,
            )
        ]
        adaptive_remotes = [
            pair for pair in eligible if pair[1].remote and pair[1].min_complexity_score is not None
        ]
        local_context = None
        score: int | None = None
        if adaptive_remotes or selected_policy is RoutingPolicy.AUTO:
            local_context = max(
                (
                    target.context_window
                    for _, target in eligible
                    if not target.remote and target.context_window is not None
                ),
                default=None,
            )
        if adaptive_remotes:
            score = self.complexity_score(
                request,
                estimated_input_tokens=input_tokens,
                local_context_window=local_context,
            )
            eligible_floors = [
                target.min_complexity_score
                for _, target in adaptive_remotes
                if target.min_complexity_score is not None and target.min_complexity_score <= score
            ]
            selected_floor = (
                max(eligible_floors)
                if eligible_floors
                else min(
                    target.min_complexity_score
                    for _, target in adaptive_remotes
                    if target.min_complexity_score is not None
                )
            )
            selected_pair = min(
                (
                    pair
                    for pair in adaptive_remotes
                    if pair[1].min_complexity_score == selected_floor
                ),
                key=lambda pair: (
                    mode_ranks.get(pair[1].name, len(mode_ranks))
                    if mode_ranks is not None
                    else pair[1].priority,
                    pair[1].priority,
                    pair[0],
                ),
            )
            tiered_names = {target.name for _, target in adaptive_remotes}
            eligible = [
                pair
                for pair in eligible
                if pair[1].name not in tiered_names or pair[1].name == selected_pair[1].name
            ]
            logger.info(
                "Adaptive remote tier selection: complexity_score=%s, "
                "minimum_score=%s, selected=%s",
                score,
                selected_floor,
                selected_pair[1].name,
            )
        eligible.sort(
            key=lambda pair: (
                mode_ranks.get(pair[1].name, len(mode_ranks))
                if mode_ranks is not None
                else pair[1].priority,
                pair[1].priority,
                pair[0],
            )
        )
        local = [target for _, target in eligible if not target.remote]
        remote = [target for _, target in eligible if target.remote]

        if selected_policy is RoutingPolicy.LOCAL_ONLY:
            result = local
        elif selected_policy is RoutingPolicy.REMOTE_ONLY:
            result = remote
        elif selected_policy is RoutingPolicy.LOCAL_FIRST:
            result = local + remote
        elif selected_policy is RoutingPolicy.REMOTE_FIRST:
            result = remote + local
        else:
            if connectivity is Connectivity.OFFLINE or (
                connectivity is Connectivity.UNKNOWN
                and not self.allow_remote_when_connectivity_unknown
            ):
                remote = []
            if score is None:
                score = self.complexity_score(
                    request,
                    estimated_input_tokens=input_tokens,
                    local_context_window=local_context,
                )
            threshold = (
                self.auto_complexity_threshold
                if complexity_threshold is None
                else complexity_threshold
            )
            if threshold < 0:
                raise ValueError("complexity_threshold cannot be negative")
            remote_wanted = (
                score >= threshold or not local or request.options.get("resource_offload") is True
            )
            result = remote + local if remote_wanted else local + remote

        if not result:
            raise NoRouteError("no eligible provider target")
        return tuple(result)

    def select(self, request: ChatRequest, **kwargs: object) -> ProviderTarget:
        return self.plan(request, **kwargs)[0]

    def complexity_score(
        self,
        request: ChatRequest,
        *,
        estimated_input_tokens: int | None = None,
        local_context_window: int | None = None,
    ) -> int:
        tokens = (
            self.estimate_input_tokens(request)
            if estimated_input_tokens is None
            else estimated_input_tokens
        )
        output_reserve = request.max_output_tokens or 0
        score = 0
        if local_context_window is not None and tokens + output_reserve > math.floor(
            local_context_window * 0.8
        ):
            score += 2
        if request.mode == "think":
            score += 1
        if tokens > 160:
            score += 1

        content = " ".join(message.content.lower() for message in request.messages)
        words = set(re.findall(r"\w+", content, flags=re.UNICODE))
        if words.intersection(_REASONING_CUES):
            score += 1
        if len(_CONNECTOR_PATTERN.findall(content)) >= 3:
            score += 1

        contextual_tokens = sum(
            self._estimate_text_tokens(message.content)
            for message in request.messages
            if message.origin
            in {
                ContentOrigin.STATIC_INSTRUCTION,
                ContentOrigin.CONVERSATION_HISTORY,
                ContentOrigin.TOOL_RESULT,
            }
        )
        if contextual_tokens > 64:
            score += 1
        if request.options.get("complex") is True:
            score += 2
        return score

    @classmethod
    def estimate_input_tokens(cls, request: ChatRequest) -> int:
        # This estimate protects context and budget gates without importing a
        # model-specific tokenizer. One token per UTF-8 byte plus conservative
        # Chat Completions framing is intentionally an upper bound for the
        # byte-fallback tokenizers supported by the initial adapters.
        return 8 + sum(
            16 + cls._estimate_text_tokens(message.content) for message in request.messages
        )

    @staticmethod
    def _estimate_text_tokens(content: str) -> int:
        return max(1, len(content.encode("utf-8")))

    def _eligible(
        self,
        target: ProviderTarget,
        request: ChatRequest,
        *,
        connectivity: Connectivity,
        estimated_input_tokens: int,
    ) -> bool:
        if not target.enabled or request.mode not in target.modes:
            return False
        mode_ranks = self._mode_ranks.get(request.mode)
        if mode_ranks is not None and target.name not in mode_ranks:
            return False
        if self._denied(target) or not self._allowed(target):
            return False
        if target.languages and request.language.lower() not in target.languages:
            return False
        if not request.required_features.issubset(target.features):
            return False
        requested_output = request.max_output_tokens
        if requested_output is None:
            output_tokens = target.max_output_tokens or 0
        elif target.max_output_tokens is None:
            output_tokens = requested_output
        else:
            # Output limits are per-target caps, not minimum capabilities.
            # Streaming applies the same clamp before provider execution.
            output_tokens = min(requested_output, target.max_output_tokens)
        if (
            target.context_window is not None
            and estimated_input_tokens + output_tokens > target.context_window
        ):
            return False
        if self.health is not None and not self.health.is_available(target.health_key):
            return False
        if target.remote:
            if request.remote_authorized is not True:
                return False
            if connectivity is Connectivity.OFFLINE:
                return False
        return True

    def _allowed(self, target: ProviderTarget) -> bool:
        if not self.allowlist:
            return True
        return target.name in self.allowlist or target.provider in self.allowlist

    def _denied(self, target: ProviderTarget) -> bool:
        return target.name in self.denylist or target.provider in self.denylist


__all__ = [
    "Connectivity",
    "NoRouteError",
    "ProviderRegistry",
    "ProviderTarget",
    "RoutePlanner",
    "RoutingPolicy",
]
