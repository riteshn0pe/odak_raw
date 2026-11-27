"""Tenant-managed agent CRUD: the deployable unit of chat behaviour.

An agent bundles persona, model, retrieval, tool, guardrail, context, and
channel settings into one versioned JSONB blob. Reads are open to any
authenticated caller (chat flows need to resolve an agent by id),
but mutations are owner/admin only since they change what every channel says
and does. Writes bump `version` and explicitly evict app.api.deps' per-request
cache so the next chat request sees the change without waiting out the TTL.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.api.deps import ContainerDep, TenantDep, invalidate_agent_config, require_role
from app.core.db import tenant_scope
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.domain.agent_config import AgentConfig
from app.domain.entities import TenantContext
from app.services import audit
from app.services.agents.registry import BY_KEY
from app.services.agents.render import render_preview

logger = get_logger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


async def _record(
    session: Any,
    tenant: TenantContext,
    action: str,
    *,
    target_id: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Write an audit row in the mutation's own transaction.

    The acting user's email is resolved from the same session so the tenant's
    trail names who created, edited, or deleted an agent.
    """
    actor = (
        await session.execute(
            text("SELECT email FROM users WHERE id = :id"),
            {"id": tenant.user_id},
        )
    ).mappings().first()
    await audit.record(
        action,
        actor_type="user",
        actor_id=tenant.user_id,
        actor_email=actor["email"] if actor else None,
        target_tenant=tenant.tenant_id,
        target_type="agent",
        target_id=target_id,
        detail=detail,
        session=session,
    )


class AgentSummary(BaseModel):
    id: UUID
    name: str
    is_default: bool
    version: int
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Card aggregates the dashboard's agents index renders per row: how much the
    # agent knows, how busy it has been this week, where it is reachable, and
    # which starter template it came from. All four default, so a row assembled
    # without the aggregate columns still serialises rather than 500ing.
    source_count: int = 0
    conversations_7d: int = 0
    channels: list[str] = Field(default_factory=list)
    template: str | None = None
    # The registry columns (migration 026). `agent_key` links a swarm row to
    # its registry entry; chat rows carry None. `state`/`state_reason` are the
    # live activation state computed from the tenant's facts — None when the
    # row has no registry entry (a chat agent's state is its channels, not its
    # facts) or when the state could not be resolved (never a 500).
    agent_key: str | None = None
    kind: str = "chat"
    enabled: bool = True
    state: str | None = None
    state_reason: str = ""
    activated_at: datetime | None = None


class AgentDetail(AgentSummary):
    config: AgentConfig


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str
    config: dict[str, Any] | None = None
    is_default: bool = False


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = None
    config: dict[str, Any] | None = None
    is_default: bool | None = None


class AgentPreviewRequest(BaseModel):
    """The Instructions editor's live preview pane posts the runtime values the
    template references (language, today, …) and, while editing, the draft
    prompt itself. All optional: a preview with no runtime renders against the
    facts alone, and one without a template renders what is stored.

    The draft override is the point of a preview pane — the owner wants to see
    what the unsaved text becomes, not what the last saved version said. Both
    swarm and chat rows accept it: a swarm template carries the ``{{company.*}}``
    holes, a chat persona may carry them too, and the preview renderer treats
    them identically."""

    model_config = ConfigDict(extra="forbid")

    runtime: dict[str, Any] = Field(default_factory=dict)
    template: str | None = Field(default=None, max_length=100_000)


class AgentPreviewOut(BaseModel):
    template: str
    rendered: str
    missing: list[str] = Field(default_factory=list)


class AgentEnabledUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


def _parse_config(raw: Any) -> AgentConfig:
    """Validate stored JSONB, tolerating an empty or absent config."""
    if not raw:
        return AgentConfig.default()
    return AgentConfig.model_validate(raw)


