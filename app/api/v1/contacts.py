"""Tenant-facing contacts: search, detail, edit, erasure and export.

These endpoints exist for support and for GDPR, which is why `email`, `phone`,
`name` and `external_id` are stored in plaintext: they have to be searchable by a
human and interpolatable into an action URL, and the tenant already receives them
unmasked in escalations. RLS is the boundary, not encryption.

Two fields are not editable anywhere here. `external_id` is the tenant's own user
id as asserted by a signed token, and `verified_at` is the record of that
assertion — they describe how we came to believe who someone is, so a dashboard
user retyping them would turn an assertion into an opinion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ContainerDep, TenantDep, require_role
from app.core.constants import (
    AUDIT_CONTACT_DELETED,
    AUDIT_CONTACT_EXPORTED,
    AUDIT_CONTACT_UPDATED,
    CONTACT_EVENT_ERASED,
    CONTACT_EVENT_EXPORTED,
)
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.services import audit
from app.services.contacts import validate_attributes

logger = get_logger(__name__)

router = APIRouter(
    prefix="/contacts",
    tags=["contacts"],
    dependencies=[require_role("owner", "admin", "member")],
)


class ContactOut(BaseModel):
    id: UUID
    external_id: str | None = None
    email: str | None = None
    phone: str | None = None
    name: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    source: str = "api"
    verified: bool = False
    verified_at: datetime | None = None
    external_user_refs: list[str] = Field(default_factory=list)
    last_seen_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ContactEventOut(BaseModel):
    id: UUID
    conversation_id: UUID | None = None
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    at: datetime | None = None


class ContactConversationOut(BaseModel):
    id: UUID
    channel: str | None = None
    started_at: datetime | None = None
    identity_verified: bool = False


class ContactDetailOut(ContactOut):
    events: list[ContactEventOut] = Field(default_factory=list)
    conversations: list[ContactConversationOut] = Field(default_factory=list)


class ContactUpdate(BaseModel):
    """The three editable fields. `external_id` and `verified_at` are absent by
    design, not by omission."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=40)
    attributes: dict[str, Any] | None = None


class ContactExportOut(BaseModel):
    contact: ContactOut
    events: list[ContactEventOut] = Field(default_factory=list)
    conversation_ids: list[UUID] = Field(default_factory=list)
    exported_at: datetime


