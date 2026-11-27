"""Postgres persistence for email channels, replay receipts, threads and delivery state.

Two scoping rules, and the first one is the interesting one.

**Routing runs before a tenant is known.** ``whatsapp_integrations`` carries no
RLS at all (``migrations/003_whatsapp.sql`` FORCEs only the two receipt tables),
which is what lets ``WhatsAppRepository.resolve_active_by_phone_number_id`` read
it under ``admin_scope``. ``email_channels`` is not built that way: migration 008
puts ``ENABLE`` + ``FORCE ROW LEVEL SECURITY`` and a ``tenant_isolation`` policy
on it, and under ``admin_scope`` ``current_tenant_id()`` is NULL, so that policy
denies every row. A bare ``admin_scope`` SELECT would silently resolve nothing.

So the tenant is discovered first and the channel is read second:

1. the provisioned address is ``<slug>-<8 hex>@<EMAIL_INBOUND_DOMAIN>``, so the
   local part names the tenant. ``tenants`` is a platform table and is exactly
   what ``admin_scope`` is documented for — "tenant lookup during request
   resolution, before a tenant is known".
2. with the tenant id in hand, the channel row is read under ``tenant_scope``,
   where the policy is satisfied.

No policy is weakened and no second mechanism is introduced: this is the WhatsApp
arrangement — admin_scope for the platform-level lookup, tenant_scope for
tenant-owned data — applied to a table that, unlike WhatsApp's, is RLS-protected.
Every other method here uses ``tenant_scope``.
"""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID, uuid4

from app.core.db import admin_scope, tenant_scope
from app.core.logging import get_logger
from app.domain.email_channel import (
    EmailChannel,
    EmailChannelStatus,
    EmailDeliveryUpdate,
    EmailInboundReceipt,
    EmailReceiptState,
    EmailThread,
    delivery_rank,
)
from app.domain.email_text import mask_address, normalise_address, strip_plus_tag
from sqlalchemy import text
from sqlalchemy.engine import RowMapping

logger = get_logger(__name__)

# Values are joined with this before being handed to string_to_array. A literal
# array parameter would have to be typed for asyncpg; a delimiter that cannot
# appear in an RFC 5322 address or msg-id needs no type at all.
ARRAY_DELIMITER = "\x01"

# <slug>-<8 hex>. The suffix is what stops an outsider mailing a tenant's agent
# directly from a guessed slug, bypassing the tenant's own inbox and filters.
_ADDRESS_LOCAL = re.compile(r"^(?P<slug>.+)-(?P<suffix>[0-9a-f]{8})$", re.IGNORECASE)

_CHANNEL_COLUMNS = """
    id, tenant_id, agent_id, address, forward_from, sending_domain,
    sending_domain_verified_at, display_name, allowed_forwarders,
    ingest_attachments, link_sources, audit_sends_remaining, status,
    error_reason, created_at, updated_at, disconnected_at
"""
_THREAD_COLUMNS = """
    id, tenant_id, channel_id, conversation_id, external_user_ref,
    participant_ciphertext, participant_masked, subject_normalised, message_ids,
    last_message_id, reply_seq, agent_reply_count, deliverable, created_at,
    last_inbound_at, last_outbound_at, updated_at
"""