async def _check_escalation_destinations(tenant_id: UUID, raw: dict[str, Any] | None) -> None:
    """Hold `escalation.destination_emails` to the same rule the email screen does.

    `EscalationConfig` documents this restriction and says it is enforced at the
    API rather than in the model, because the model has no database access. It was
    only ever enforced on the email-escalation endpoint, so a config written
    through this router reached the column unchecked — and the dashboard's
    escalation panel writes exactly that way. An unrestricted destination list is
    an exfiltration primitive: an escalation carries the visitor's question, the
    answer and their contact details.

    Runs before the write transaction opens, deliberately. The check reads the
    member and verified-domain tables in its own `tenant_scope`, and nesting that
    inside a scope already holding `SELECT ... FOR UPDATE` on the agent row would
    take a second connection while blocking on the first.

    Mutates `raw` in place, replacing the addresses with their normalised form, so
    both write paths store what the email screen stores. Raises `ValidationError`
    when an address is neither a team member nor on a verified sending domain.
    """
    if not isinstance(raw, dict):
        return
    escalation = raw.get("escalation")
    if not isinstance(escalation, dict):
        return
    addresses = escalation.get("destination_emails")
    if not isinstance(addresses, list) or not addresses:
        return

    from app.api.v1.integrations_email import _assert_destinations_allowed

    escalation["destination_emails"] = await _assert_destinations_allowed(
        tenant_id, [str(value) for value in addresses]
    )


# The agents index renders four aggregates on every card. They are correlated
# scalar subqueries rather than a second round trip because an N+1 here lands on
# the product's busiest screen: the list endpoint stays exactly one statement no
# matter how many agents a tenant owns. Every table read below is RLS-scoped the
# same way `agents` is, so the surrounding `tenant_scope` isolates them and no
# explicit tenant_id predicate is needed (nor wanted — adding one here would
# diverge from every other query in this router).
#
# `_LIST_SQL` and `_DETAIL_SQL` carry the same aggregate expressions and must be
# kept in step; they are spelled out rather than composed so the SQL stays a
# plain literal. `_DETAIL_SQL` is the single detail projection used by the read,
# create and update handlers alike.
_LIST_SQL = """
    SELECT a.id, a.name, a.is_default, a.version, a.created_at, a.updated_at,
           a.agent_key, a.kind, a.enabled, a.activated_at,
           (SELECT count(*) FROM sources s
             WHERE s.agent_id = a.id
               -- mirrors app.api.v1.sources.NOT_DELETED: a source is soft
               -- deleted by a flag inside its own config blob, so the agent
               -- card and the sources screen cannot report different totals
               AND NOT COALESCE((s.config->>'deleted')::boolean, false)
           ) AS source_count,
           (SELECT count(*) FROM conversations c
             WHERE c.agent_id = a.id
               AND c.started_at >= now() - interval '7 days'
           ) AS conversations_7d,
           -- one entry per channel actually wired, using the same liveness
           -- tests delete_agent and the email channel screen apply
           COALESCE(array_remove(ARRAY[
               (SELECT 'whatsapp'::text FROM whatsapp_integrations wi
                 WHERE wi.agent_id = a.id AND wi.status <> 'disconnected' LIMIT 1),
               (SELECT 'email'::text FROM email_channels ec
                 WHERE ec.agent_id = a.id AND ec.status <> 'disconnected' LIMIT 1)
           ], NULL), ARRAY[]::text[]) AS channels,
           -- the starter template an agent was created from is stored in the
           -- widget theme blob (WidgetConfig.theme is the one untyped dict in
           -- AgentConfig, which is why the value survives a config round trip)
           CASE
               WHEN jsonb_typeof(a.config -> 'widget' -> 'theme' -> 'template') = 'string'
               THEN a.config -> 'widget' -> 'theme' ->> 'template'
           END AS template
      FROM agents a
     ORDER BY a.created_at
"""

