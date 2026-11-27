"""Per-request LLM provider selection.

The runner is built once with a single shared managed provider, but an agent's
``ModelConfig`` can override the backend, model, and temperature. This turns
that config into the concrete provider to use for one request:

  managed, no override   reuse the shared managed provider untouched
  managed, override      build on the platform key honouring backend/model/temp,
                         cached by (backend, model, temperature)

A managed override that cannot be honoured — a stored backend the deployment
holds no key for, or a blank model id — falls back to the shared managed
provider and logs the reason: that is a misconfiguration, not a request to call
a provider with nothing.

"Blank means inherit" is the rule for an agent: an empty ``model`` (or an empty
``backend``) means the deployment's own LLM_BACKEND/DEFAULT_LLM_MODEL, so
clearing the field in the dashboard hands the agent back to the platform default
rather than posting an empty model id to whatever backend was last stored.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.adapters.llm.factory import backend_options, build_provider, platform_key
from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.agent_config import ModelConfig
from app.domain.ports import LLMProvider

logger = get_logger(__name__)

# current Claude models reject `temperature`; the anthropic adapter never sends
# it and its constructor does not accept it, so it is never passed as an option
_TEMPERATURE_BACKENDS = frozenset({"openai", "openrouter", "gemini", "nvidia"})

# the temperature the shared managed provider was built with — an agent that
# leaves it here (the ModelConfig default) can reuse that provider directly
_DEFAULT_TEMPERATURE = ModelConfig.model_fields["temperature"].default


class ModelResolver:
    def __init__(
        self,
        settings: Settings,
        managed: LLMProvider,
        track: Callable[[Any], Any],
    ) -> None:
        self._settings = settings
        self._managed = managed
        self._track = track
        # built providers outlive the request; cached so a hot agent does not
        # open a fresh httpx client on every question, and closed at shutdown
        self._cache: dict[tuple[Any, ...], LLMProvider] = {}

    def resolve(self, model: ModelConfig) -> LLMProvider:
        return self._with_fallback(model, self._resolve_managed(model))

    def _with_fallback(self, model: ModelConfig, provider: LLMProvider) -> LLMProvider:
        """Honour `ModelConfig.fallback_model` when one can be built.

        The fallback is a second model on the primary's backend, built on the
        platform key, wrapped so an outage of the primary degrades to it instead
        of failing the answer.
        """
        fallback_name = (model.fallback_model or "").strip()
        if not fallback_name:
            return provider
        backend = self._settings.LLM_BACKEND if not model.backend else model.backend
        key = platform_key(self._settings, backend)
        if not key:
            logger.warning("fallback_model_no_platform_key", backend=backend)
            return provider
        if backend != self._settings.LLM_BACKEND and not (model.model or "").strip():
            # nothing was built for that backend anyway; the shared managed
            # provider already handled it
            return provider
        try:
            cache_key = ("fallback", backend, model.model, fallback_name, model.temperature)
            fallback_provider = self._build(
                cache_key, backend, key, fallback_name, model.temperature
            )
        except Exception:
            logger.warning(
                "fallback_model_build_failed", backend=backend, model=fallback_name
            )
            return provider
        if fallback_provider is provider:
            return provider
        from app.adapters.llm.base import FallbackLLM

        return FallbackLLM([provider, fallback_provider])

    def _resolve_managed(self, model: ModelConfig) -> LLMProvider:
        backend = model.backend if model.backend else self._settings.LLM_BACKEND
        name = (model.model or "").strip()
        # an agent that names no model wants whatever the deployment runs. only
        # the platform's own backend can supply that default — DEFAULT_LLM_MODEL
        # is a model id for LLM_BACKEND and means nothing to another vendor
        if not name and backend == self._settings.LLM_BACKEND:
            name = self._settings.DEFAULT_LLM_MODEL.strip()
        if not name:
            logger.warning(
                "managed_model_unset_using_shared", backend=backend
            )
            return self._managed
        if not platform_key(self._settings, backend):
            # a backend left over from an earlier deployment, or one whose key was
            # never filled in. answering on the shared provider beats a 401
            logger.warning("managed_backend_no_platform_key", backend=backend)
            return self._managed
        if self._matches_shared_default(backend, name, model.temperature):
            return self._managed
        return self._build(
            ("managed", backend, name, model.temperature),
            backend,
            platform_key(self._settings, backend),
            name,
            model.temperature,
        )

    def _build(
        self,
        cache_key: tuple[Any, ...],
        backend: str,
        api_key: str,
        model: str,
        temperature: float,
    ) -> LLMProvider:
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        provider = build_provider(backend, api_key, model, **self._options(backend, temperature))
        self._track(provider)
        self._cache[cache_key] = provider
        return provider

    def _options(self, backend: str, temperature: float) -> dict[str, Any]:
        options = backend_options(self._settings, backend)
        if backend in _TEMPERATURE_BACKENDS:
            options["temperature"] = temperature
        return options

    def _matches_shared_default(self, backend: str, name: str, temperature: float) -> bool:
        """True when the agent asks for exactly what the shared provider already is.

        Takes the already-normalised backend and model, so an agent that inherits
        both by leaving them blank reuses the shared provider instead of building
        a second identical client.
        """
        if backend != self._settings.LLM_BACKEND:
            return False
        if name != self._settings.DEFAULT_LLM_MODEL:
            return False
        return not (
            backend in _TEMPERATURE_BACKENDS and temperature != _DEFAULT_TEMPERATURE
        )