def _contact_out(row: dict[str, Any]) -> ContactOut:
    return ContactOut(
        id=row["id"],
        external_id=row.get("external_id"),
        email=row.get("email"),
        phone=row.get("phone"),
        name=row.get("name"),
        attributes=dict(row.get("attributes") or {}),
        source=str(row.get("source") or "api"),
        verified=row.get("verified_at") is not None,
        verified_at=row.get("verified_at"),
        external_user_refs=list(row.get("external_user_refs") or []),
        last_seen_at=row.get("last_seen_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _event_out(row: dict[str, Any]) -> ContactEventOut:
    return ContactEventOut(
        id=row["id"],
        conversation_id=row.get("conversation_id"),
        kind=str(row.get("kind") or ""),
        payload=dict(row.get("payload") or {}),
        at=row.get("at"),
    )


@router.get("", response_model=list[ContactOut])
async def list_contacts(
    tenant: TenantDep,
    container: ContainerDep,
    q: Annotated[str, Query(max_length=200)] = "",
    verified: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ContactOut]:
    """Search contacts by email, phone, name or the tenant's own user id."""
    rows = await container.contacts_repo.search(
        tenant.tenant_id, query=q, verified=verified, limit=limit, offset=offset
    )
    return [_contact_out(row) for row in rows]


@router.get("/{contact_id}", response_model=ContactDetailOut)
async def get_contact(
    contact_id: UUID, tenant: TenantDep, container: ContainerDep
) -> ContactDetailOut:
    """One contact with its recent threads and its event history."""
    repo = container.contacts_repo
    row = await repo.get(tenant.tenant_id, contact_id)
    if row is None:
        raise NotFoundError("no such contact")

    events = await repo.list_events(tenant.tenant_id, contact_id, limit=50)
    threads = await repo.conversations_for(tenant.tenant_id, contact_id, limit=20)
    base = _contact_out(row)
    return ContactDetailOut(
        **base.model_dump(),
        events=[_event_out(event) for event in events],
        conversations=[
            ContactConversationOut(
                id=thread["id"],
                channel=str(thread.get("channel") or ""),
                started_at=thread.get("started_at"),
                identity_verified=bool(thread.get("identity_verified")),
            )
            for thread in threads
        ],
    )


@router.patch("/{contact_id}", response_model=ContactOut)
async def update_contact(
    contact_id: UUID,
    payload: ContactUpdate,
    tenant: TenantDep,
    container: ContainerDep,
) -> ContactOut:
    """Edit `name`, `phone` and `attributes`. Nothing else.

    `attributes` is validated here rather than only by the database's CHECK
    because the message has to name the offending key: these values are
    interpolated into URL paths, and "attributes must be flat" is not actionable
    without knowing which one was not.
    """
    attributes = (
        validate_attributes(
            payload.attributes,
            max_keys=container.settings.CONTACT_MAX_ATTRIBUTES,
            max_value_chars=container.settings.CONTACT_ATTRIBUTE_MAX_LENGTH,
        )
        if payload.attributes is not None
        else None
    )

    row = await container.contacts_repo.update(
        tenant.tenant_id,
        contact_id,
        name=payload.name,
        phone=payload.phone,
        attributes=attributes,
    )
    if row is None:
        raise NotFoundError("no such contact")

    await audit.record(
        AUDIT_CONTACT_UPDATED,
        actor_type="user",
        actor_id=tenant.user_id,
        target_tenant=tenant.tenant_id,
        target_type="contact",
        target_id=str(contact_id),
        detail={"fields": sorted(payload.model_dump(exclude_none=True).keys())},
    )
    return _contact_out(row)


@router.delete(
    "/{contact_id}", status_code=204, dependencies=[require_role("owner")]
)
async def delete_contact(
    contact_id: UUID, tenant: TenantDep, container: ContainerDep
) -> None:
    """GDPR erasure. Owner only.

    The row goes; `conversations.contact_id` becomes NULL by FK, so the threads
    survive as anonymous rather than disappearing along with the person — a
    transcript is the tenant's own record of a conversation it took part in.
    """
    repo = container.contacts_repo
    row = await repo.get(tenant.tenant_id, contact_id)
    if row is None:
        raise NotFoundError("no such contact")

    # The event is written before the delete because `contact_events.contact_id`
    # cascades: after the row is gone there is nothing to hang it on. The audit
    # row is the durable record.
    await repo.record_event(
        tenant.tenant_id, contact_id, kind=CONTACT_EVENT_ERASED, payload={}
    )
    deleted = await repo.delete(tenant.tenant_id, contact_id)
    if not deleted:
        raise NotFoundError("no such contact")

    await audit.record(
        AUDIT_CONTACT_DELETED,
        actor_type="user",
        actor_id=tenant.user_id,
        target_tenant=tenant.tenant_id,
        target_type="contact",
        target_id=str(contact_id),
        detail={"had_external_id": row.get("external_id") is not None},
    )


@router.get("/{contact_id}/export", response_model=ContactExportOut)
async def export_contact(
    contact_id: UUID, tenant: TenantDep, container: ContainerDep
) -> ContactExportOut:
    """Everything stored about one person, for a subject access request.

    Conversation ids only, not transcripts: the messages are exported by the
    conversations endpoints, and duplicating them here would make one response
    unbounded in the size of somebody's chat history.
    """
    repo = container.contacts_repo
    row = await repo.get(tenant.tenant_id, contact_id)
    if row is None:
        raise NotFoundError("no such contact")

    events = await repo.list_events(tenant.tenant_id, contact_id, limit=200)
    threads = await repo.conversations_for(tenant.tenant_id, contact_id, limit=200)

    await repo.record_event(
        tenant.tenant_id, contact_id, kind=CONTACT_EVENT_EXPORTED, payload={}
    )
    await audit.record(
        AUDIT_CONTACT_EXPORTED,
        actor_type="user",
        actor_id=tenant.user_id,
        target_tenant=tenant.tenant_id,
        target_type="contact",
        target_id=str(contact_id),
        detail={"events": len(events), "conversations": len(threads)},
    )

    return ContactExportOut(
        contact=_contact_out(row),
        events=[_event_out(event) for event in events],
        conversation_ids=[thread["id"] for thread in threads],
        exported_at=datetime.now(UTC),
    )
