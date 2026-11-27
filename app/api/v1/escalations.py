"""Escalations: what the tenant reads, and the one thing a caller may write.

Two audiences in one router, which is why the role gate is per route rather than
on the router. The list, detail, stats and test endpoints are dashboard-only. The
contact endpoint accepts any authenticated caller, because the person who has to
supply a reply-to address is the visitor, and they have no dashboard login.

Nothing here ever returns `contact_ciphertext`. The masked form is the only
contact detail that crosses the wire on a read, and the plaintext exists solely to
be rendered into the notification that goes to the tenant's own inbox.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ContainerDep, TenantDep, load_agent_config, require_role
from app.core.constants import CONTACT_SOURCE_API
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.domain.escalation import (
    EscalationTrigger,
    VisitorContact,
)
from app.services import audit
from app.services.audit import AUDIT_ESCALATION_CONTACT_ADDED, AUDIT_ESCALATION_TEST_SENT

logger = get_logger(__name__)

# Per visitor, per minute. Three is enough for a typo and a correction; more than
# that is somebody walking ids, and the endpoint writes to a row they were handed
# rather than one they chose.
CONTACT_RATE_PER_MINUTE = 3

TEST_QUESTION = (
    "This is a test escalation from the dashboard. If you are reading it, the "
    "wiring works and a real one will reach you the same way."
)

router = APIRouter(prefix="/escalations", tags=["escalations"])

_DASHBOARD_ROLES = ("owner", "admin", "member")


class EscalationOut(BaseModel):
    """One escalation as the dashboard sees it."""

    id: UUID
    trigger: str
    channel: str
    state: str
    question: str
    answer: str | None = None
    confidence: float = 0.0
    agent_id: UUID | None = None
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    # documents that were retrieved and judged insufficient: the ones to fix
    considered: list[dict[str, Any]] = Field(default_factory=list)
    contact_masked: str | None = None
    contact_source: str = "unknown"
    external_user_ref: str | None = None
    page_url: str | None = None
    note: str | None = None
    receipts: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime | None = None
    notified_at: datetime | None = None


class EscalationListOut(BaseModel):
    items: list[EscalationOut] = Field(default_factory=list)
    total: int = 0


class EscalationStatsOut(BaseModel):
    """Daily counts per trigger. One row per (day, trigger) pair."""

    days: int
    rows: list[dict[str, Any]] = Field(default_factory=list)


class EscalationContactRequest(BaseModel):
    """What the contact form collects. Either email or phone must be present."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str | None = Field(default=None, max_length=320)
    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)


class EscalationContactOut(BaseModel):
    accepted: bool


class EscalationTestOut(BaseModel):
    escalated: bool
    escalation_id: UUID | None = None
    # populated when a gate stopped it: disabled, trigger_off, no_destination,
    # no_contact, rate_limited or cooldown. That answer is the point of the test.
    suppressed_reason: str | None = None


def _out(row: Any) -> EscalationOut:
    return EscalationOut(
        id=row.id,
        trigger=row.trigger,
        channel=row.channel,
        state=row.state,
        question=row.question,
        answer=row.answer,
        confidence=row.confidence,
        agent_id=row.agent_id,
        conversation_id=row.conversation_id,
        message_id=row.message_id,
        considered=list(row.considered or []),
        contact_masked=row.contact_masked,
        contact_source=row.contact_source,
        external_user_ref=row.external_user_ref,
        page_url=row.page_url,
        note=row.note,
        receipts=list(row.receipts or []),
        created_at=row.created_at,
        notified_at=row.notified_at,
    )