_DETAIL_SQL = """
    SELECT a.id, a.name, a.config, a.is_default, a.version, a.created_at, a.updated_at,
           a.agent_key, a.kind, a.enabled, a.activated_at,
           (SELECT count(*) FROM sources s
             WHERE s.agent_id = a.id
               AND NOT COALESCE((s.config->>'deleted')::boolean, false)
           ) AS source_count,
           (SELECT count(*) FROM conversations c
             WHERE c.agent_id = a.id
               AND c.started_at >= now() - interval '7 days'
           ) AS conversations_7d,
           COALESCE(array_remove(ARRAY[
               (SELECT 'whatsapp'::text FROM whatsapp_integrations wi
                 WHERE wi.agent_id = a.id AND wi.status <> 'disconnected' LIMIT 1),
               (SELECT 'email'::text FROM email_channels ec
                 WHERE ec.agent_id = a.id AND ec.status <> 'disconnected' LIMIT 1)
           ], NULL), ARRAY[]::text[]) AS channels,
           CASE
               WHEN jsonb_typeof(a.config -> 'widget' -> 'theme' -> 'template') = 'string'
               THEN a.config -> 'widget' -> 'theme' ->> 'template'
           END AS template
      FROM agents a
     WHERE a.id = :id
"""


def _count(raw: Any) -> int:
    """Coerce an aggregate cell to a non-negative count, defaulting to zero."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _channels(raw: Any) -> list[str]:
    """Normalise the channel array, tolerating NULL or a driver-specific type."""
    if not isinstance(raw, list):
        return []
    return [str(value) for value in raw if value is not None]


def _template(raw: Any) -> str | None:
    """Only a string is a template id; anything else reads as no template."""
    return raw if isinstance(raw, str) else None


def _agent_key(raw: Any) -> str | None:
    """A swarm row's registry key; chat rows (and odd values) read as None."""
    return raw if isinstance(raw, str) and raw else None


async def _agent_states(
    container: Any, tenant_id: UUID
) -> dict[str, tuple[str, str]]:
    """Every registry agent's live (state, reason) for this tenant.

    Fail-open: an activation state is decoration on a card, never a reason to
    fail the request — a facts read that cannot be served yields an empty map
    and the cards render without the badge rather than 500ing.
    """
    try:
        states = await container.agent_prompts.state_for(tenant_id)
    except Exception:
        logger.warning("agent_states_unavailable", tenant_id=str(tenant_id), exc_info=True)
        return {}
    return {
        key: (str(state.state), str(state.reason)) for key, state in states.items()
    }


def _state_fields(
    row: Any, states: dict[str, tuple[str, str]] | None
) -> dict[str, Any]:
    """The activation columns for one row, from the states map by agent_key.

    A row with no registry key (a chat agent) gets no state: its liveness is
    its channels, which the `channels` aggregate already reports.
    """
    key = _agent_key(row["agent_key"])
    if key is None or not states:
        return {"state": None, "state_reason": ""}
    state, reason = states.get(key, (None, ""))
    return {"state": state, "state_reason": reason}


