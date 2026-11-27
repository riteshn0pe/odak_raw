"""Render-at-invocation: tenant facts into agent prompt templates.

The layer between the registry (what an agent *is*) and the services that run
agents. Three responsibilities:

1. **Facts** — :meth:`AgentPromptService.facts_for` reads the tenant's growth
   settings and users into a :class:`~app.services.agents.facts.TenantFacts`,
   the value set every ``{{hole}}`` resolves against.
2. **Prompts** — :meth:`AgentPromptService.system_for` takes the agent's stored
   template (its ``agents`` row, editable from the dashboard exactly like a
   template-added chat agent) and renders it with those facts at invocation.
   Nothing is baked at crawl time; a re-crawl or a profile edit changes every
   agent's behaviour on the next render with no prompt rewrite.
3. **Activation** — :meth:`AgentPromptService.state_for` computes each agent's
   state from the fact base (planned / disabled / not_applicable / blocked /
   active), and :meth:`AgentPromptService.gate` is what the job queue asks
   before running gated work.

Every method degrades to the registry's own template when the tenant has no
row yet — the rows are materialized lazily by :meth:`ensure_rows` on first
dashboard read, and a job that runs before that must not fail for want of a
card.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.core.db import tenant_scope
from app.core.logging import get_logger
from app.services.agents import defaults
from app.services.agents.facts import TenantFacts, build_company_namespace
from app.services.agents.registry import AGENTS, BY_KEY, SWARM_SPECS, AgentSpec
from app.services.agents.render import MissingFacts, render, render_preview

logger = get_logger(__name__)

# Agent states, worst to best. The dashboard badge and the job gate both read
# these exact strings; the dashboard renders anything unknown as "blocked"
# rather than crashing, but the gate treats only "active" as a go.
STATE_PLANNED = "planned"
STATE_DISABLED = "disabled"
STATE_NOT_APPLICABLE = "not_applicable"
STATE_BLOCKED = "blocked"
STATE_ACTIVE = "active"

# The D2C tenant type (profile.py ProfileBlock.tenant_type): outbound agents
# have no strangers to write to for a pure store, so they show as not
# applicable rather than blocked-for-a-reason-they-cannot-fix. Only an
# explicit "B" gates — "" (owner has not chosen) stays applicable and each
# agent is judged on its facts alone.
D2C_TYPE = "B"

# Facts cache TTL. Short on purpose: facts_version in the cache *value* lets
# deps.load_agent_config key on it, but this cache itself is only a per-request
# dedupe across the handful of agents one job run touches — a minute of
# staleness after a profile edit is the same window the settings screen has.
FACTS_TTL_SECONDS = 60


@dataclass(slots=True, frozen=True)
class AgentState:
    """One agent's activation state, with the reason a human can act on."""

    key: str
    state: str
    reason: str = ""
    missing: tuple[str, ...] = ()


def compute_state(
    spec: AgentSpec,
    facts: TenantFacts,
    *,
    enabled: bool = True,
) -> AgentState:
    """The activation state machine (agent.md §9). Pure, so tests need no DB.

    Order matters and is the priority the dashboard shows: a planned agent is
    planned no matter what; a disabled agent is off even when its facts are
    complete (the owner said stop); not_applicable beats blocked because a D2C
    tenant seeing "blocked" on the cold-mail agent would try to fix a fact that
    is not the problem.
    """
    if spec.status == "planned":
        return AgentState(key=spec.key, state=STATE_PLANNED, reason="Not built yet")
    if not enabled:
        return AgentState(key=spec.key, state=STATE_DISABLED, reason="Turned off by the owner")
    tenant_type = str(facts.company.get("tenant_type") or "").strip()
    if spec.outbound_only and tenant_type == D2C_TYPE:
        return AgentState(
            key=spec.key,
            state=STATE_NOT_APPLICABLE,
            reason="No outbound for a store-only business",
        )
    missing = tuple(facts.missing(spec.requires))
    if missing:
        return AgentState(
            key=spec.key,
            state=STATE_BLOCKED,
            reason="Waiting on the business profile: " + ", ".join(missing),
            missing=missing,
        )
    return AgentState(key=spec.key, state=STATE_ACTIVE)


