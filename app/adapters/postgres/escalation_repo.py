"""Postgres persistence for human escalations.

One table, ``escalations`` (``migrations/007_escalation.sql``), everything under
``tenant_scope`` — an escalation always belongs to a known tenant, so nothing here
needs ``admin_scope``.

The dedupe key is the interesting part. ``reserve`` is an
``INSERT ... ON CONFLICT DO NOTHING RETURNING id`` against
``escalations_dedupe_unique (tenant_id, dedupe_key)``, so the cooldown is a
database guarantee rather than a cache TTL. A cache-based cooldown fails open
during a Redis blip, and failing open here means paging a support team a hundred
times for one broken document.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from app.core.db import tenant_scope
from app.domain.escalation import (
    CONTACT_SOURCE_UNKNOWN,
    EscalationState,
    EscalationTrigger,
    SourceRef,
)
from sqlalchemy import text
from sqlalchemy.engine import RowMapping

_COLUMNS = """
    id, tenant_id, agent_id, conversation_id, message_id, trigger, channel, state,
    dedupe_key, confidence, question, answer, considered, contact_ciphertext,
    contact_masked, contact_source, external_user_ref, page_url, note, receipts,
    created_at, notified_at, updated_at
"""

_RESERVE_SQL = f"""
    INSERT INTO escalations (
        id, tenant_id, agent_id, conversation_id, message_id, trigger, channel,
        state, dedupe_key, confidence, question, answer, considered,
        contact_ciphertext, contact_masked, contact_source, external_user_ref,
        page_url, note
    ) VALUES (
        :id, :tenant_id, :agent_id, :conversation_id, :message_id, :trigger,
        :channel, 'pending', :dedupe_key, :confidence, :question, :answer,
        CAST(:considered AS jsonb), :contact_ciphertext, :contact_masked,
        :contact_source, :external_user_ref, :page_url, :note
    )
    ON CONFLICT ON CONSTRAINT escalations_dedupe_unique DO NOTHING
    RETURNING {_COLUMNS}
"""
_GET_SQL = f"SELECT {_COLUMNS} FROM escalations WHERE id = :id"
_MARK_NOTIFIED_SQL = """
    UPDATE escalations
       SET state = 'notified',
           receipts = CAST(:receipts AS jsonb),
           notified_at = now(),
           updated_at = now()
     WHERE id = :id
"""
_MARK_FAILED_SQL = """
    UPDATE escalations
       SET state = 'failed',
           receipts = CAST(:receipts AS jsonb),
           note = COALESCE(:note, note),
           updated_at = now()
     WHERE id = :id
"""
_SET_CONTACT_SQL = f"""
    UPDATE escalations
       SET contact_ciphertext = :contact_ciphertext,
           contact_masked = :contact_masked,
           contact_source = :contact_source,
           updated_at = now()
     WHERE id = :id AND state = 'pending'
    RETURNING {_COLUMNS}
"""
_FILTERS = """
     WHERE (:state IS NULL OR state = :state)
       AND (:trigger IS NULL OR trigger = :trigger)
       AND (:channel IS NULL OR channel = :channel)
       AND (:conversation_id IS NULL OR conversation_id = :conversation_id)
       AND (CAST(:since AS timestamptz) IS NULL OR created_at >= CAST(:since AS timestamptz))
"""
_LIST_SQL = f"""
    SELECT {_COLUMNS} FROM escalations
    {_FILTERS}
     ORDER BY created_at DESC
     LIMIT :limit OFFSET :offset
"""
_COUNT_SQL = f"""
    SELECT count(*) AS total FROM escalations
    {_FILTERS}
"""
# The hourly cap. Counted from the table rather than a counter key for the same
# reason the dedupe is a constraint: a rate limit that resets when the cache
# restarts is not a rate limit.
_RATE_SQL = """
    SELECT count(*) AS used FROM escalations
     WHERE created_at >= now() - make_interval(hours => :hours)
       AND state <> 'suppressed'
"""
_RATE_BY_DAY_SQL = """
    SELECT date_trunc('day', created_at) AS day, trigger, count(*) AS total
      FROM escalations
     WHERE created_at >= now() - make_interval(days => :days)
     GROUP BY 1, 2
     ORDER BY 1
