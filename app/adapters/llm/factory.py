"""LLM provider construction from settings or per tenant configuration.

Model IDs live in configuration, never in code. Tenants on the managed tier get
the platform default.
"""

from __future__ import annotations

from typing import Any

from app.adapters.llm.anthropic import AnthropicProvider
from app.adapters.llm.base import FallbackLLM
from app.adapters.llm.gemini_native import gemini_native
from app.adapters.llm.openai_compatible import nvidia, openai, openrouter
from app.core.config import Settings
from app.core.errors import ConfigError
from app.domain.ports import LLMProvider


def build_provider(backend: str, api_key: str, model: str, **options: Any) -> LLMProvider:
    if not api_key:
        raise ConfigError(f"no api key configured for llm backend {backend!r}")

    match backend:
        case "anthropic":
            return AnthropicProvider(api_key, model, **options)
        case "openai":
            return openai(api_key, model, **options)
        case "openrouter":
            return openrouter(api_key, model, **options)
        case "gemini":
            return gemini_native(api_key, model, **options)
        case "nvidia":
            return nvidia(api_key, model, **options)
        case _:
            raise ConfigError(f"unknown llm backend {backend!r}")


def platform_key(settings: Settings, backend: str) -> str:
    return {
        "anthropic": settings.ANTHROPIC_API_KEY,
        "openai": settings.OPENAI_API_KEY,
        "openrouter": settings.OPENROUTER_API_KEY,
        "gemini": settings.GEMINI_API_KEY,
        "nvidia": settings.NVIDIA_API_KEY,
    }.get(backend, "")


def backend_options(settings: Settings, backend: str) -> dict[str, Any]:
    """Deployment-level construction options that are neither key nor model.

    Only NVIDIA has one today: a self-hosted NIM container speaks the same
    protocol as the hosted catalogue at a different address, so its base URL is
    configuration rather than a constant. An empty setting leaves the adapter on
    its own default.
    """
    if backend == "nvidia" and settings.NVIDIA_BASE_URL:
        return {"base_url": settings.NVIDIA_BASE_URL}
    if backend == "anthropic":
        return {"prompt_caching": settings.LLM_PROMPT_CACHING}
    return {}


def build_from_settings(settings: Settings, model: str, **options: Any) -> LLMProvider:
    """Managed tier provider, using the platform's own key."""
    backend = settings.LLM_BACKEND
    return build_provider(
        backend,
        platform_key(settings, backend),
        model,
        **backend_options(settings, backend),
        **options,
    )


def build_chain(specs: list[tuple[str, str, str]], **options: Any) -> LLMProvider:
    """Build a fallback chain from (backend, api_key, model) triples.

    Order matters: cheapest capable provider first, so degradation costs more
    rather than less. Crossing vendors is the point — a second model inside one
    provider fails together with the first during that provider's outage.
    """
    providers = [build_provider(backend, key, model, **options) for backend, key, model in specs]
    if len(providers) == 1:
        return providers[0]
    return FallbackLLM(providers)