_TENANT_BY_SLUG_SQL = """
    SELECT id FROM tenants WHERE slug = :slug AND status = 'active'
"""
_ACTIVE_CHANNEL_BY_ADDRESS_SQL = f"""
    SELECT {_CHANNEL_COLUMNS}
      FROM email_channels
     WHERE address = :address AND status = 'active'
"""
_GET_CHANNEL_SQL = f"""
    SELECT {_CHANNEL_COLUMNS} FROM email_channels WHERE id = :channel_id
"""
_LIST_CHANNELS_SQL = f"""
    SELECT {_CHANNEL_COLUMNS} FROM email_channels
     WHERE status <> 'disconnected'
     ORDER BY created_at
"""
_CREATE_CHANNEL_SQL = f"""
    INSERT INTO email_channels (
        id, tenant_id, agent_id, address, forward_from, display_name,
        allowed_forwarders, ingest_attachments, link_sources, status
    ) VALUES (
        :id, :tenant_id, :agent_id, :address, :forward_from, :display_name,
        COALESCE(string_to_array(NULLIF(:allowed_forwarders, ''), :delimiter),
                 ARRAY[]::TEXT[]),
        :ingest_attachments, :link_sources, :status
    )
    RETURNING {_CHANNEL_COLUMNS}
"""
_UPDATE_CHANNEL_SQL = f"""
    UPDATE email_channels
       SET agent_id = COALESCE(:agent_id, agent_id),
           address = COALESCE(:address, address),
           forward_from = CASE WHEN :has_forward_from THEN :forward_from
                               ELSE forward_from END,
           display_name = CASE WHEN :has_display_name THEN :display_name
                               ELSE display_name END,
           sending_domain = CASE WHEN :has_sending_domain THEN :sending_domain
                                 ELSE sending_domain END,
           sending_domain_verified_at = CASE
               WHEN :verified THEN now()
               WHEN :has_sending_domain THEN NULL
               ELSE sending_domain_verified_at
           END,
           allowed_forwarders = CASE
               WHEN :has_allowed_forwarders THEN COALESCE(
                   string_to_array(NULLIF(:allowed_forwarders, ''), :delimiter),
                   ARRAY[]::TEXT[])
               ELSE allowed_forwarders
           END,
           ingest_attachments = COALESCE(:ingest_attachments, ingest_attachments),
           link_sources = COALESCE(:link_sources, link_sources),
           audit_sends_remaining = COALESCE(:audit_sends_remaining, audit_sends_remaining),
           status = COALESCE(:status, status),
           error_reason = CASE
               WHEN :has_error_reason THEN :error_reason
               WHEN :status = 'active' THEN NULL
               ELSE error_reason
           END,
           disconnected_at = CASE
               WHEN :status = 'disconnected' THEN now()
               WHEN :status = 'active' THEN NULL
               ELSE disconnected_at
           END,
           updated_at = now()
     WHERE id = :channel_id
    RETURNING {_CHANNEL_COLUMNS}
"""
_CONSUME_AUDIT_SEND_SQL = """
    UPDATE email_channels
       SET audit_sends_remaining = audit_sends_remaining - 1, updated_at = now()
     WHERE id = :channel_id AND audit_sends_remaining > 0
    RETURNING audit_sends_remaining
"""

# The insert IS the replay guard. A read-then-write races a webhook redelivery,
# which is the exact case this table exists for.
_RESERVE_INBOUND_SQL = """
    INSERT INTO email_inbound_receipts (
        id, tenant_id, channel_id, message_id, provider_id, state, received_at
    ) VALUES (
        :id, :tenant_id, :channel_id, :message_id, :provider_id, 'reserved',
        COALESCE(:received_at, now())
    )
    ON CONFLICT ON CONSTRAINT email_inbound_receipts_message_unique DO NOTHING
    RETURNING id, tenant_id, channel_id, message_id, provider_id, state,
              ignored_reason, received_at, processed_at, error
"""
_SET_RECEIPT_STATE_SQL = """
    UPDATE email_inbound_receipts
       SET state = :state,
           ignored_reason = COALESCE(:ignored_reason, ignored_reason),
           error = COALESCE(:error, error),
           processed_at = CASE WHEN :state = 'reserved' THEN processed_at ELSE now() END
     WHERE message_id = :message_id
"""

