"""API key management: issue, list, revoke, and re-scope tenant API keys.

Only a sha256 hash is ever stored (see app.core.security). The plaintext key is
returned exactly once, at creation time, and is unrecoverable after that response
— losing it means minting a new key, not "recovering" the old one. Scopes exist
so a key can be limited to what it actually needs (e.g. chat only, no document
access), which bounds the damage of a leaked key.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.api.deps import ContainerDep, TenantDep, require_role
from app.core.db import tenant_scope
from app.core.errors import NotFoundError, ValidationError
from app.core.security import generate_api_key
from app.domain.entities import TenantContext
from app.services import audit

router = APIRouter(
    prefix="/keys",
    tags=["keys"],
    dependencies=[require_role("owner", "admin")],
)

DEFAULT_SCOPES = ["chat:write", "documents:read"]


async def _record(
    session: Any,
    tenant: TenantContext,
    action: str,
    *,
    target_id: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Write an audit row in the mutation's own transaction.

    The acting user's email is resolved from the same session so the trail names
    who issued or revoked a key. Only the key prefix and scopes are recorded here
    — never the plaintext credential or its hash.
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
        target_type="api_key",
        target_id=target_id,
        detail=detail,
        session=session,
    )


class ApiKeyOut(BaseModel):
    """Safe-to-list view of a key. Never carries key_hash or plaintext."""

    id: UUID
    name: str
    key_prefix: str
    scopes: list[str]
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime | None = None


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_SCOPES))
    expires_in_days: int | None = Field(default=None, gt=0)
    test: bool = False


class ApiKeyCreated(ApiKeyOut):
    """Returned only from the create endpoint.

    api_key holds the plaintext credential. It is shown here once and is not
    retrievable through any other route — the server only ever stores its hash.
    """

    api_key: str
    plaintext_warning: str = "this key is shown once and cannot be retrieved again"


class ApiKeyScopesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scopes: list[str] = Field(min_length=1)


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(tenant: TenantDep) -> list[ApiKeyOut]:
    """List every key for the tenant, revoked or not, so history stays visible."""

    async with tenant_scope(tenant.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, name, key_prefix, scopes, last_used_at,
                           expires_at, revoked_at, created_at
                      FROM api_keys
                     ORDER BY created_at DESC
                    """
                )
            )
        ).mappings().all()
    return [
        ApiKeyOut(
            id=row["id"],
            name=row["name"],
            key_prefix=row["key_prefix"],
            scopes=list(row["scopes"] or []),
            last_used_at=row["last_used_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


@router.post("", response_model=ApiKeyCreated, status_code=201)
async def create_key(
    payload: ApiKeyCreate,
    tenant: TenantDep,
    container: ContainerDep,
) -> ApiKeyCreated:
    plaintext, key_hash, key_prefix = generate_api_key(test=payload.test)
    expires_at = None
    if payload.expires_in_days:
        # `expires_in_days` is an unbounded int from the request body; a huge
        # value overflows `timedelta`/`datetime` and would surface as a 500.
        try:
            expires_at = datetime.now(UTC) + timedelta(days=payload.expires_in_days)
        except OverflowError as exc:
            raise ValidationError("expiry is too far in the future") from exc
    key_id = uuid4()

    async with tenant_scope(tenant.tenant_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO api_keys (
                        id, tenant_id, name, key_hash, key_prefix, scopes, expires_at
                    ) VALUES (
                        :id, :tenant_id, :name, :key_hash, :key_prefix, :scopes, :expires_at
                    )
                    RETURNING id, name, key_prefix, scopes, expires_at, created_at
                    """
                ),
                {
                    "id": key_id,
                    "tenant_id": tenant.tenant_id,
                    "name": payload.name,
                    "key_hash": key_hash,
                    "key_prefix": key_prefix,
                    "scopes": payload.scopes,
                    "expires_at": expires_at,
                },
            )
        ).mappings().one()

        await _record(
            session,
            tenant,
            "apikey.issued",
            target_id=str(row["id"]),
            detail={
                "name": row["name"],
                "key_prefix": row["key_prefix"],
                "scopes": list(row["scopes"] or []),
            },
        )

    return ApiKeyCreated(
        id=row["id"],
        name=row["name"],
        key_prefix=row["key_prefix"],
        scopes=list(row["scopes"] or []),
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        api_key=plaintext,
    )


@router.delete("/{key_id}", status_code=204)
async def revoke_key(key_id: UUID, tenant: TenantDep) -> None:
    """Revoke a key. Idempotent: revoking twice is not an error."""

    async with tenant_scope(tenant.tenant_id) as session:
        existing = (
            await session.execute(
                text("SELECT id, revoked_at FROM api_keys WHERE id = :id FOR UPDATE"),
                {"id": key_id},
            )
        ).mappings().first()
        if existing is None:
            raise NotFoundError("no such API key")

        if existing["revoked_at"] is None:
            await session.execute(
                text("UPDATE api_keys SET revoked_at = now() WHERE id = :id"),
                {"id": key_id},
            )
            await _record(
                session, tenant, "apikey.revoked", target_id=str(key_id)
            )


@router.patch("/{key_id}/scopes", response_model=ApiKeyOut)
async def update_key_scopes(
    key_id: UUID,
    payload: ApiKeyScopesUpdate,
    tenant: TenantDep,
) -> ApiKeyOut:
    async with tenant_scope(tenant.tenant_id) as session:
        existing = (
            await session.execute(
                text(
                    "SELECT id, revoked_at FROM api_keys WHERE id = :id FOR UPDATE"
                ),
                {"id": key_id},
            )
        ).mappings().first()
        if existing is None:
            raise NotFoundError("no such API key")
        if existing["revoked_at"] is not None:
            raise ValidationError("cannot change scopes on a revoked key")

        row = (
            await session.execute(
                text(
                    """
                    UPDATE api_keys
                       SET scopes = :scopes
                     WHERE id = :id
                    RETURNING id, name, key_prefix, scopes, last_used_at,
                              expires_at, revoked_at, created_at
                    """
                ),
                {"id": key_id, "scopes": payload.scopes},
            )
        ).mappings().one()

        await _record(
            session,
            tenant,
            "apikey.scopes_updated",
            target_id=str(key_id),
            detail={"scopes": list(row["scopes"] or [])},
        )

    return ApiKeyOut(
        id=row["id"],
        name=row["name"],
        key_prefix=row["key_prefix"],
        scopes=list(row["scopes"] or []),
        last_used_at=row["last_used_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        created_at=row["created_at"],
    )