@router.get("", response_model=EscalationListOut, dependencies=[require_role(*_DASHBOARD_ROLES)])
async def list_escalations(
    tenant: TenantDep,
    container: ContainerDep,
    state: Annotated[str | None, Query(max_length=20)] = None,
    trigger: Annotated[str | None, Query(max_length=40)] = None,
    channel: Annotated[str | None, Query(max_length=20)] = None,
    conversation_id: Annotated[UUID | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EscalationListOut:
    """Newest first, with a total so the dashboard can page."""
    rows, total = await container.escalation_repo.list_for_tenant(
        tenant.tenant_id,
        state=state,
        trigger=trigger,
        channel=channel,
        conversation_id=conversation_id,
        since=since,
        limit=limit,
        offset=offset,
    )
    return EscalationListOut(items=[_out(row) for row in rows], total=total)


@router.get(
    "/stats", response_model=EscalationStatsOut, dependencies=[require_role(*_DASHBOARD_ROLES)]
)
async def escalation_stats(
    tenant: TenantDep,
    container: ContainerDep,
    days: Annotated[int, Query(ge=1, le=180)] = 30,
) -> EscalationStatsOut:
    """Escalation rate by day and by trigger.

    Declared before `/{escalation_id}` so the literal path wins the match rather
    than being read as a malformed UUID.
    """
    rows = await container.escalation_repo.rate_by_day(tenant.tenant_id, days=days)
    return EscalationStatsOut(days=days, rows=rows)


@router.post(
    "/test", response_model=EscalationTestOut, dependencies=[require_role("owner", "admin")]
)
async def test_escalation(tenant: TenantDep, container: ContainerDep) -> EscalationTestOut:
    """Send a synthetic escalation through the real path, gates included.

    `external_user_ref` is a fresh random value so the cooldown can never swallow
    a test, and every other gate is left in place: a tenant testing the wiring
    wants to be told that escalation is disabled or that no destination is set,
    not to be shown a success that a real customer will not get.
    """
    agent_id, config = await load_agent_config(tenant, container)
    decision = await container.escalation.maybe_escalate(
        tenant_id=tenant.tenant_id,
        config=config,
        trigger=EscalationTrigger.USER_REQUESTED,
        question=TEST_QUESTION,
        channel="api",
        agent_id=agent_id,
        external_user_ref=f"test:{uuid4().hex}",
        note="synthetic test from the dashboard",
    )
    await audit.record(
        AUDIT_ESCALATION_TEST_SENT,
        actor_type="user",
        actor_id=tenant.user_id,
        target_tenant=tenant.tenant_id,
        target_type="escalation",
        target_id=str(decision.escalation_id) if decision.escalation_id else None,
        detail={"escalated": decision.escalated, "reason": decision.suppressed_reason},
    )
    return EscalationTestOut(
        escalated=decision.escalated,
        escalation_id=decision.escalation_id,
        suppressed_reason=decision.suppressed_reason,
    )


@router.get(
    "/{escalation_id}",
    response_model=EscalationOut,
    dependencies=[require_role(*_DASHBOARD_ROLES)],
)
async def get_escalation(
    escalation_id: UUID, tenant: TenantDep, container: ContainerDep
) -> EscalationOut:
    row = await container.escalation_repo.get(tenant.tenant_id, escalation_id)
    if row is None:
        raise NotFoundError("no such escalation")
    return _out(row)


@router.post("/{escalation_id}/contact", response_model=EscalationContactOut)
async def add_contact(
    escalation_id: UUID,
    payload: EscalationContactRequest,
    tenant: TenantDep,
    container: ContainerDep,
) -> EscalationContactOut:
    """Attach the reply-to details the escalation asked the caller to collect.

    Any authenticated caller, and three checks before the write, because the id
    in the path was handed to a caller rather than chosen by them: a session's
    visitor must be the one the escalation was created for, and a contact that
    is already set is never overwritten. Both of those are what stops this
    being an endpoint for editing a stranger's escalation.
    """
    visitor = tenant.external_user_ref or str(tenant.tenant_id)
    await container.rate_limiter.check(
        "escalation-contact", visitor, CONTACT_RATE_PER_MINUTE, 60
    )

    row = await container.escalation_repo.get(tenant.tenant_id, escalation_id)
    if row is None:
        raise NotFoundError("no such escalation")
    if tenant.external_user_ref and row.external_user_ref != tenant.external_user_ref:
        # not a 403: telling a caller that the row exists but is not theirs is an
        # enumeration oracle
        raise NotFoundError("no such escalation")
    if row.contact_ciphertext is not None:
        raise ConflictError("this escalation already has a contact")

    contact = VisitorContact(
        email=payload.email or None,
        name=payload.name or None,
        phone=payload.phone or None,
        source=CONTACT_SOURCE_API,
    )
    accepted = await container.escalation.set_contact(tenant.tenant_id, escalation_id, contact)
    if accepted:
        await audit.record(
            AUDIT_ESCALATION_CONTACT_ADDED,
            actor_type="visitor",
            actor_id=None,
            target_tenant=tenant.tenant_id,
            target_type="escalation",
            target_id=str(escalation_id),
            detail={"has_email": bool(payload.email), "has_phone": bool(payload.phone)},
        )
    return EscalationContactOut(accepted=accepted)
