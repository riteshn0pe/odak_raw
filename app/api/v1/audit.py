"""Tenant-facing view of the audit trail.

The same append-only log the staff console reads, but scoped hard to the calling
tenant: a tenant sees what happened inside its own org and nothing else. Like the
admin side, this is read-only by construction — writes happen inside the actions
being audited, via `services.audit.record`.

The log is a platform table with no RLS, so the read runs under admin_scope; the
isolation here is the mandatory `target_tenant = <this tenant>` filter, not the
database. Owner/admin only, since the trail names who did what.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import TenantDep, require_role

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get(
    "",
    response_model=list[dict],
    dependencies=[require_role("owner", "admin")],
)
async def list_audit(
    tenant: TenantDep,
    action: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    """This tenant's trail, newest first, with an optional action filter.

    The `target_tenant` filter is pinned to the caller's own id and cannot be
    widened by a query parameter — a tenant can never read another tenant's rows.
    """
    from app.services import audit

    return await audit.query(
        target_tenant=tenant.tenant_id,
        action=action,
        limit=limit,
    )
