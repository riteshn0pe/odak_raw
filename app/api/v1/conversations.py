"""Conversation history endpoints.

Read-only transcript access for anyone authenticated for a tenant. There is no
write path here; feedback and message creation happen through the chat flow.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import ContainerDep, TenantDep
from app.api.schemas import ConversationOut, MessageOut
from app.core.db import tenant_scope
from app.core.errors import NotFoundError

router = APIRouter(prefix="/conversations", tags=["conversations"])


class ConversationListOut(BaseModel):
    items: list[ConversationOut]
    total: int


class ConversationDetailOut(BaseModel):
    conversation: ConversationOut
    messages: list[MessageOut]


@router.get("", response_model=ConversationListOut)
async def list_conversations(
    tenant: TenantDep,
    container: ContainerDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ConversationListOut:
    rows = await container.conversations.list_conversations(tenant.tenant_id, limit, offset)
    total = await container.conversations.count_conversations(tenant.tenant_id)
    return ConversationListOut(
        items=[ConversationOut(**row) for row in rows],
        total=total,
    )


@router.get("/{conversation_id}", response_model=ConversationDetailOut)
async def get_conversation(
    conversation_id: UUID, tenant: TenantDep, container: ContainerDep
) -> ConversationDetailOut:
    # no single-conversation getter is exposed on the repository, so the
    # summary row is fetched directly here, scoped by RLS like everything else
    async with tenant_scope(tenant.tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT id, agent_id, channel, external_user_ref, turn_count, "
                    "started_at, last_activity FROM conversations WHERE id = :id"
                ),
                {"id": conversation_id},
            )
        ).mappings().first()

    if row is None:
        raise NotFoundError("no such conversation")

    messages = await container.conversations.transcript(tenant.tenant_id, conversation_id)

    return ConversationDetailOut(
        conversation=ConversationOut(**row),
        messages=[MessageOut(**message) for message in messages],
    )
