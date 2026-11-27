"""Platform level repositories. These read tenant tables before a tenant is
known, so they use admin_scope and never touch RLS protected rows.

Repository boundaries are per aggregate, not per table, and every tenant owned
query goes through tenant_scope from app.core.db.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.db import admin_scope
from app.domain.entities import Tenant

TENANT_FIELDS = "id, name, slug, status"


async def get_tenant_by_id(tenant_id: UUID) -> Tenant | None:
    async with admin_scope() as session:
        row = (
            await session.execute(
                text(f"SELECT {TENANT_FIELDS} FROM tenants WHERE id = :id"),
                {"id": tenant_id},
            )
        ).mappings().first()
    if row is None:
        return None
    return Tenant(id=row["id"], name=row["name"], slug=row["slug"], status=row["status"])


async def get_tenant_by_slug(slug: str) -> Tenant | None:
    async with admin_scope() as session:
        row = (
            await session.execute(
                text(f"SELECT {TENANT_FIELDS} FROM tenants WHERE slug = :slug"),
                {"slug": slug},
            )
        ).mappings().first()
    if row is None:
        return None
    return Tenant(id=row["id"], name=row["name"], slug=row["slug"], status=row["status"])


async def create_tenant(name: str, slug: str) -> Tenant:
    tenant_id = uuid4()
    async with admin_scope() as session:
        await session.execute(
            text("INSERT INTO tenants (id, name, slug) VALUES (:id, :name, :slug)"),
            {"id": tenant_id, "name": name, "slug": slug},
        )
    return Tenant(id=tenant_id, name=name, slug=slug, status="active")


async def create_user(
    email: str, name: str | None = None, password_hash: str | None = None
) -> UUID:
    user_id = uuid4()
    async with admin_scope() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, name, password_hash) "
                "VALUES (:id, :email, :name, :password_hash)"
            ),
            {"id": user_id, "email": email, "name": name, "password_hash": password_hash},
        )
    return user_id


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    async with admin_scope() as session:
        row = (
            await session.execute(text("SELECT * FROM users WHERE email = :email"), {"email": email})
        ).mappings().first()
    return dict(row) if row else None


async def get_user_by_id(user_id: UUID) -> dict[str, Any] | None:
    async with admin_scope() as session:
        row = (
            await session.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id})
        ).mappings().first()
    return dict(row) if row else None


async def list_memberships_for_user(user_id: UUID) -> list[dict[str, Any]]:
    """Every tenant a user can act in, for the dashboard's tenant switcher.

    A user is not scoped to one tenant, so this deliberately runs outside
    ``tenant_scope`` — there is no single tenant to set as context yet.
    """
    async with admin_scope() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT t.id AS tenant_id, t.slug, t.name, t.status, m.role
                      FROM memberships m
                      JOIN tenants t ON t.id = m.tenant_id
                     WHERE m.user_id = :user_id
                     ORDER BY m.created_at
                    """
                ),
                {"user_id": user_id},
            )
        ).mappings().all()
    return [dict(row) for row in rows]


async def generate_unique_tenant_slug(base: str) -> str:
    """Derive an available slug from a tenant name.

    Collisions are common ("Acme", "Acme Inc") and a human should not have to
    invent a unique identifier to sign up, so a numeric suffix is appended only
    when needed.
    """
    import re

    root = re.sub(r"[^a-z0-9-]+", "-", base.strip().lower()).strip("-")[:48] or "tenant"
    if len(root) < 2:
        root = f"{root}-tenant"
    candidate = root
    suffix = 0
    while await get_tenant_by_slug(candidate) is not None:
        suffix += 1
        candidate = f"{root}-{suffix}"
    return candidate


async def add_membership(tenant_id: UUID, user_id: UUID, role: str = "member") -> None:
    from app.api.tenancy import forget_membership

    async with admin_scope() as session:
        await session.execute(
            text(
                "INSERT INTO memberships (tenant_id, user_id, role) VALUES (:t, :u, :r) "
                "ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = EXCLUDED.role"
            ),
            {"t": tenant_id, "u": user_id, "r": role},
        )
    # request resolution caches this row for a few seconds; a role written here has
    # to take effect on the next request, not at the end of that window
    forget_membership(user_id)


async def get_user_role(tenant_id: UUID, user_id: UUID) -> str | None:
    async with admin_scope() as session:
        row = (
            await session.execute(
                text("SELECT role FROM memberships WHERE tenant_id = :t AND user_id = :u"),
                {"t": tenant_id, "u": user_id},
            )
        ).mappings().first()
    return row["role"] if row else None


async def create_default_agent(tenant_id: UUID, name: str = "Assistant") -> UUID:
    agent_id = uuid4()
    async with admin_scope() as session:
        await session.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, config, is_default) "
                "VALUES (:id, :t, :name, '{}'::jsonb, true)"
            ),
            {"id": agent_id, "t": tenant_id, "name": name},
        )
    return agent_id


async def touch_api_key(key_id: UUID) -> None:
    async with admin_scope() as session:
        await session.execute(
            text("UPDATE api_keys SET last_used_at = :now WHERE id = :id"),
            {"id": key_id, "now": datetime.now()},
        )
