"""Tenant resolution. Runs before any handler and decides who is asking.

Three credential paths, one outcome: a `TenantContext` or a 401. Nothing
downstream re-derives the tenant, so a mistake here is a cross-tenant data leak
rather than a bad response.

Order matters. The dashboard JWT and the API key carry real authority; inbound
channel identifiers (WhatsApp) are resolved from provider-verified routing keys.
"""

from __future__ import annotations

from time import time
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.core.constants import Channel
from app.core.db import admin_scope
from app.core.errors import AuthenticationError, TenantSuspendedError
from app.core.logging import get_logger
from app.core.security import hash_api_key, verify_token
from app.domain.entities import TenantContext

logger = get_logger(__name__)

# Membership resolution runs on every dashboard request, and against a pooler on
# another continent that lookup alone can cost most of a fast endpoint's latency —
# a poll loop pays it twice a second while the numbers on screen do not move. The
# row it reads changes when someone is added to a workspace, has their role
# changed, or the workspace is suspended, so it is cached for seconds, not
# minutes: the token is still verified on every request, and the worst case is a
# role or suspension taking effect one short window late.
_MEMBERSHIP_TTL_SECONDS = 15.0
_membership_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _membership_cached(key: tuple[str, str]) -> dict[str, Any] | None:
    hit = _membership_cache.get(key)
    if hit is None:
        return None
    expires, row = hit
    if expires <= time():
        _membership_cache.pop(key, None)
        return None
    return row


def _membership_store(key: tuple[str, str], row: dict[str, Any]) -> None:
    if len(_membership_cache) > 512:
        # bounded, and cheap to rebuild: drop everything rather than track an LRU
        _membership_cache.clear()
    _membership_cache[key] = (time() + _MEMBERSHIP_TTL_SECONDS, row)


def forget_membership(user_id: UUID | str | None = None) -> None:
    """Drop cached membership rows, so a role or suspension change is immediate.

    Called by the endpoints that write memberships or tenant status. Without an
    argument it clears the whole cache, which is what a tenant-level change wants.
    """
    if user_id is None:
        _membership_cache.clear()
        return
    wanted = str(user_id)
    for key in [key for key in _membership_cache if key[0] == wanted]:
        _membership_cache.pop(key, None)


async def from_api_key(plaintext: str) -> TenantContext:
    """Server-to-server. The key is stored hashed, so the lookup is by hash.

    Comparing hashes rather than plaintext means a database disclosure does not
    hand over working credentials.
    """
    key_hash = hash_api_key(plaintext)

    async with admin_scope() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT k.id AS key_id, k.tenant_id, k.scopes, k.expires_at,
                           t.slug, t.status
                      FROM api_keys k
                      JOIN tenants t ON t.id = k.tenant_id
                     WHERE k.key_hash = :key_hash AND k.revoked_at IS NULL
                    """
                ),
                {"key_hash": key_hash},
            )
        ).mappings().first()

    if row is None:
        raise AuthenticationError("invalid api key")
    if row["expires_at"] is not None:
        from datetime import UTC, datetime

        if row["expires_at"] < datetime.now(UTC):
            raise AuthenticationError("api key expired")
    _require_active(row["status"], row["tenant_id"])

    return TenantContext(
        tenant_id=row["tenant_id"],
        tenant_slug=row["slug"],
        channel=Channel.API,
        api_key_id=row["key_id"],
    )


async def from_whatsapp(phone_number_id: str) -> TenantContext:
    """Inbound WhatsApp. Meta's `phone_number_id` is the routing key.

    It is unique per tenant integration, which is why the column carries a unique
    constraint — two tenants resolving from one number would cross-post messages.
    """
    async with admin_scope() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT w.tenant_id, w.agent_id, t.slug, t.status
                      FROM whatsapp_integrations w
                      JOIN tenants t ON t.id = w.tenant_id
                     WHERE w.phone_number_id = :pnid AND w.status = 'active'
                    """
                ),
                {"pnid": phone_number_id},
            )
        ).mappings().first()

    if row is None:
        raise AuthenticationError("unknown whatsapp number")
    _require_active(row["status"], row["tenant_id"])

    return TenantContext(
        tenant_id=row["tenant_id"],
        tenant_slug=row["slug"],
        channel=Channel.WHATSAPP,
        agent_id=row["agent_id"],
    )


async def from_user_jwt(token: str, secret: str, tenant_slug: str | None = None) -> TenantContext:
    """Dashboard. A user may belong to several tenants, so the target is chosen
    by slug and then checked against membership — never taken on trust."""
    payload = verify_token(token, secret)
    try:
        user_id = UUID(str(payload["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        # A correctly signed token of another family (a resume ticket carries a
        # call id as `sub`, not a UUID) must fail auth, not 500.
        raise AuthenticationError("invalid session token") from exc

    cache_key = (str(user_id), tenant_slug or "")
    row: dict[str, Any] | None = _membership_cached(cache_key)
    if row is None:
        async with admin_scope() as session:
            query = """
                SELECT m.tenant_id, m.role, t.slug, t.status
                  FROM memberships m
                  JOIN tenants t ON t.id = m.tenant_id
                 WHERE m.user_id = :user_id
            """
            params: dict[str, Any] = {"user_id": user_id}
            if tenant_slug:
                query += " AND t.slug = :slug"
                params["slug"] = tenant_slug
            query += " ORDER BY m.created_at LIMIT 1"

            found = (await session.execute(text(query), params)).mappings().first()
        row = dict(found) if found is not None else None
        if row is not None:
            _membership_store(cache_key, row)

    if row is None:
        raise AuthenticationError("no tenant membership")
    _require_active(row["status"], row["tenant_id"])

    return TenantContext(
        tenant_id=row["tenant_id"],
        tenant_slug=row["slug"],
        channel=Channel.API,
        user_id=user_id,
        role=row["role"],
    )


def _require_active(status: str, tenant_id: UUID) -> None:
    if status != "active":
        logger.info("tenant_not_active", tenant_id=str(tenant_id), status=status)
        raise TenantSuspendedError(f"tenant is {status}")