_THREAD_BY_MESSAGE_IDS_SQL = f"""
    SELECT {_THREAD_COLUMNS} FROM email_threads
     WHERE channel_id = :channel_id
       AND message_ids && string_to_array(:candidates, :delimiter)
     ORDER BY updated_at DESC
     LIMIT 1
"""
_THREAD_BY_ID_SQL = f"""
    SELECT {_THREAD_COLUMNS} FROM email_threads WHERE id = :thread_id
"""
_THREAD_BY_SUBJECT_SQL = f"""
    SELECT {_THREAD_COLUMNS} FROM email_threads
     WHERE channel_id = :channel_id
       AND external_user_ref = :external_user_ref
       AND subject_normalised = :subject_normalised
       AND updated_at >= now() - make_interval(hours => :window_hours)
     ORDER BY updated_at DESC
     LIMIT 1
"""
_THREAD_BY_PARTICIPANT_SQL = f"""
    SELECT {_THREAD_COLUMNS} FROM email_threads
     WHERE channel_id = :channel_id AND external_user_ref = :external_user_ref
     ORDER BY updated_at DESC
     LIMIT 1
"""
_CREATE_THREAD_SQL = f"""
    INSERT INTO email_threads (
        id, tenant_id, channel_id, conversation_id, external_user_ref,
        participant_ciphertext, participant_masked, subject_normalised,
        message_ids, last_message_id, last_inbound_at
    ) VALUES (
        :id, :tenant_id, :channel_id, :conversation_id, :external_user_ref,
        :participant_ciphertext, :participant_masked, :subject_normalised,
        COALESCE(string_to_array(NULLIF(:message_ids, ''), :delimiter),
                 ARRAY[]::TEXT[]),
        :last_message_id, now()
    )
    ON CONFLICT ON CONSTRAINT email_threads_conversation_unique DO UPDATE
       SET message_ids = (
               SELECT ARRAY(
                   SELECT DISTINCT unnest(
                       email_threads.message_ids || EXCLUDED.message_ids))),
           last_message_id = COALESCE(EXCLUDED.last_message_id,
                                      email_threads.last_message_id),
           last_inbound_at = now(),
           updated_at = now()
    RETURNING {_THREAD_COLUMNS}
"""
_APPEND_MESSAGE_ID_SQL = f"""
    UPDATE email_threads
       SET message_ids = CASE
               WHEN :message_id = ANY(message_ids) THEN message_ids
               ELSE array_append(message_ids, :message_id)
           END,
           last_message_id = :message_id,
           subject_normalised = CASE
               WHEN :has_subject THEN :subject_normalised ELSE subject_normalised END,
           last_inbound_at = CASE WHEN :inbound THEN now() ELSE last_inbound_at END,
           last_outbound_at = CASE WHEN :inbound THEN last_outbound_at ELSE now() END,
           reply_seq = CASE WHEN :inbound THEN reply_seq ELSE reply_seq + 1 END,
           agent_reply_count = CASE
               WHEN :inbound THEN agent_reply_count ELSE agent_reply_count + 1 END,
           updated_at = now()
     WHERE id = :thread_id
    RETURNING {_THREAD_COLUMNS}
"""
_NEXT_REPLY_SEQ_SQL = """
    SELECT reply_seq FROM email_threads WHERE id = :thread_id
"""
_MARK_UNDELIVERABLE_SQL = """
    UPDATE email_threads
       SET deliverable = false, updated_at = now()
     WHERE id = :thread_id
"""

