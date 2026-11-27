"""Per-agent LLM routing: which backend and model each agent actually runs on.

Staff-only. A tenant never sees this. Per-tenant model choice across a dozen
agents would be a dozen ways for a tenant to make their own product worse; the
override exists for support, enterprise deals and debugging, which is why it
lives behind the admin API.

Resolution order, and nothing may skip a step:

    tenant override row  ->  platform default row  ->  container.llm / utility_llm

The last step is today's behaviour, so a deployment that never touches this table
behaves exactly as it did before the table existed. That is the property that
makes this safe to land in Stage 1 and wire every later agent through.

**Every growth agent takes its LLM from ``llm_for``, never from ``container.llm``
directly.** This is the one sanctioned indirection in the codebase, and it is
what makes the admin agent graph a control surface rather than a picture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.services.agents.registry import AGENT_KEYS as _REGISTRY_KEYS

logger = get_logger(__name__)

# Every agent that can be routed, with the human label the admin graph shows.
# Derived from the registry (app/services/agents/registry.py) — the registry is
# the single registration place, so a new agent key appears here the moment it
# is defined and the three-copy drift agent.md §3.2 closes is closed here too.
# The overrides below are labels that carry more meaning on the admin graph
# than the registry's card names: `support` is the existing RAG agent (one row
# here so the graph shows the whole system), `builtin_things` reads better as
# what it does on a routing screen than the card's "Capability provider", and
# `growth_profile` is the routing name the admin screen has always shown for
# the whoami agent. The key *set* is exactly the registry's — orchestrator and
# growth_followup included — which is what `_check` and `effective` route on.
_LABEL_OVERRIDES: dict[str, str] = {
    "support": "Support (RAG)",
    "builtin_things": "Builtin things",
    "growth_profile": "Profile",
}

AGENT_KEYS: dict[str, str] = {
    key: _LABEL_OVERRIDES.get(key, name) for key, name in _REGISTRY_KEYS.items()
}

ROLES: tuple[str, ...] = ("primary", "utility")

# Mirrors the Literal on Settings.LLM_BACKEND and the CHECK constraint in
# migration 016. Three copies of one list is two too many, but the database and
# the settings type each have to stand alone, so this one exists to fail an
# admin write early with a readable message rather than an IntegrityError.
BACKENDS: tuple[str, ...] = ("gemini", "openai", "anthropic", "openrouter", "nvidia")


@dataclass(slots=True, frozen=True)
class Resolved:
    """Which model an agent is on, and where that decision came from.

    ``origin`` is what the admin screen displays beside the model — "inherited"
    reads very differently from "set for this tenant" when someone is debugging
    why one customer's mails read oddly.
    """

    backend: str
    model: str
    origin: str  # "tenant" | "platform" | "fallback"


class ModelConfigService:
    """Resolves and caches per-agent model routing."""

    # Cache TTL. Config changes only when a staff member clicks, and every write
    # invalidates explicitly — the TTL is the backstop for a second process that
    # did not see the invalidation, not the primary mechanism.
    CACHE_TTL_SECONDS = 300

    def __init__(self, container: Any) -> None:
        self._container = container

    # ------------------------------------------------------------------ reads

    async def resolve(self, tenant_id: UUID, agent_key: str, role: str) -> Resolved:
        """Which backend and model apply, without building a provider."""
        self._check(agent_key, role)

        cache_key = f"agentmodel:{tenant_id}:{agent_key}:{role}"
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached

        row = await self._container.growth.models.resolve(tenant_id, agent_key, role)
        if row is None:
            resolved = self._fallback(role)
        else:
            resolved = Resolved(
                backend=str(row["backend"]),
                model=str(row["model"]),
                origin="tenant" if row["tenant_id"] is not None else "platform",
            )

        await self._cache_set(cache_key, resolved)
        return resolved

    async def llm_for(self, tenant_id: UUID, agent_key: str, role: str = "primary") -> Any:
        """The provider this agent should use. The only door growth agents use.

        Falls back to the container's own provider on any construction failure —
        a missing platform key for the chosen backend, a model name the vendor
        rejected — because an agent that cannot start at all is a worse outcome
        than an agent running on the deployment default. The failure is logged
        with the agent key so the admin screen's owner can see what they broke.
        """
        resolved = await self.resolve(tenant_id, agent_key, role)
        if resolved.origin == "fallback":
            return self._container_llm(role)

        try:
            from app.adapters.llm.factory import backend_options, build_provider, platform_key

            settings = self._container.settings
            return build_provider(
                resolved.backend,
                platform_key(settings, resolved.backend),
                resolved.model,
                **backend_options(settings, resolved.backend),
            )
        except Exception:
            logger.warning(
                "agent_model_build_failed",
                tenant_id=str(tenant_id),
                agent_key=agent_key,
                role=role,
                backend=resolved.backend,
                model=resolved.model,
                exc_info=True,
            )
            return self._container_llm(role)

    async def effective(self, tenant_id: UUID | None) -> list[dict[str, Any]]:
        """Every agent and role, with what currently applies.

        Always the full grid — ten agents times two roles — including the ones
        nobody has configured, because the admin graph draws a node per agent and
        a node missing from the response would be a node missing from the graph.
        """
        rows = await self._container.growth.models.effective(tenant_id)
        by_key: dict[tuple[str, str, bool], dict[str, Any]] = {}
        for row in rows:
            key = (str(row["agent_key"]), str(row["role"]), row["tenant_id"] is not None)
            by_key[key] = row

        out: list[dict[str, Any]] = []
        for agent_key, label in AGENT_KEYS.items():
            for role in ROLES:
                platform_row = by_key.get((agent_key, role, False))
                tenant_row = (
                    by_key.get((agent_key, role, True)) if tenant_id is not None else None
                )
                chosen = tenant_row or platform_row
                fallback = self._fallback(role)
                out.append(
                    {
                        "agent_key": agent_key,
                        "label": label,
                        "role": role,
                        "backend": str(chosen["backend"]) if chosen else fallback.backend,
                        "model": str(chosen["model"]) if chosen else fallback.model,
                        "origin": (
                            "tenant"
                            if tenant_row
                            else "platform"
                            if platform_row
                            else "fallback"
                        ),
                        "platform_backend": (
                            str(platform_row["backend"]) if platform_row else None
                        ),
                        "platform_model": str(platform_row["model"]) if platform_row else None,
                        "tenant_backend": str(tenant_row["backend"]) if tenant_row else None,
                        "tenant_model": str(tenant_row["model"]) if tenant_row else None,
                        "updated_by": str(chosen["updated_by"]) if chosen else "",
                        "updated_at": chosen["updated_at"] if chosen else None,
                    }
                )
        return out

    # ----------------------------------------------------------------- writes

    async def put(
        self,
        tenant_id: UUID | None,
        *,
        agent_key: str,
        role: str,
        backend: str,
        model: str,
        updated_by: str,
    ) -> dict[str, Any]:
        self._check(agent_key, role)
        if backend not in BACKENDS:
            raise ValidationError(f"backend must be one of {list(BACKENDS)}")
        if not model or not model.strip():
            raise ValidationError("model is required")

        row: dict[str, Any] = await self._container.growth.models.put(
            tenant_id,
            agent_key=agent_key,
            role=role,
            backend=backend,
            model=model.strip(),
            updated_by=updated_by,
        )
        await self._invalidate(tenant_id, agent_key, role)
        logger.info(
            "agent_model_set",
            tenant_id=str(tenant_id) if tenant_id else None,
            agent_key=agent_key,
            role=role,
            backend=backend,
            model=model,
        )
        return row

    async def delete(self, tenant_id: UUID | None, *, agent_key: str, role: str) -> int:
        self._check(agent_key, role)
        removed: int = await self._container.growth.models.delete(
            tenant_id, agent_key=agent_key, role=role
        )
        await self._invalidate(tenant_id, agent_key, role)
        return removed

    # ---------------------------------------------------------------- private

    def _check(self, agent_key: str, role: str) -> None:
        if agent_key not in AGENT_KEYS:
            raise ValidationError(f"unknown agent_key {agent_key!r}")
        if role not in ROLES:
            raise ValidationError(f"role must be one of {list(ROLES)}")

    def _container_llm(self, role: str) -> Any:
        return (
            self._container.utility_llm if role == "utility" else self._container.llm
        )

    def _fallback(self, role: str) -> Resolved:
        """What the container would use, described without building it.

        Reads the settings rather than the provider object so that ``effective``
        can report the fallback for every unconfigured agent without constructing
        nine providers to ask them their name.
        """
        settings = self._container.settings
        model = settings.DEFAULT_LLM_MODEL
        if role == "utility" and settings.UTILITY_LLM_MODEL:
            model = settings.UTILITY_LLM_MODEL
        return Resolved(backend=settings.LLM_BACKEND, model=model, origin="fallback")

    async def _cache_get(self, key: str) -> Resolved | None:
        try:
            raw = await self._container.cache.get(key)
        except Exception:
            # a cache outage degrades to reading the row every time, which is one
            # indexed lookup — never to failing the agent
            logger.debug("agent_model_cache_read_failed", exc_info=True)
            return None
        if not raw:
            return None
        try:
            import json

            data = json.loads(raw)
            return Resolved(
                backend=str(data["backend"]),
                model=str(data["model"]),
                origin=str(data["origin"]),
            )
        except Exception:
            return None

    async def _cache_set(self, key: str, resolved: Resolved) -> None:
        try:
            import json

            await self._container.cache.set(
                key,
                json.dumps(
                    {
                        "backend": resolved.backend,
                        "model": resolved.model,
                        "origin": resolved.origin,
                    }
                ),
                ttl_seconds=self.CACHE_TTL_SECONDS,
            )
        except Exception:
            logger.debug("agent_model_cache_write_failed", exc_info=True)

    async def _invalidate(
        self, tenant_id: UUID | None, agent_key: str, role: str
    ) -> None:
        """Drop the cached resolution after a write.

        A platform-default change affects every tenant, and there is no key to
        enumerate them by — so the platform write bumps a generation counter that
        is *not* implemented here, and instead relies on the 5-minute TTL. That
        is a deliberate trade: a staff model change taking up to five minutes to
        reach every running process is acceptable, and a fan-out invalidation
        across every tenant key is not worth building for a dropdown.
        """
        if tenant_id is None:
            return
        try:
            await self._container.cache.delete(
                f"agentmodel:{tenant_id}:{agent_key}:{role}"
            )
        except Exception:
            logger.debug("agent_model_cache_invalidate_failed", exc_info=True)