"""
_PURGE_SQL = """
    DELETE FROM escalations
     WHERE ctid IN (
         SELECT ctid FROM escalations
          WHERE created_at < now() - make_interval(days => :days)
          LIMIT :batch
     )
"""


def _load_json(value: Any) -> Any:
    """JSONB comes back as text from asyncpg unless a codec is registered."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _considered_json(considered: tuple[SourceRef, ...]) -> str:
    return json.dumps(
        [
            {
                "filename": ref.filename,
                "page_from": ref.page_from,
                "score": round(ref.score, 4),
                "document_id": str(ref.document_id),
            }
            for ref in considered
        ]
    )


@dataclass(slots=True)
class EscalationRow:
    """One stored escalation, as the table holds it.

    A persistence projection rather than a domain type: it carries the encrypted
    contact and the raw JSONB, and the service turns it into an
    ``EscalationEvent`` after decrypting. Keeping the two apart is what stops a
    notifier from accidentally rendering ciphertext.
    """

    id: UUID
    tenant_id: UUID
    trigger: str
    channel: str
    state: str
    dedupe_key: str
    question: str
    agent_id: UUID | None = None
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    confidence: float = 0.0
    answer: str | None = None
    considered: list[dict[str, Any]] = field(default_factory=list)
    contact_ciphertext: str | None = None
    contact_masked: str | None = None
    contact_source: str = CONTACT_SOURCE_UNKNOWN
    external_user_ref: str | None = None
    page_url: str | None = None
    note: str | None = None
    receipts: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime | None = None
    notified_at: datetime | None = None
    updated_at: datetime | None = None


def _to_row(row: RowMapping) -> EscalationRow:
    considered = _load_json(row["considered"]) or []
    receipts = _load_json(row["receipts"]) or []
    return EscalationRow(
        id=row["id"],
        tenant_id=row["tenant_id"],
        trigger=row["trigger"],
        channel=row["channel"],
        state=row["state"],
        dedupe_key=row["dedupe_key"],
        question=row["question"] or "",
        agent_id=row["agent_id"],
        conversation_id=row["conversation_id"],
        message_id=row["message_id"],
        confidence=float(row["confidence"] or 0.0),
        answer=row["answer"],
        considered=list(considered) if isinstance(considered, list) else [],
        contact_ciphertext=row["contact_ciphertext"],
        contact_masked=row["contact_masked"],
        contact_source=row["contact_source"] or CONTACT_SOURCE_UNKNOWN,
        external_user_ref=row["external_user_ref"],
        page_url=row["page_url"],
        note=row["note"],
        receipts=list(receipts) if isinstance(receipts, list) else [],
        created_at=row["created_at"],
        notified_at=row["notified_at"],
        updated_at=row["updated_at"],
    )