_RECORD_DELIVERY_SQL = """
    INSERT INTO email_delivery_receipts (
        tenant_id, channel_id, provider_id, recipient_masked, status,
        bounce_type, error, provider_timestamp
    ) VALUES (
        :tenant_id, :channel_id, :provider_id, :recipient_masked, :status,
        :bounce_type, :error, :provider_timestamp
    )
    ON CONFLICT ON CONSTRAINT email_delivery_receipts_provider_unique DO UPDATE
       SET status = CASE
               WHEN :status_rank >= (CASE email_delivery_receipts.status
                   WHEN 'queued' THEN 0
                   WHEN 'sent' THEN 1
                   WHEN 'delivered' THEN 2
                   WHEN 'failed' THEN 3
                   WHEN 'bounced' THEN 4
                   WHEN 'complained' THEN 5
                   ELSE 0 END)
               THEN EXCLUDED.status
               ELSE email_delivery_receipts.status
           END,
           bounce_type = COALESCE(EXCLUDED.bounce_type,
                                  email_delivery_receipts.bounce_type),
           error = COALESCE(EXCLUDED.error, email_delivery_receipts.error),
           recipient_masked = COALESCE(EXCLUDED.recipient_masked,
                                       email_delivery_receipts.recipient_masked),
           provider_timestamp = COALESCE(EXCLUDED.provider_timestamp,
                                         email_delivery_receipts.provider_timestamp),
           updated_at = now()
"""
_ADD_SUPPRESSION_SQL = """
    INSERT INTO email_suppressions (tenant_id, address_hash, address_masked, reason)
    VALUES (:tenant_id, :address_hash, :address_masked, :reason)
    ON CONFLICT ON CONSTRAINT email_suppressions_unique DO NOTHING
"""
_IS_SUPPRESSED_SQL = """
    SELECT 1 FROM email_suppressions WHERE address_hash = :address_hash LIMIT 1
"""
_PURGE_RECEIPTS_SQL = """
    DELETE FROM email_inbound_receipts
     WHERE ctid IN (
         SELECT ctid FROM email_inbound_receipts
          WHERE tenant_id = :tenant_id
            AND received_at < now() - make_interval(days => :days)
          LIMIT :batch
     )
"""
_PURGE_DELIVERY_SQL = """
    DELETE FROM email_delivery_receipts
     WHERE ctid IN (
         SELECT ctid FROM email_delivery_receipts
          WHERE tenant_id = :tenant_id
            AND updated_at < now() - make_interval(days => :days)
          LIMIT :batch
     )
"""
_PURGE_CANDIDATE_TENANTS_SQL = """
    SELECT DISTINCT tenant_id FROM email_inbound_receipts
     WHERE received_at < now() - make_interval(days => :days)
     LIMIT :limit
"""


def slug_from_address(address: str) -> str | None:
    """The tenant slug encoded in a provisioned inbound address.

    ``acme-corp-9f2b1c04@agents.example.com`` -> ``acme-corp``. Returns None when
    the local part does not carry the random suffix, which means it is not one of
    our provisioned addresses and no tenant can own it.
    """
    local = strip_plus_tag(address).partition("@")[0]
    if not local:
        return None
    match = _ADDRESS_LOCAL.match(local)
    if match is None:
        return None
    return match.group("slug").lower()


