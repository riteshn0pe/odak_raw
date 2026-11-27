"""Conversation and message persistence.

Two layers, kept separate on purpose:

  checkpoints        LangGraph's own tables, holding mid-run graph state. They
                     exist so a crashed or interrupted turn can resume, and they
                     expire — they are operational state, not a record.
  conversations      This module. The durable transcript a tenant reads,
  + messages         exports, and runs analytics over. Survives checkpoint expiry.

Conflating the two is the usual mistake: it either loses the product record when
checkpoints expire, or keeps graph internals alive forever.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.constants import Channel, MessageRole
from app.core.db import tenant_scope
from app.core.logging import get_logger
from app.domain.entities import Citation, Conversation

logger = get_logger(__name__)


class ConversationRepository:
    async def get_or_create(
        self,
        tenant_id: UUID,
        agent_id: UUID,
        channel: Channel,
        external_user_ref: str | None = None,
        conversation_id: UUID | None = None,
    ) -> Conversation:
        """Resolve the thread this turn belongs to.

        An explicit id wins. Otherwise a known external reference resumes that
        person's existing thread — this is what makes a returning WhatsApp number
        or an identified web visitor pick up where they left off instead of
        starting cold.
        """
        async with tenant_scope(tenant_id) as session:
            row = None
            if conversation_id is not None:
                row = (
                    await session.execute(
                        text("SELECT * FROM conversations WHERE id = :id"),
                        {"id": conversation_id},
                    )
                ).mappings().first()
            elif external_user_ref:
                row = (
                    await session.execute(
                        text(
                            "SELECT * FROM conversations "
                            "WHERE channel = :channel AND external_user_ref = :ref "
                            "ORDER BY last_activity DESC LIMIT 1"
                        ),
                        {"channel": channel.value, "ref": external_user_ref},
                    )
                ).mappings().first()

            if row is not None:
                return Conversation(
                    id=row["id"],
                    tenant_id=tenant_id,
                    agent_id=row["agent_id"],
                    channel=Channel(row["channel"]),
                    external_user_ref=row["external_user_ref"],
                    summary=row["summary"],
                    turn_count=row["turn_count"],
                    started_at=row["started_at"],
                    last_activity=row["last_activity"],
                )

            new_id = conversation_id or uuid4()
            await session.execute(
                text(
                    "INSERT INTO conversations (id, tenant_id, agent_id, channel, external_user_ref) "
                    "VALUES (:id, :tenant_id, :agent_id, :channel, :ref)"
                ),
                {
                    "id": new_id,
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "channel": channel.value,
                    "ref": external_user_ref,
                },
            )

        return Conversation(
            id=new_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            channel=channel,
            external_user_ref=external_user_ref,
        )

    async def add_message(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        role: MessageRole,
        content: str,
        citations: list[Citation] | None = None,
        confidence: float | None = None,
        latency_ms: int | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        model: str | None = None,
    ) -> UUID:
        message_id = uuid4()
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO messages (
                        id, tenant_id, conversation_id, role, content, citations,
                        confidence, latency_ms, tokens_in, tokens_out, model
                    ) VALUES (
                        :id, :tenant_id, :conversation_id, :role, :content,
                        CAST(:citations AS jsonb), :confidence, :latency_ms,
                        :tokens_in, :tokens_out, :model
                    )
                    """
                ),
                {
                    "id": message_id,
                    "tenant_id": tenant_id,
                    "conversation_id": conversation_id,
                    "role": role.value,
                    "content": content,
                    "citations": json.dumps(
                        [_citation_json(citation) for citation in (citations or [])]
                    ),
                    "confidence": confidence,
                    "latency_ms": latency_ms,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "model": model,
                },
            )
            # turn_count drives the rolling-summary cadence, so it advances on the
            # assistant message — one exchange, not two
            await session.execute(
                text(
                    "UPDATE conversations "
                    "SET last_activity = now(), "
                    "    turn_count = turn_count + CASE WHEN :role = 'assistant' THEN 1 ELSE 0 END "
                    "WHERE id = :id"
                ),
                {"id": conversation_id, "role": role.value},
            )
        return message_id

    async def recent_messages(
        self, tenant_id: UUID, conversation_id: UUID, limit: int = 12
    ) -> list[dict[str, Any]]:
        """Last N messages in chronological order.

        Fetched newest-first so the limit takes the *recent* end, then reversed
        so the model reads them forward.

        `id` is projected because callers need to point at a specific turn, not
        only read it: the resume path matches a client-supplied anchor against
        it, and an action call records the message it was dispatched from. Every
        consumer reads the rows by key, so the extra column is additive.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, role, content, citations, created_at FROM messages "
                        "WHERE conversation_id = :id ORDER BY created_at DESC LIMIT :limit"
                    ),
                    {"id": conversation_id, "limit": limit},
                )
            ).mappings().all()
        return [dict(row) for row in reversed(rows)]

    async def set_summary(self, tenant_id: UUID, conversation_id: UUID, summary: str) -> None:
        """Store the rolling summary of turns that fell out of the window.

        This is what stops a long conversation from either exploding the prompt
        or silently forgetting its own beginning.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text("UPDATE conversations SET summary = :summary WHERE id = :id"),
                {"summary": summary, "id": conversation_id},
            )

    async def record(
        self, tenant_id: UUID, kind: str, quantity: int, **meta: Any
    ) -> None:
        """Append one usage event of any kind.

        The generic form the telemetry adapter calls. `record_query_usage` below
        is the three-event shorthand for an answered question, which is the only
        place that always writes the same trio.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO usage_events (tenant_id, kind, quantity, meta) "
                    "VALUES (:tenant_id, :kind, :quantity, CAST(:meta AS jsonb))"
                ),
                {
                    "tenant_id": tenant_id,
                    "kind": kind,
                    "quantity": quantity,
                    "meta": json.dumps(_jsonable(meta)),
                },
            )

    async def record_query_usage(
        self,
        tenant_id: UUID,
        tokens_in: int,
        tokens_out: int,
        model: str,
        conversation_id: UUID | None = None,
    ) -> None:
        meta = json.dumps(
            {"model": model, "conversation_id": str(conversation_id) if conversation_id else None}
        )
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO usage_events (tenant_id, kind, quantity, meta) VALUES "
                    "(:tenant_id, 'query', 1, CAST(:meta AS jsonb)), "
                    "(:tenant_id, 'llm_token_in', :tokens_in, CAST(:meta AS jsonb)), "
                    "(:tenant_id, 'llm_token_out', :tokens_out, CAST(:meta AS jsonb))"
                ),
                {
                    "tenant_id": tenant_id,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "meta": meta,
                },
            )

    async def set_feedback(
        self, tenant_id: UUID, message_id: UUID, feedback: str
    ) -> None:
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text("UPDATE messages SET feedback = :feedback WHERE id = :id"),
                {"feedback": feedback, "id": message_id},
            )

    async def list_conversations(
        self, tenant_id: UUID, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, agent_id, channel, external_user_ref, turn_count, "
                        "started_at, last_activity FROM conversations "
                        "ORDER BY last_activity DESC LIMIT :limit OFFSET :offset"
                    ),
                    {"limit": limit, "offset": offset},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def count_conversations(self, tenant_id: UUID) -> int:
        async with tenant_scope(tenant_id) as session:
            total = (
                await session.execute(text("SELECT count(*) FROM conversations"))
            ).scalar_one()
        return int(total)

    async def usage_summary(
        self, tenant_id: UUID, since: Any = None
    ) -> list[dict[str, Any]]:
        """Quantities by kind, the substrate for the analytics dashboard.

        Aggregated on read rather than materialised: `usage_events` is
        append-only and this is a plan-quota-sized query, not a table scan over
        a tenant's full history.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT kind, sum(quantity) AS quantity, count(*) AS events "
                        "FROM usage_events "
                        "WHERE (:since IS NULL OR ts >= :since) "
                        "GROUP BY kind ORDER BY kind"
                    ),
                    {"since": since},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def message_stats(self, tenant_id: UUID, since: Any = None) -> dict[str, Any]:
        """Latency, feedback, and volume — the numbers a support lead checks daily."""
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT count(*) FILTER (WHERE role = 'assistant') AS answers,
                               avg(latency_ms) FILTER (WHERE role = 'assistant') AS avg_latency_ms,
                               avg(confidence) FILTER (WHERE role = 'assistant') AS avg_confidence,
                               count(*) FILTER (WHERE feedback = 'up') AS thumbs_up,
                               count(*) FILTER (WHERE feedback = 'down') AS thumbs_down
                          FROM messages
                         WHERE (:since IS NULL OR created_at >= :since)
                        """
                    ),
                    {"since": since},
                )
            ).mappings().one()
        return dict(row)

    async def low_confidence_answers(
        self,
        tenant_id: UUID,
        threshold: float = 0.45,
        limit: int = 20,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Questions the agent could not answer confidently.

        The highest-value analytics screen per `productisation.md` §4: it tells a
        tenant exactly which document to upload next. Pairs each low-confidence
        assistant turn with the user question immediately before it in the same
        conversation. `since` bounds the window so the dashboard's day selector is
        meaningful; the filter is on the assistant turn's `created_at`.
        """
        params: dict[str, Any] = {"threshold": threshold, "limit": limit}
        since_clause = ""
        if since is not None:
            since_clause = "AND created_at >= :since"
            params["since"] = since
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT prior_content AS question, confidence, created_at FROM (
                            SELECT content,
                                   confidence,
                                   created_at,
                                   role,
                                   lag(content) OVER (
                                       PARTITION BY conversation_id ORDER BY created_at
                                   ) AS prior_content,
                                   lag(role) OVER (
                                       PARTITION BY conversation_id ORDER BY created_at
                                   ) AS prior_role
                              FROM messages
                        ) paired
                        WHERE role = 'assistant' AND prior_role = 'user'
                          AND confidence IS NOT NULL AND confidence < :threshold
                          {since_clause}
                        ORDER BY created_at DESC
                        LIMIT :limit
                        """
                    ),
                    params,
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def transcript(
        self, tenant_id: UUID, conversation_id: UUID
    ) -> list[dict[str, Any]]:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, role, content, citations, confidence, latency_ms, "
                        "tokens_in, tokens_out, model, feedback, created_at FROM messages "
                        "WHERE conversation_id = :id ORDER BY created_at"
                    ),
                    {"id": conversation_id},
                )
            ).mappings().all()
        return [dict(row) for row in rows]


def _citation_json(citation: Citation) -> dict[str, Any]:
    payload = asdict(citation)
    payload["document_id"] = str(citation.document_id)
    return payload


def _jsonable(value: Any) -> Any:
    """Coerce arbitrary metadata into something json can serialise.

    Callers pass UUIDs, enums, and datetimes freely. Stringifying whatever json
    cannot handle keeps a telemetry call from failing on a type it never
    promised to accept.
    """
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