class EscalationRepository:
    """All escalation state. Raw SQL through SQLAlchemy Core, no ORM."""

    async def reserve(
        self,
        tenant_id: UUID,
        *,
        trigger: EscalationTrigger | str,
        channel: str,
        dedupe_key: str,
        question: str,
        confidence: float,
        agent_id: UUID | None = None,
        conversation_id: UUID | None = None,
        message_id: UUID | None = None,
        answer: str | None = None,
        considered: tuple[SourceRef, ...] = (),
        contact_ciphertext: str | None = None,
        contact_masked: str | None = None,
        contact_source: str = CONTACT_SOURCE_UNKNOWN,
        external_user_ref: str | None = None,
        page_url: str | None = None,
        note: str | None = None,
    ) -> EscalationRow | None:
        """Claim the right to escalate. None means the cooldown already holds one.

        The row lands in state `pending`; notification happens afterwards and
        outside the transaction, so a slow Slack webhook never holds a database
        connection open.
        """
        params = {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "trigger": str(trigger),
            "channel": channel,
            "dedupe_key": dedupe_key,
            "confidence": float(confidence),
            "question": question[:8000],
            "answer": answer[:20000] if answer else None,
            "considered": _considered_json(considered),
            "contact_ciphertext": contact_ciphertext,
            "contact_masked": contact_masked,
            "contact_source": contact_source,
            "external_user_ref": external_user_ref,
            "page_url": page_url[:2000] if page_url else None,
            "note": note[:2000] if note else None,
        }
        async with tenant_scope(tenant_id) as session:
            row = (await session.execute(text(_RESERVE_SQL), params)).mappings().first()
        return None if row is None else _to_row(row)

    async def get(self, tenant_id: UUID, escalation_id: UUID) -> EscalationRow | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                (await session.execute(text(_GET_SQL), {"id": escalation_id}))
                .mappings()
                .first()
            )
        return None if row is None else _to_row(row)

    async def mark_notified(
        self, tenant_id: UUID, escalation_id: UUID, receipts: list[dict[str, Any]]
    ) -> None:
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(_MARK_NOTIFIED_SQL),
                {"id": escalation_id, "receipts": json.dumps(receipts)},
            )

    async def mark_failed(
        self,
        tenant_id: UUID,
        escalation_id: UUID,
        receipts: list[dict[str, Any]],
        *,
        note: str | None = None,
    ) -> None:
        """Every notifier failed. The row stays as the durable record of the miss."""
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(_MARK_FAILED_SQL),
                {
                    "id": escalation_id,
                    "receipts": json.dumps(receipts),
                    "note": note[:2000] if note else None,
                },
            )

    async def set_contact(
        self,
        tenant_id: UUID,
        escalation_id: UUID,
        *,
        contact_ciphertext: str,
        contact_masked: str,
        contact_source: str,
    ) -> EscalationRow | None:
        """Attach a contact collected after the fact.

        Restricted to `state = 'pending'` in the WHERE clause: once the mail has
        gone out, editing the row cannot change what a human already read, and
        allowing it would make the audit trail lie.
        """
        params = {
            "id": escalation_id,
            "contact_ciphertext": contact_ciphertext,
            "contact_masked": contact_masked,
            "contact_source": contact_source,
        }
        async with tenant_scope(tenant_id) as session:
            row = (await session.execute(text(_SET_CONTACT_SQL), params)).mappings().first()
        return None if row is None else _to_row(row)

    async def list_for_tenant(
        self,
        tenant_id: UUID,
        *,
        state: str | None = None,
        trigger: str | None = None,
        channel: str | None = None,
        conversation_id: UUID | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EscalationRow], int]:
        params: dict[str, Any] = {
            "state": state,
            "trigger": trigger,
            "channel": channel,
            "conversation_id": conversation_id,
            "since": since,
        }
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(_LIST_SQL),
                        {**params, "limit": min(max(limit, 1), 200), "offset": max(offset, 0)},
                    )
                )
                .mappings()
                .all()
            )
            total_row = (await session.execute(text(_COUNT_SQL), params)).mappings().first()
        total = int(total_row["total"]) if total_row else 0
        return [_to_row(row) for row in rows], total

    async def count_recent(self, tenant_id: UUID, hours: int = 1) -> int:
        """Escalations in the last `hours`, for the hourly cap."""
        async with tenant_scope(tenant_id) as session:
            row = (
                (await session.execute(text(_RATE_SQL), {"hours": hours})).mappings().first()
            )
        return int(row["used"]) if row else 0

    async def rate_by_day(self, tenant_id: UUID, days: int = 30) -> list[dict[str, Any]]:
        """Daily counts per trigger. What the dashboard chart reads."""
        async with tenant_scope(tenant_id) as session:
            rows = (
                (await session.execute(text(_RATE_BY_DAY_SQL), {"days": days}))
                .mappings()
                .all()
            )
        return [
            {
                "day": row["day"].date().isoformat() if row["day"] else None,
                "trigger": row["trigger"],
                "total": int(row["total"]),
            }
            for row in rows
        ]

    async def purge_older_than(self, tenant_id: UUID, days: int, *, batch: int = 1000) -> int:
        """Retention sweep, one tenant, one bounded batch."""
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(text(_PURGE_SQL), {"days": days, "batch": batch})
        return int(getattr(result, "rowcount", 0) or 0)

    @staticmethod
    def pending_state() -> str:
        """The state a reserved row starts in. Exposed so a caller need not import
        the enum just to compare one string."""
        return str(EscalationState.PENDING)