def _to_channel(row: RowMapping) -> EmailChannel:
    return EmailChannel(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        address=row["address"],
        forward_from=row["forward_from"],
        sending_domain=row["sending_domain"],
        sending_domain_verified_at=row["sending_domain_verified_at"],
        display_name=row["display_name"],
        allowed_forwarders=list(row["allowed_forwarders"] or []),
        ingest_attachments=bool(row["ingest_attachments"]),
        link_sources=bool(row["link_sources"]),
        audit_sends_remaining=int(row["audit_sends_remaining"]),
        status=EmailChannelStatus(row["status"]),
        error_reason=row["error_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        disconnected_at=row["disconnected_at"],
    )


def _to_thread(row: RowMapping) -> EmailThread:
    return EmailThread(
        id=row["id"],
        tenant_id=row["tenant_id"],
        channel_id=row["channel_id"],
        conversation_id=row["conversation_id"],
        external_user_ref=row["external_user_ref"],
        participant_ciphertext=row["participant_ciphertext"],
        participant_masked=row["participant_masked"],
        subject_normalised=row["subject_normalised"] or "",
        message_ids=list(row["message_ids"] or []),
        last_message_id=row["last_message_id"],
        reply_seq=int(row["reply_seq"]),
        agent_reply_count=int(row["agent_reply_count"]),
        deliverable=bool(row["deliverable"]),
        created_at=row["created_at"],
        last_inbound_at=row["last_inbound_at"],
        last_outbound_at=row["last_outbound_at"],
        updated_at=row["updated_at"],
    )


def _to_receipt(row: RowMapping) -> EmailInboundReceipt:
    return EmailInboundReceipt(
        id=row["id"],
        tenant_id=row["tenant_id"],
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        state=EmailReceiptState(row["state"]),
        received_at=row["received_at"],
        provider_id=row["provider_id"],
        ignored_reason=row["ignored_reason"],
        processed_at=row["processed_at"],
        error=row["error"],
    )


class EmailRepository:
    """All email channel state. Raw SQL through SQLAlchemy Core, no ORM."""

    async def resolve_active_by_address(self, address: str) -> EmailChannel | None:
        """Route an inbound message to its channel, before any tenant is known.

        Two hops, for the RLS reason in the module docstring: the slug in the
        local part identifies the tenant under ``admin_scope``, then the channel
        row is read under ``tenant_scope`` where ``tenant_isolation`` passes.

        Returns None for anything that does not resolve to an active channel of an
        active tenant. The caller answers 200 to that, never 404 — a status code
        that distinguishes "no such tenant" from "wrong signature" is an
        enumeration oracle, and a 4xx makes the provider retry forever.
        """
        normalised = normalise_address(address)
        if not normalised:
            return None
        slug = slug_from_address(normalised)
        if slug is None:
            return None

        async with admin_scope() as session:
            result = await session.execute(text(_TENANT_BY_SLUG_SQL), {"slug": slug})
            row = result.mappings().first()
        if row is None:
            return None
        tenant_id: UUID = row["id"]

        async with tenant_scope(tenant_id) as session:
            channel = (
                (
                    await session.execute(
                        text(_ACTIVE_CHANNEL_BY_ADDRESS_SQL), {"address": normalised}
                    )
                )
                .mappings()
                .first()
            )
        if channel is None:
            return None
        return _to_channel(channel)

    async def get_channel(self, tenant_id: UUID, channel_id: UUID) -> EmailChannel | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                (await session.execute(text(_GET_CHANNEL_SQL), {"channel_id": channel_id}))
                .mappings()
                .first()
            )
        return None if row is None else _to_channel(row)

    async def list_channels(self, tenant_id: UUID) -> list[EmailChannel]:
        async with tenant_scope(tenant_id) as session:
            rows = (await session.execute(text(_LIST_CHANNELS_SQL))).mappings().all()
        return [_to_channel(row) for row in rows]

    async def create_channel(
        self,
        tenant_id: UUID,
        agent_id: UUID,
        *,
        address: str,
        forward_from: str | None = None,
        display_name: str | None = None,
        allowed_forwarders: list[str] | None = None,
        ingest_attachments: bool = False,
        link_sources: bool = False,
        status: str = EmailChannelStatus.PENDING,
    ) -> EmailChannel:
        params = {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "address": normalise_address(address),
            "forward_from": normalise_address(forward_from) if forward_from else None,
            "display_name": display_name,
            "allowed_forwarders": ARRAY_DELIMITER.join(allowed_forwarders or []),
            "ingest_attachments": ingest_attachments,
            "link_sources": link_sources,
            "status": str(status),
            "delimiter": ARRAY_DELIMITER,
        }
        async with tenant_scope(tenant_id) as session:
            row = (await session.execute(text(_CREATE_CHANNEL_SQL), params)).mappings().first()
        if row is None:  # pragma: no cover - RETURNING on a successful INSERT
            raise RuntimeError("email channel insert returned no row")
        return _to_channel(row)

    async def update_channel(
        self,
        tenant_id: UUID,
        channel_id: UUID,
        *,
        agent_id: UUID | None = None,
        address: str | None = None,
        forward_from: str | None = None,
        has_forward_from: bool = False,
        display_name: str | None = None,
        has_display_name: bool = False,
        sending_domain: str | None = None,
        has_sending_domain: bool = False,
        verified: bool = False,
        allowed_forwarders: list[str] | None = None,
        ingest_attachments: bool | None = None,
        link_sources: bool | None = None,
        audit_sends_remaining: int | None = None,
        status: str | None = None,
        error_reason: str | None = None,
        has_error_reason: bool = False,
    ) -> EmailChannel | None:
        """Partial update. Nullable columns need a `has_*` flag beside them.

        COALESCE cannot express "set this to NULL" — it reads a NULL parameter as
        "leave alone" — so clearing `forward_from` or `error_reason` would be
        impossible without the flag.
        """
        params = {
            "channel_id": channel_id,
            "agent_id": agent_id,
            "address": normalise_address(address) if address else None,
            "forward_from": normalise_address(forward_from) if forward_from else None,
            "has_forward_from": has_forward_from,
            "display_name": display_name,
            "has_display_name": has_display_name,
            "sending_domain": (sending_domain or "").strip().lower() or None,
            "has_sending_domain": has_sending_domain,
            "verified": verified,
            "allowed_forwarders": ARRAY_DELIMITER.join(allowed_forwarders or []),
            "has_allowed_forwarders": allowed_forwarders is not None,
            "ingest_attachments": ingest_attachments,
            "link_sources": link_sources,
            "audit_sends_remaining": audit_sends_remaining,
            "status": str(status) if status else None,
            "error_reason": error_reason,
            "has_error_reason": has_error_reason,
            "delimiter": ARRAY_DELIMITER,
        }
        async with tenant_scope(tenant_id) as session:
            row = (await session.execute(text(_UPDATE_CHANNEL_SQL), params)).mappings().first()
        return None if row is None else _to_channel(row)

    async def mark_channel_error(
        self, tenant_id: UUID, channel_id: UUID, reason: str
    ) -> EmailChannel | None:
        return await self.update_channel(
            tenant_id,
            channel_id,
            status=EmailChannelStatus.ERROR,
            error_reason=reason[:500],
            has_error_reason=True,
        )

    async def disconnect_channel(self, tenant_id: UUID, channel_id: UUID) -> EmailChannel | None:
        return await self.update_channel(
            tenant_id, channel_id, status=EmailChannelStatus.DISCONNECTED
        )

    async def consume_audit_send(self, tenant_id: UUID, channel_id: UUID) -> int | None:
        """Decrement the free-audit budget atomically. None means it is spent.

        The guard is in the WHERE clause rather than a prior SELECT so two
        concurrent sends cannot both see the last credit.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (await session.execute(text(_CONSUME_AUDIT_SEND_SQL), {"channel_id": channel_id}))
                .mappings()
                .first()
            )
        return None if row is None else int(row["audit_sends_remaining"])

    async def reserve_inbound_message(
        self,
        tenant_id: UUID,
        channel_id: UUID,
        message_id: str,
        *,
        provider_id: str | None = None,
        received_at: datetime | None = None,
    ) -> EmailInboundReceipt | None:
        """Claim a Message-ID. None means somebody already claimed it.

        Never widened to "return the existing row": the caller must be able to
        tell a first delivery from a replay, and a redelivery that got the
        existing row back would answer the same mail twice.
        """
        params = {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "message_id": message_id[:998],
            "provider_id": provider_id,
            "received_at": received_at,
        }
        async with tenant_scope(tenant_id) as session:
            row = (await session.execute(text(_RESERVE_INBOUND_SQL), params)).mappings().first()
        return None if row is None else _to_receipt(row)

    async def set_inbound_receipt_state(
        self,
        tenant_id: UUID,
        message_id: str,
        state: str,
        *,
        ignored_reason: str | None = None,
        error: str | None = None,
    ) -> None:
        params = {
            "message_id": message_id[:998],
            "state": str(state),
            "ignored_reason": ignored_reason,
            "error": error[:2000] if error else None,
        }
        async with tenant_scope(tenant_id) as session:
            await session.execute(text(_SET_RECEIPT_STATE_SQL), params)

    async def get_thread(self, tenant_id: UUID, thread_id: UUID) -> EmailThread | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                (await session.execute(text(_THREAD_BY_ID_SQL), {"thread_id": thread_id}))
                .mappings()
                .first()
            )
        return None if row is None else _to_thread(row)

    async def resolve_thread(
        self,
        tenant_id: UUID,
        channel_id: UUID,
        *,
        candidate_message_ids: list[str],
        external_user_ref: str,
        subject_normalised: str,
        window_hours: int,
    ) -> EmailThread | None:
        """Find the thread this message belongs to, headers first.

        In-Reply-To and References are authoritative and are tried first. The
        subject fallback is scoped to one sender, one channel and a time window,
        because "Re: invoice" is not identifying on its own — without those three
        it would splice two customers' conversations together.
        """
        async with tenant_scope(tenant_id) as session:
            if candidate_message_ids:
                row = (
                    (
                        await session.execute(
                            text(_THREAD_BY_MESSAGE_IDS_SQL),
                            {
                                "channel_id": channel_id,
                                "candidates": ARRAY_DELIMITER.join(candidate_message_ids),
                                "delimiter": ARRAY_DELIMITER,
                            },
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is not None:
                    return _to_thread(row)

            if not subject_normalised:
                return None
            row = (
                (
                    await session.execute(
                        text(_THREAD_BY_SUBJECT_SQL),
                        {
                            "channel_id": channel_id,
                            "external_user_ref": external_user_ref,
                            "subject_normalised": subject_normalised,
                            "window_hours": window_hours,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _to_thread(row)

    async def latest_thread_for_participant(
        self, tenant_id: UUID, channel_id: UUID, external_user_ref: str
    ) -> EmailThread | None:
        """Newest thread for one sender, ignoring headers and the time window.

        Used by the bounce path: a provider callback names a recipient, not a
        thread, so a hard bounce is attributed to that participant's most recent
        thread rather than being dropped.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(_THREAD_BY_PARTICIPANT_SQL),
                        {"channel_id": channel_id, "external_user_ref": external_user_ref},
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else _to_thread(row)

    async def upsert_thread(
        self,
        tenant_id: UUID,
        channel_id: UUID,
        conversation_id: UUID,
        *,
        external_user_ref: str,
        participant_ciphertext: str,
        participant_masked: str,
        subject_normalised: str,
        message_ids: list[str],
        last_message_id: str | None = None,
    ) -> EmailThread:
        """Create the thread for a conversation, or merge into the existing one.

        ON CONFLICT rather than an existence check: two messages of the same
        thread can arrive inside one provider batch.
        """
        params = {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "conversation_id": conversation_id,
            "external_user_ref": external_user_ref,
            "participant_ciphertext": participant_ciphertext,
            "participant_masked": participant_masked,
            "subject_normalised": subject_normalised,
            "message_ids": ARRAY_DELIMITER.join(message_ids),
            "last_message_id": last_message_id,
            "delimiter": ARRAY_DELIMITER,
        }
        async with tenant_scope(tenant_id) as session:
            row = (await session.execute(text(_CREATE_THREAD_SQL), params)).mappings().first()
        if row is None:  # pragma: no cover - DO UPDATE always returns a row
            raise RuntimeError("email thread upsert returned no row")
        return _to_thread(row)

    async def append_message_id(
        self,
        tenant_id: UUID,
        thread_id: UUID,
        message_id: str,
        *,
        inbound: bool,
        subject_normalised: str | None = None,
    ) -> EmailThread | None:
        """Record one more message on the thread.

        Outbound also bumps `reply_seq` and `agent_reply_count` in the same
        statement, which is what makes EMAIL_MAX_REPLIES_PER_THREAD a real
        ceiling under concurrency rather than a suggestion.
        """
        params = {
            "thread_id": thread_id,
            "message_id": message_id[:998],
            "inbound": inbound,
            "subject_normalised": subject_normalised,
            "has_subject": subject_normalised is not None,
        }
        async with tenant_scope(tenant_id) as session:
            row = (await session.execute(text(_APPEND_MESSAGE_ID_SQL), params)).mappings().first()
        return None if row is None else _to_thread(row)

    async def next_reply_seq(self, tenant_id: UUID, thread_id: UUID) -> int:
        """Peek the next sequence number a new outbound Message-ID will carry.

        PEEKS without consuming it: the increment is owned by
        ``append_message_id`` (which bumps ``reply_seq`` for outbound messages on
        successful recording), so a redelivered reply reuses the same seq and
        Message-ID.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (await session.execute(text(_NEXT_REPLY_SEQ_SQL), {"thread_id": thread_id}))
                .mappings()
                .first()
            )
        return 0 if row is None else int(row["reply_seq"])

    async def mark_undeliverable(self, tenant_id: UUID, thread_id: UUID) -> None:
        """Stop writing to this thread. Set on a hard bounce or a complaint."""
        async with tenant_scope(tenant_id) as session:
            await session.execute(text(_MARK_UNDELIVERABLE_SQL), {"thread_id": thread_id})

    async def record_delivery(
        self, tenant_id: UUID, channel_id: UUID | None, update: EmailDeliveryUpdate
    ) -> None:
        params = {
            "tenant_id": tenant_id,
            "channel_id": channel_id,
            "provider_id": update.provider_id,
            "recipient_masked": update.recipient_masked,
            "status": str(update.status),
            "status_rank": delivery_rank(str(update.status)),
            "bounce_type": update.bounce_type,
            "error": update.error[:2000] if update.error else None,
            "provider_timestamp": update.provider_timestamp,
        }
        async with tenant_scope(tenant_id) as session:
            await session.execute(text(_RECORD_DELIVERY_SQL), params)

    async def add_suppression(
        self, tenant_id: UUID, *, address: str, address_hash: str, reason: str
    ) -> None:
        """Never mail this address again for this tenant.

        The address itself is never stored: the hash is the lookup key and the
        mask is what a human reads in the dashboard.
        """
        params = {
            "tenant_id": tenant_id,
            "address_hash": address_hash,
            "address_masked": mask_address(address),
            "reason": reason,
        }
        async with tenant_scope(tenant_id) as session:
            await session.execute(text(_ADD_SUPPRESSION_SQL), params)

    async def is_suppressed(self, tenant_id: UUID, address_hash: str) -> bool:
        async with tenant_scope(tenant_id) as session:
            row = (
                (await session.execute(text(_IS_SUPPRESSED_SQL), {"address_hash": address_hash}))
                .mappings()
                .first()
            )
        return row is not None

    async def purge_receipts_older_than(
        self, days: int, *, batch: int = 1000, tenant_limit: int = 200
    ) -> int:
        """Delete expired inbound and delivery receipts. Returns rows removed.

        The tenant list is gathered under `admin_scope` because a cron job has no
        tenant, but every DELETE runs under that tenant's own `tenant_scope`, so
        the RLS policy — not this loop — is what confines each statement.
        """
        async with admin_scope() as session:
            tenant_rows = (
                (
                    await session.execute(
                        text(_PURGE_CANDIDATE_TENANTS_SQL),
                        {"days": days, "limit": tenant_limit},
                    )
                )
                .mappings()
                .all()
            )

        removed = 0
        for tenant_row in tenant_rows:
            tenant_id: UUID = tenant_row["tenant_id"]
            params = {"tenant_id": tenant_id, "days": days, "batch": batch}
            try:
                async with tenant_scope(tenant_id) as session:
                    receipts = await session.execute(text(_PURGE_RECEIPTS_SQL), params)
                    deliveries = await session.execute(text(_PURGE_DELIVERY_SQL), params)
                removed += int(getattr(receipts, "rowcount", 0) or 0)
                removed += int(getattr(deliveries, "rowcount", 0) or 0)
            except Exception as exc:  # one tenant's failure must not stop the sweep
                logger.warning(
                    "email_receipt_purge_failed", tenant_id=str(tenant_id), error=str(exc)
                )
        return removed