def _to_summary(
    row: Any, states: dict[str, tuple[str, str]] | None = None
) -> AgentSummary:
    return AgentSummary(
        id=row["id"],
        name=row["name"],
        is_default=row["is_default"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        source_count=_count(row["source_count"]),
        conversations_7d=_count(row["conversations_7d"]),
        channels=_channels(row["channels"]),
        template=_template(row["template"]),
        agent_key=_agent_key(row["agent_key"]),
        kind=str(row["kind"] or "chat"),
        enabled=bool(row["enabled"]),
        activated_at=row["activated_at"],
        **_state_fields(row, states),
    )


def _to_detail(
    row: Any, states: dict[str, tuple[str, str]] | None = None
) -> AgentDetail:
    return AgentDetail(
        id=row["id"],
        name=row["name"],
        is_default=row["is_default"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        config=_parse_config(row["config"]),
        source_count=_count(row["source_count"]),
        conversations_7d=_count(row["conversations_7d"]),
        channels=_channels(row["channels"]),
        template=_template(row["template"]),
        agent_key=_agent_key(row["agent_key"]),
        kind=str(row["kind"] or "chat"),
        enabled=bool(row["enabled"]),
        activated_at=row["activated_at"],
        **_state_fields(row, states),
    )


@router.get("", response_model=list[AgentSummary])
async def list_agents(
    tenant: TenantDep, container: ContainerDep
) -> list[AgentSummary]:
    # Materialize the swarm's rows on first dashboard load so the fourteen
    # swarm cards exist the moment the owner opens the page (the chat-kind
    # `support` is deliberately not materialized — its persona is the tenant's
    # own agent row). Fail-open: a tenant whose rows cannot be created still
    # sees the agents it already has rather than an error page.
    try:
        await container.agent_prompts.ensure_rows(tenant.tenant_id)
    except Exception:
        logger.warning(
            "agent_rows_materialize_failed", tenant_id=str(tenant.tenant_id), exc_info=True
        )
    async with tenant_scope(tenant.tenant_id) as session:
        rows = (await session.execute(text(_LIST_SQL))).mappings().all()
    states = await _agent_states(container, tenant.tenant_id)
    return [_to_summary(row, states) for row in rows]


@router.get("/{agent_id}", response_model=AgentDetail)
async def get_agent(
    agent_id: UUID, tenant: TenantDep, container: ContainerDep
) -> AgentDetail:
    async with tenant_scope(tenant.tenant_id) as session:
        row = (
            await session.execute(text(_DETAIL_SQL), {"id": agent_id})
        ).mappings().first()
    if row is None:
        raise NotFoundError("no such agent")
    states = await _agent_states(container, tenant.tenant_id)
    return _to_detail(row, states)


@router.post(
    "",
    response_model=AgentDetail,
    status_code=201,
    dependencies=[require_role("owner", "admin")],
)
async def create_agent(payload: AgentCreate, tenant: TenantDep) -> AgentDetail:
    await _check_escalation_destinations(tenant.tenant_id, payload.config)
    config = AgentConfig.model_validate(payload.config) if payload.config else AgentConfig.default()
    agent_id = uuid4()
    async with tenant_scope(tenant.tenant_id) as session:
        if payload.is_default:
            # clear any existing default first so the partial unique index
            # (one default per tenant) is never briefly violated
            await session.execute(
                text("UPDATE agents SET is_default = false WHERE is_default = true")
            )
        await session.execute(
            text(
                """
                INSERT INTO agents (id, tenant_id, name, config, version, is_default)
                VALUES (:id, :tenant_id, :name, CAST(:config AS jsonb), :version, :is_default)
                """
            ),
            {
                "id": agent_id,
                "tenant_id": tenant.tenant_id,
                "name": payload.name,
                "config": json.dumps(config.model_dump(mode="json")),
                "version": config.version,
                "is_default": payload.is_default,
            },
        )
        row = (
            await session.execute(text(_DETAIL_SQL), {"id": agent_id})
        ).mappings().one()

        await _record(
            session,
            tenant,
            "agent.created",
            target_id=str(agent_id),
            detail={"name": row["name"], "is_default": row["is_default"]},
        )
    return _to_detail(row)


@router.patch(
    "/{agent_id}",
    response_model=AgentDetail,
    dependencies=[require_role("owner", "admin")],
)
async def update_agent(agent_id: UUID, payload: AgentUpdate, tenant: TenantDep, container: ContainerDep) -> AgentDetail:
    await _check_escalation_destinations(tenant.tenant_id, payload.config)
    async with tenant_scope(tenant.tenant_id) as session:
        existing = (
            await session.execute(
                text(
                    "SELECT id, name, config, is_default, version FROM agents "
                    "WHERE id = :id FOR UPDATE"
                ),
                {"id": agent_id},
            )
        ).mappings().first()
        if existing is None:
            raise NotFoundError("no such agent")

        new_config = _parse_config(existing["config"])
        config_changed = False
        if payload.config is not None:
            merged = {**new_config.model_dump(), **payload.config}
            new_config = AgentConfig.model_validate(merged)
            config_changed = True

        new_version = existing["version"] + 1 if config_changed else existing["version"]
        new_name = payload.name if payload.name is not None else existing["name"]
        new_is_default = (
            payload.is_default if payload.is_default is not None else existing["is_default"]
        )

        if payload.is_default is True:
            await session.execute(
                text(
                    "UPDATE agents SET is_default = false "
                    "WHERE is_default = true AND id <> :id"
                ),
                {"id": agent_id},
            )

        await session.execute(
            text(
                """
                UPDATE agents
                   SET name = :name,
                       config = CAST(:config AS jsonb),
                       version = :version,
                       is_default = :is_default,
                       updated_at = now()
                 WHERE id = :id
                """
            ),
            {
                "id": agent_id,
                "name": new_name,
                "config": json.dumps(new_config.model_dump(mode="json")),
                "version": new_version,
                "is_default": new_is_default,
            },
        )

        row = (
            await session.execute(text(_DETAIL_SQL), {"id": agent_id})
        ).mappings().one()

        await _record(
            session,
            tenant,
            "agent.updated",
            target_id=str(agent_id),
            detail={
                "name": new_name,
                "version": new_version,
                "config_changed": config_changed,
            },
        )

    if config_changed:
        await invalidate_agent_config(container, tenant.tenant_id, agent_id)
    states = await _agent_states(container, tenant.tenant_id)
    return _to_detail(row, states)


@router.delete(
    "/{agent_id}",
    status_code=204,
    dependencies=[require_role("owner", "admin")],
)
async def delete_agent(agent_id: UUID, tenant: TenantDep, container: ContainerDep) -> None:
    async with tenant_scope(tenant.tenant_id) as session:
        existing = (
            await session.execute(
                text("SELECT id, kind FROM agents WHERE id = :id"), {"id": agent_id}
            )
        ).mappings().first()
        if existing is None:
            raise NotFoundError("no such agent")

        # Swarm rows are the platform's own cards, created from the registry:
        # they are the product, not user content. The row can be disabled
        # (PATCH /{agent_id}/enabled), never removed — deleting one would
        # silently drop the owner's edit of a definition the registry serves.
        if str(existing["kind"] or "chat") == "swarm":
            raise ConflictError(
                "swarm agents are part of the platform and cannot be deleted; "
                "disable them instead"
            )

        # The last-agent guard counts chat rows only: a swarm row is never a
        # tenant's usable agent (it carries no channels and is never the
        # default), so a tenant whose remaining rows are all swarm must still
        # be refused the deletion of their last *chat* agent exactly as before.
        chat_count = (
            await session.execute(
                text("SELECT count(*) FROM agents WHERE kind = 'chat'")
            )
        ).scalar_one()
        if chat_count <= 1:
            raise ConflictError("cannot delete a tenant's only agent")

        has_whatsapp = (
            await session.execute(
                text(
                    "SELECT 1 FROM whatsapp_integrations "
                    "WHERE agent_id = :id AND status <> 'disconnected'"
                ),
                {"id": agent_id},
            )
        ).first()
        if has_whatsapp is not None:
            raise ConflictError("agent is in use by an active WhatsApp integration")

        await session.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})

        await _record(session, tenant, "agent.deleted", target_id=str(agent_id))

    await invalidate_agent_config(container, tenant.tenant_id, agent_id)


