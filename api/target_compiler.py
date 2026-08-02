"""Compile validated LLM configuration into executable provider targets."""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping

import config
from api.catalog import CatalogError, ModelCatalog, ModelPrice
from api.routing import ProviderTarget
from api.streaming import ExecutionTarget

logger = logging.getLogger(__name__)


class TargetCompiler:
    """Pure configuration boundary for talk/think execution targets."""

    def __init__(
        self,
        settings: config.LLMSettings,
        *,
        models: Mapping[str, str],
        language: str,
        default_retry_attempts: int,
        registered_providers: Collection[str],
        ollama_remote: bool,
        ollama_enabled: bool,
        catalog: ModelCatalog | None = None,
    ) -> None:
        if default_retry_attempts < 1:
            raise ValueError("default_retry_attempts must be at least one")
        self.settings = settings
        self.models = dict(models)
        self.language = language
        self.default_retry_attempts = default_retry_attempts
        self.registered_providers = frozenset(registered_providers)
        self.ollama_remote = ollama_remote
        self.ollama_enabled = ollama_enabled
        self.catalog = catalog
        self._targets = {target.name: target for target in settings.targets}
        self._providers = {provider.name: provider for provider in settings.providers}

    def compile_all(self) -> dict[str, tuple[ExecutionTarget, ...]]:
        return {
            "talk": self.compile("talk"),
            "think": self.compile("think"),
        }

    def compile(self, mode: str) -> tuple[ExecutionTarget, ...]:
        mode_settings = self._mode_settings(mode)
        if not mode_settings.candidates:
            return (self._ollama_target(mode),)

        executions: list[ExecutionTarget] = []
        for priority, name in enumerate(mode_settings.candidates):
            target = self._targets[name]
            provider = self._providers.get(target.provider)
            if target.provider == "ollama":
                remote = self.ollama_remote
                enabled = self.ollama_enabled
            else:
                remote = provider is not None and provider.locality == "remote"
                enabled = (
                    provider is not None
                    and provider.enabled
                    and provider.name in self.registered_providers
                )
            model = target.model_for_language(self.language)
            price = self._price_for_target(target, provider, model) if remote else None
            maximum_output = self._minimum_defined(
                target.max_output_tokens,
                mode_settings.max_output_tokens,
                price.max_output_tokens if price is not None else None,
            )
            context_window = self._minimum_defined(
                target.context_window,
                price.context_window if price is not None else None,
            )
            executions.append(
                ExecutionTarget(
                    route=ProviderTarget(
                        name=target.name,
                        provider=target.provider,
                        model=model,
                        remote=remote,
                        modes=frozenset({mode}),
                        languages=frozenset(target.languages),
                        context_window=context_window,
                        max_output_tokens=maximum_output,
                        min_complexity_score=target.min_complexity_score,
                        priority=priority,
                        enabled=enabled,
                    ),
                    retry_attempts=target.retry_attempts,
                    max_output_tokens=maximum_output,
                    max_output_words=target.max_output_words,
                    options=dict(target.options),
                    price=price,
                )
            )

        if self.settings.emergency_local_only and not any(
            execution.route.provider == "ollama"
            and not execution.route.remote
            and execution.route.enabled
            for execution in executions
        ):
            if self.ollama_enabled and not self.ollama_remote:
                executions.append(
                    self._ollama_target(
                        mode,
                        name=f"ollama-emergency-{mode}",
                        priority=len(executions),
                    )
                )
        return tuple(executions)

    def _ollama_target(
        self,
        mode: str,
        *,
        name: str | None = None,
        priority: int = 0,
    ) -> ExecutionTarget:
        return ExecutionTarget(
            ProviderTarget(
                name=name or f"ollama-{mode}",
                provider="ollama",
                model=self.models[mode],
                remote=self.ollama_remote,
                modes=frozenset({mode}),
                languages=frozenset({self.language}),
                priority=priority,
                enabled=self.ollama_enabled,
            ),
            retry_attempts=self.default_retry_attempts,
        )

    def _price_for_target(
        self,
        target: config.LLMTargetSettings,
        provider: config.LLMProviderSettings | None,
        model: str,
    ) -> ModelPrice | None:
        if self.catalog is None or target.catalog_id is None or provider is None:
            return None
        try:
            price = self.catalog.get(target.catalog_id)
        except CatalogError:
            return None
        if price.provider != provider.name or price.model != model:
            logger.error("Catalog identity does not match target %s", target.name)
            return None
        return price

    def _mode_settings(self, mode: str) -> config.LLMModeSettings:
        if mode == "talk":
            return self.settings.talk
        if mode == "think":
            return self.settings.think
        raise ValueError(f"Unknown model mode: {mode!r}")

    @staticmethod
    def _minimum_defined(*values: int | None) -> int | None:
        defined = tuple(value for value in values if value is not None)
        return min(defined) if defined else None


__all__ = ["TargetCompiler"]
