"""FastAPI dependencies: credential resolution and per-request wiring.

Every route that touches tenant data depends on `current_tenant`. That is the
single place a request acquires an identity, so a route that forgets it has no
tenant to query with and fails loudly rather than leaking.
"""

from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request

from app.api.tenancy import from_api_key, from_user_jwt
from app.core.config import Settings
from app.core.container import Container
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.logging import get_logger, tenant_id_var
from app.domain.agent_config import AgentConfig
from app.domain.entities import TenantContext

logger = get_logger(__name__)

# agent config changes rarely and is read on every request. a short ttl keeps a
# stale read bounded without needing cross-instance invalidation
AGENT_CONFIG_TTL_SECONDS = 60


def get_container(request: Request) -> Container:
    return request.app.state.container  # type: ignore[no-any-return]


def get_settings(request: Request) -> Settings:
    return request.app.state.container.settings  # type: ignore[no-any-return]


def client_ip(request: Request) -> str:
    """Best-effort client IP for abuse logging and coarse rate limiting.

    Prefers the left-most `X-Forwarded-For` hop, because in the target
    deployment TLS is terminated by a proxy and `request.client` is then the
    proxy, not the caller; falls back to the socket peer when the header is
    absent (a direct call, or local dev). The header is caller-supplied and so
    spoofable — it is only ever used for coarse abuse controls and log
    attribution here, never to authenticate or authorise. The non-spoofable
    per-account caps are the real credential-stuffing control.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = request.client
    return client.host if client else "unknown"


async def current_tenant(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_tenant: Annotated[str | None, Header()] = None,
) -> TenantContext:
    """Resolve the caller.

    Ordered by authority: a server-side API key outranks a dashboard user token.
    """
    settings: Settings = request.app.state.container.settings

    if authorization and authorization.startswith("Bearer sk_"):
        ctx = await from_api_key(authorization.removeprefix("Bearer ").strip())
    elif authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        ctx = await from_user_jwt(token, settings.SECRET_KEY, x_tenant)
    else:
        raise AuthenticationError("no credentials supplied")

    # Bind the resolved tenant onto the logging context. `request_context`
    # cleared it to None at the top of the request; every authenticated caller
    # passes through here, so binding once labels all downstream logs on this
    # request without threading the id through each route.
    tenant_id_var.set(str(ctx.tenant_id))
    return ctx


TenantDep = Annotated[TenantContext, Depends(current_tenant)]
ContainerDep = Annotated[Container, Depends(get_container)]


def require_role(*allowed: str) -> Any:
    """Gate a route on membership role.

    Only meaningful for dashboard users. An API key has no role, so key-authed
    calls are rejected here — administrative actions should not be reachable with
    a credential that lives in a config file.
    """

    async def _check(tenant: TenantDep) -> TenantContext:
        if tenant.role is None:
            raise AuthorizationError("this action requires a signed-in user")
        if tenant.role not in allowed:
            raise AuthorizationError(f"role {tenant.role!r} may not perform this action")
        return tenant

    return Depends(_check)


async def _facts_version(container: Container, tenant_id: UUID) -> int:
    """The tenant's facts_version, or 0 when it cannot be read.

    The version is what makes a chat-agent cache entry sensitive to profile
    edits without any explicit fan-out: bump it and the old entry is simply
    never looked up again. The read is fail-open to 0 — the chat path must
    never 500 for a facts read, and the worst case is a stale cache bounded by
    the TTL, which is exactly the pre-version behaviour.
    """
    try:
        facts = await container.agent_prompts.facts_for(tenant_id)
    except Exception:
        logger.warning(
            "agent_cache_facts_unavailable", tenant_id=str(tenant_id), exc_info=True
        )
        return 0
    try:
        return int(facts.version)
    except (TypeError, ValueError):
        return 0


async def load_agent_config(
    tenant: TenantContext, container: Container, agent_id: Any = None
) -> tuple[Any, AgentConfig]:
    """Fetch the agent row and its config.

    Cached: this is read on every chat request and changes only when a tenant
    edits its agent. The cache key carries the agent id and the tenant's
    ``facts_version``, so a profile edit invalidates every chat-agent entry
    for that tenant without explicit fan-out — the old entries become
    unreachable the moment the version moves. Writes additionally evict the
    current key via ``invalidate_agent_config``, and the TTL bounds anything
    that slips past either mechanism.

    Falls back to the tenant's default agent when none is named, so a caller that
    does not care about agents never has to send an ID.
    """
    from sqlalchemy import text

    from app.core.db import tenant_scope

    target = agent_id or tenant.agent_id
    version = await _facts_version(container, tenant.tenant_id)
    cache_key = f"agent:{tenant.tenant_id}:{target or 'default'}:f{version}"

    cached = await container.cache.get(cache_key)
    if cached:
        try:
            payload = json.loads(cached)
            return (
                UUID(payload["id"]) if payload.get("id") else None,
                AgentConfig.model_validate(payload["config"]),
            )
        except Exception:
            # a malformed or stale-shaped entry is not worth failing over.
            # drop it and read through
            logger.warning("agent_cache_unreadable", key=cache_key, exc_info=True)
            await container.cache.delete(cache_key)

    async with tenant_scope(tenant.tenant_id) as session:
        if target is not None:
            row = (
                await session.execute(
                    text("SELECT id, config FROM agents WHERE id = :id"), {"id": target}
                )
            ).mappings().first()
        else:
            row = (
                await session.execute(
                    text(
                        "SELECT id, config FROM agents WHERE is_default "
                        "ORDER BY created_at LIMIT 1"
                    )
                )
            ).mappings().first()

    if row is None:
        # a tenant with no agent is a provisioning bug, but answering with a
        # default beats a 500 — the platform defaults are safe. deliberately not
        # cached, so provisioning the agent takes effect immediately
        logger.warning("agent_missing_using_defaults", tenant_id=str(tenant.tenant_id))
        return None, AgentConfig.default()

    config = AgentConfig.model_validate(row["config"] or {})
    await container.cache.set(
        cache_key,
        json.dumps({"id": str(row["id"]), "config": config.model_dump(mode="json")}),
        ttl_seconds=AGENT_CONFIG_TTL_SECONDS,
    )
    return row["id"], config


async def invalidate_agent_config(
    container: Container, tenant_id: UUID, agent_id: UUID | None = None
) -> None:
    """Drop cached config after a write.

    Both targets are cleared because the same agent is reachable by id and as
    the tenant's default, and a write to one view must not leave the other
    stale. The keys carry the tenant's current ``facts_version`` (see
    ``load_agent_config``), so the invalidation resolves the same version the
    next read will and deletes exactly that key; a version that moves between
    the write and this call leaves a stale entry that the TTL reaps — the same
    bound a missed invalidation always had.
    """
    version = await _facts_version(container, tenant_id)
    keys = [f"agent:{tenant_id}:default:f{version}"]
    if agent_id is not None:
        keys.append(f"agent:{tenant_id}:{agent_id}:f{version}")
    for key in keys:
        await container.cache.delete(key)