@router.post(
    "/{agent_id}/preview",
    response_model=AgentPreviewOut,
    dependencies=[require_role("owner", "admin")],
)
async def preview_agent(
    agent_id: UUID,
    tenant: TenantDep,
    container: ContainerDep,
    payload: AgentPreviewRequest | None = None,
) -> AgentPreviewOut:
    """Render a stored prompt template against the tenant's current facts.

    The Instructions editor's live preview pane. For a swarm row the template
    is the tenant's stored edit (or the registry default before one exists)
    and the render goes through ``AgentPromptService.preview``, which never
    raises — it names every unresolved hole instead. For a chat row the
    persona is rendered through the same preview renderer with the tenant's
    facts: a chat persona declares no required holes, so this cannot fail
    either.
    """
    runtime: dict[str, Any] = dict(payload.runtime) if payload is not None else {}

    async with tenant_scope(tenant.tenant_id) as session:
        row = (
            await session.execute(text(_DETAIL_SQL), {"id": agent_id})
        ).mappings().first()
    if row is None:
        raise NotFoundError("no such agent")

    # a draft from the editor wins over whatever is stored: the pane exists to
    # show unsaved edits, and one code path serves both kinds of row
    override = payload.template if payload is not None and payload.template is not None else None
    if override is not None:
        facts = await container.agent_prompts.facts_for(tenant.tenant_id)
        rendered, missing = render_preview(override, facts.namespaces, runtime=runtime)
        return AgentPreviewOut(
            template=override,
            rendered=rendered,
            missing=[str(name) for name in missing],
        )

    agent_key = _agent_key(row["agent_key"])
    spec = BY_KEY.get(agent_key) if agent_key is not None else None
    if spec is not None:
        template = await container.agent_prompts.template_for(tenant.tenant_id, spec)
        rendered, missing = await container.agent_prompts.preview(
            tenant.tenant_id, spec.key, runtime=runtime
        )
        return AgentPreviewOut(
            template=template if template else spec.prompt,
            rendered=rendered,
            missing=[str(name) for name in missing],
        )

    config = _parse_config(row["config"])
    facts = await container.agent_prompts.facts_for(tenant.tenant_id)
    rendered, missing = render_preview(
        config.system_prompt, facts.namespaces, runtime=runtime
    )
    return AgentPreviewOut(
        template=config.system_prompt,
        rendered=rendered,
        missing=[str(name) for name in missing],
    )


