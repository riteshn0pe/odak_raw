"""Inbound-email persistence value objects.

Mirrors ``app/domain/channels.py`` for WhatsApp and follows the same rule the
docstring there states: the domain layer carries provider identifiers and
encrypted-secret references only. The participant's real address travels as
ciphertext plus a display-safe mask, and decryption happens at the service edge.

Field names track ``migrations/008_email.sql`` column for column on purpose — a
repository that renames on the way through makes the SQL unreadable next to the
schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

from app.core.constants import Channel

EMAIL_CHANNEL_VALUE = "email"


def email_channel() -> Channel:
    """``Channel.EMAIL``, resolved by value rather than by attribute.

    The enum member is added to ``app/core/constants.py`` alongside migration 008
    (which widens the ``conversations_channel_valid`` CHECK to accept ``email``).
    Resolving by value keeps this module importable while that one-line edit is
    pending and costs nothing once it lands.

    Until the member exists, ``Channel(...)`` raises ``ValueError``, which would
    take the whole email path down over a missing enum line. ``Channel`` is a
    ``StrEnum``, so the plain string behaves identically everywhere it is used —
    it is written to ``conversations.channel`` and compared by value.
    """
    try:
        return Channel(EMAIL_CHANNEL_VALUE)
    except ValueError:  # pragma: no cover - transitional, see above
        return cast(Channel, EMAIL_CHANNEL_VALUE)


class EmailChannelStatus(StrEnum):
    """Mirrors the email_channels_status_valid CHECK."""

    PENDING = "pending"
    ACTIVE = "active"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class EmailReceiptState(StrEnum):
    """Mirrors the email_inbound_receipts_state_valid CHECK.

    ``IGNORED`` has no WhatsApp counterpart: an auto-responder or a mailing list
    is dropped deliberately and that is not a failure to retry.
    """

    RESERVED = "reserved"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"


class EmailDeliveryStatus(StrEnum):
    """Mirrors the email_delivery_receipts_status_valid CHECK."""

    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    BOUNCED = "bounced"
    COMPLAINED = "complained"
    FAILED = "failed"


# Delivery state only ever advances. A provider redelivers and reorders its own
# callbacks, so an upsert that took the latest event at face value would let a
# `sent` overwrite a `bounced`.
_DELIVERY_RANK: dict[str, int] = {
    EmailDeliveryStatus.QUEUED: 0,
    EmailDeliveryStatus.SENT: 1,
    EmailDeliveryStatus.DELIVERED: 2,
    EmailDeliveryStatus.FAILED: 3,
    EmailDeliveryStatus.BOUNCED: 4,
    EmailDeliveryStatus.COMPLAINED: 5,
}


def delivery_rank(status: str) -> int:
    return _DELIVERY_RANK.get(status, 0)


class EmailReplyMode(StrEnum):
    """How the agent behaves on inbound mail. ``DRAFT`` is the default."""

    OFF = "off"
    DRAFT = "draft"
    AUTO = "auto"


@dataclass(slots=True)
class EmailChannel:
    """One watched address for one tenant agent."""

    id: UUID
    tenant_id: UUID
    agent_id: UUID
    address: str
    forward_from: str | None = None
    sending_domain: str | None = None
    sending_domain_verified_at: datetime | None = None
    display_name: str | None = None
    allowed_forwarders: list[str] = field(default_factory=list)
    ingest_attachments: bool = False
    link_sources: bool = False
    audit_sends_remaining: int = 20
    status: EmailChannelStatus = EmailChannelStatus.ACTIVE
    error_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    disconnected_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.status == EmailChannelStatus.ACTIVE


@dataclass(slots=True)
class EmailThread:
    """One RFC 5322 thread, mapped onto one conversation."""

    id: UUID
    tenant_id: UUID
    channel_id: UUID
    conversation_id: UUID
    external_user_ref: str
    participant_ciphertext: str
    participant_masked: str
    subject_normalised: str = ""
    message_ids: list[str] = field(default_factory=list)
    last_message_id: str | None = None
    reply_seq: int = 0
    agent_reply_count: int = 0
    deliverable: bool = True
    created_at: datetime | None = None
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def thread_key(self) -> str:
        """The hex form used in our own Message-ID and in the X-CA-Loop header."""
        return self.id.hex


@dataclass(frozen=True, slots=True)
class EmailInboundReceipt:
    """One reserved inbound Message-ID. The insert is the replay guard."""

    id: UUID
    tenant_id: UUID
    channel_id: UUID
    message_id: str
    state: EmailReceiptState
    received_at: datetime
    provider_id: str | None = None
    ignored_reason: str | None = None
    processed_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EmailDeliveryUpdate:
    """A provider callback about one outbound message."""

    provider_id: str
    status: EmailDeliveryStatus
    recipient_masked: str | None = None
    bounce_type: str | None = None
    error: str | None = None
    provider_timestamp: datetime | None = None

    @property
    def is_hard_bounce(self) -> bool:
        return self.status == EmailDeliveryStatus.BOUNCED and (
            (self.bounce_type or "").lower() in {"hard", "permanent", "suppressed", "blocked"}
        )


@dataclass(frozen=True, slots=True)
class EmailAttachmentRef:
    """Attachment metadata from the webhook. The bytes are fetched only if wanted."""

    attachment_id: str
    filename: str
    content_type: str
    size_bytes: int = 0