class AgentPromptService:
    """Facts, rendered prompts, and activation states for one tenant's swarm."""

    def __init__(self, container: Any) -> None:
        self._container = container

    # ----------------------------------------------------------------- facts

    async def facts_for(self, tenant_id: UUID) -> TenantFacts:
        """The tenant's fact base: profile blocks plus the owner's identity.

        ``owner`` is the tenant's owner-role user — the person the swarm's mail
        signs as and escalations reach. A tenant with no owner row (should be
        impossible; memberships cascade with the tenant) simply renders
        ``{{owner.name}}`` as its default, never as a blank.
        """
        cache_key = f"agentfacts:{tenant_id}"
        cached = await self._cache_get_json(cache_key)
        if cached is not None and "company" in cached and "owner" in cached:
            return TenantFacts(
                tenant_id=tenant_id,
                company=cached["company"],
                owner=cached["owner"],
                version=int(cached.get("version") or 0),
            )

        settings = await self._container.growth_profile.get(tenant_id)
        company = build_company_namespace(settings)
        owner = await self._owner(tenant_id)
        profile = settings.get("profile") or {}
        facts = TenantFacts(
            tenant_id=tenant_id,
            company=company,
            owner=owner,
            version=int(profile.get("facts_version") or 0),
        )

        await self._cache_set_json(
            cache_key,
            {"company": company, "owner": owner, "version": facts.version},
            ttl=FACTS_TTL_SECONDS,
        )
        return facts

    async def _owner(self, tenant_id: UUID) -> dict[str, Any]:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT u.name, u.email
                          FROM memberships m
                          JOIN users u ON u.id = m.user_id
                         WHERE m.tenant_id = :tid AND m.role = 'owner'
                         ORDER BY m.created_at
                         LIMIT 1
                        """
                    ),
                    {"tid": tenant_id},
                )
            ).mappings().first()
        if row is None:
            return {}
        return {"name": str(row["name"] or ""), "email": str(row["email"] or "")}

    # ---------------------------------------------------------------- prompts

    async def system_for(
        self,
        tenant_id: UUID,
        agent_key: str,
        *,
        runtime: dict[str, Any] | None = None,
    ) -> str:
        """The agent's system prompt, rendered from the tenant's facts now.

        The template comes from the tenant's ``agents`` row when one exists
        (what the owner edited) and from the registry otherwise, so a job that
        runs before the dashboard has ever been opened behaves identically to
        one that runs after. Raises :class:`MissingFacts` when a required hole
        has no value — callers that cannot tolerate that (the chat path) use
        :meth:`business_context`, whose holes all carry defaults.
        """
        spec = BY_KEY.get(agent_key)
        if spec is None:
            raise KeyError(f"unknown agent key {agent_key!r}")
        template = await self.template_for(tenant_id, spec)
        facts = await self.facts_for(tenant_id)
        return render(spec.prompt if template is None else template, facts.namespaces, runtime=runtime)

    async def template_for(self, tenant_id: UUID, spec: AgentSpec) -> str | None:
        """The stored template for one agent, or None when no row exists yet.

        Returns the raw template *with* its holes — rendering is a separate
        step so the dashboard's preview pane can show both what the owner
        wrote and what it renders to.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text("SELECT config -> 'system_prompt' AS prompt FROM agents WHERE agent_key = :key"),
                    {"key": spec.key},
                )
            ).mappings().first()
        if row is None or not str(row["prompt"] or "").strip():
            return None
        return str(row["prompt"])

    async def preview(
        self,
        tenant_id: UUID,
        agent_key: str,
        *,
        runtime: dict[str, Any] | None = None,
    ) -> tuple[str, list[str]]:
        """Render for the owner's eyes: never raises, names every gap."""
        spec = BY_KEY.get(agent_key)
        if spec is None:
            raise KeyError(f"unknown agent key {agent_key!r}")
        template = await self.template_for(tenant_id, spec)
        facts = await self.facts_for(tenant_id)
        return render_preview(
            spec.prompt if template is None else template,
            facts.namespaces,
            runtime=runtime,
        )

    async def business_context(
        self,
        tenant_id: UUID,
        *,
        runtime: dict[str, Any] | None = None,
    ) -> str:
        """The tenant-facts block appended to every customer-facing chat agent.

        The support path's only bridge to the fact base: chat agents are the
        tenant's own rows (persona authored from dashboard templates), so
        rather than rewrite their persona the runner appends this block after
        it. Every hole in ``BUSINESS_CONTEXT_BLOCK`` carries an inline default,
        so a tenant whose profile is still empty gets coherent behaviour
        ("answer from the documents, never guess a price") rather than a
        MissingFacts crash mid-conversation.
        """
        facts = await self.facts_for(tenant_id)
        try:
            return render(defaults.BUSINESS_CONTEXT_BLOCK, facts.namespaces, runtime=runtime)
        except MissingFacts:
            # unreachable while every hole in the block keeps a default; the
            # catch is here so a future default-less hole degrades to the
            # preview's ‹gap› markers instead of killing the chat request
            rendered, _ = render_preview(
                defaults.BUSINESS_CONTEXT_BLOCK, facts.namespaces, runtime=runtime
            )
            return rendered

    # ------------------------------------------------------------- activation

    async def state_for(self, tenant_id: UUID) -> dict[str, AgentState]:
        """Every registered agent's state for this tenant, keyed by agent key.

        Includes the chat-kind ``support`` spec — the graph and the A2A card
        describe it too — even though it has no swarm row and is never gated.
        """
        facts = await self.facts_for(tenant_id)
        enabled = await self._enabled_map(tenant_id)
        return {
            spec.key: compute_state(spec, facts, enabled=enabled.get(spec.key, True))
            for spec in AGENTS
        }

    async def gate(self, tenant_id: UUID, agent_key: str) -> tuple[bool, str]:
        """May gated work run for this agent right now?

        The job queue's only question. Returns ``(allowed, reason)``; a refused
        job is skipped with ``{"status": "skipped", "reason": reason}``` rather
        than failing — blocked is a state, not an error.
        """
        spec = BY_KEY.get(agent_key)
        if spec is None:
            return False, f"unknown agent key {agent_key!r}"
        facts = await self.facts_for(tenant_id)
        enabled = await self._enabled_map(tenant_id)
        state = compute_state(spec, facts, enabled=enabled.get(spec.key, True))
        if state.state == STATE_ACTIVE:
            return True, ""
        return False, f"{spec.name}: {state.state.replace('_', ' ')} — {state.reason}"

    async def _enabled_map(self, tenant_id: UUID) -> dict[str, bool]:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text("SELECT agent_key, enabled FROM agents WHERE agent_key IS NOT NULL")
                )
            ).mappings().all()
        return {str(row["agent_key"]): bool(row["enabled"]) for row in rows}

    # ------------------------------------------------------------- materialize

    async def ensure_rows(self, tenant_id: UUID) -> None:
        """Materialize the swarm's ``agents`` rows: the dashboard cards.

        One ``INSERT ... ON CONFLICT DO NOTHING`` per spec, so the registry's
        template text seeds the row exactly once and everything after that is
        the owner's edit. Swarm rows are never the tenant's default chat agent
        and carry no channels; they are cards and prompt storage, and their
        ``kind`` is what stops the delete guard from counting them as the
        tenant's only agent.
        """
        async with tenant_scope(tenant_id) as session:
            for spec in SWARM_SPECS:
                await session.execute(
                    text(
                        """
                        INSERT INTO agents (tenant_id, name, config, kind, agent_key, enabled)
                        VALUES (:tenant_id, :name, CAST(:config AS jsonb), 'swarm', :agent_key, true)
                        ON CONFLICT (tenant_id, agent_key) WHERE agent_key IS NOT NULL
                        DO NOTHING
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "name": spec.name,
                        "agent_key": spec.key,
                        "config": json.dumps(
                            {
                                "system_prompt": spec.prompt,
                                "widget": {"theme": {"template": f"swarm:{spec.key}"}},
                            }
                        ),
                    },
                )
        # activation stamp for the first tenant whose facts are already
        # complete the moment the rows appear (a tenant that onboarded before
        # this table existed)
        await self._stamp_activation(tenant_id)

    async def _stamp_activation(self, tenant_id: UUID) -> None:
        """Record when each active agent first became active, once."""
        states = await self.state_for(tenant_id)
        active_keys = [
            key for key, state in states.items() if state.state == STATE_ACTIVE
        ]
        if not active_keys:
            return
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    """
                    UPDATE agents
                       SET activated_at = COALESCE(activated_at, :now)
                     WHERE agent_key = ANY(:keys)
                       AND activated_at IS NULL
                    """
                ),
                {"now": datetime.now(timezone.utc), "keys": active_keys},
            )

    # ---------------------------------------------------------------- private

    async def invalidate_facts(self, tenant_id: UUID) -> None:
        """Drop the cached fact base. Called on every profile write.

        The TTL would catch up within a minute; this is what makes an owner's
        edit visible to the very next render instead.
        """
        try:
            await self._container.cache.delete(f"agentfacts:{tenant_id}")
        except Exception:
            logger.debug("agent_facts_cache_delete_failed", exc_info=True)

    async def _cache_get_json(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._container.cache.get(key)
        except Exception:
            # a cache outage degrades to reading settings every time — one
            # indexed lookup — never to failing the render
            logger.debug("agent_facts_cache_read_failed", exc_info=True)
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def _cache_set_json(self, key: str, value: dict[str, Any], *, ttl: int) -> None:
        try:
            await self._container.cache.set(key, json.dumps(value), ttl_seconds=ttl)
        except Exception:
            logger.debug("agent_facts_cache_write_failed", exc_info=True)