async def _cancel_queued_outbound(tenant_id: UUID) -> int:
    """Mark the tenant's queued-but-unsent outbound rows as failed.

    The off-switch must reach the queue or it is a lie about what is still in
    flight: nothing re-checks the agent's state at send time (``dispatch.
    _send_all`` trusts the queue), so a row already ``queued`` when the owner
    disables the agent would otherwise still go out. ``failed`` is this
    table's existing stop vocabulary — the same status every other cancel path
    (`cancel_queued_for_lead`, `cancel_queued_for_campaign`) writes, for the
    same reason: the row stays as the audit trail of a message that never
    went. RLS scopes the update to the tenant, like every other query in this
    router.
    """
    try:
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    "UPDATE growth_messages SET status = 'failed' "
                    "WHERE direction = 'out' AND status = 'queued'"
                )
            )
        return int(getattr(result, "rowcount", 0) or 0)
    except Exception:
        # fail-open by instruction: the disable itself must persist even when
        # the cancellation cannot. The next dispatch tick still gates on the
        # agent's state, so the exposure is the in-flight window, not a
        # permanent leak.
        logger.warning(
            "agent_disable_cancel_failed", tenant_id=str(tenant_id), exc_info=True
        )
        return 0


@router.patch(
    "/{agent_id}/enabled",
    response_model=AgentDetail,
    dependencies=[require_role("owner", "admin")],
)
async def set_agent_enabled(
    agent_id: UUID,
    payload: AgentEnabledUpdate,
    tenant: TenantDep,
    container: ContainerDep,
) -> AgentDetail:
    """The owner's off switch for a swarm agent.

    Swarm only: a chat agent is turned off by removing its channels, and an
    ``enabled`` flag nothing reads would be a button that does nothing.
    Disabling also cancels the tenant's queued outbound messages (fail-open —
    see ``_cancel_queued_outbound``) so the queue does not keep sending after
    the off-switch. The audit row records who flipped it and which way.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        existing = (
            await session.execute(
                text("SELECT id, kind FROM agents WHERE id = :id FOR UPDATE"),
                {"id": agent_id},
            )
        ).mappings().first()
        if existing is None:
            raise NotFoundError("no such agent")
        if str(existing["kind"] or "chat") != "swarm":
            raise ConflictError(
                "chat agents are turned off by removing their channels, not by "
                "this switch"
            )

        await session.execute(
            text(
                "UPDATE agents SET enabled = :enabled, updated_at = now() "
                "WHERE id = :id"
            ),
            {"enabled": payload.enabled, "id": agent_id},
        )

        row = (
            await session.execute(text(_DETAIL_SQL), {"id": agent_id})
        ).mappings().one()

        await _record(
            session,
            tenant,
            "agent.enabled",
            target_id=str(agent_id),
            detail={"enabled": payload.enabled},
        )

    if not payload.enabled:
        cancelled = await _cancel_queued_outbound(tenant.tenant_id)
        if cancelled:
            logger.info(
                "agent_disabled_cancelled_queued",
                tenant_id=str(tenant.tenant_id),
                agent_id=str(agent_id),
                cancelled=cancelled,
            )

    await invalidate_agent_config(container, tenant.tenant_id, agent_id)
    states = await _agent_states(container, tenant.tenant_id)
    return _to_detail(row, states)
