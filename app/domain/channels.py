"""Channel message and WhatsApp persistence value objects.

The domain layer carries provider identifiers and encrypted-secret references only;
decryption and database concerns remain in services and adapters.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

type JSONValue = str | int | float | bool | list["JSONValue"] | dict[str, "JSONValue"] | None


class IntegrationStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class ReceiptState(StrEnum):
    RESERVED = "reserved"
    PROCESSED = "processed"
    FAILED = "failed"


class DeliveryStatus(StrEnum):
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class InboundTextMessage:
    message_id: str
    phone_number_id: str
    sender_number: str
    text: str
    timestamp: datetime
    reply_to_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryUpdate:
    message_id: str
    status: DeliveryStatus
    timestamp: datetime
    recipient_id: str | None = None
    error_code: str | None = None
    error: str | None = None


@dataclass(slots=True)
class WhatsAppIntegration:
    id: UUID
    tenant_id: UUID
    agent_id: UUID
    waba_id: str
    phone_number_id: str
    token_ref: str | None
    status: IntegrationStatus = IntegrationStatus.ACTIVE
    display_number: str | None = None
    service_window_hours: int = 24
    template_config: dict[str, JSONValue] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    disconnected_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class InboundReceipt:
    id: UUID
    tenant_id: UUID
    integration_id: UUID
    message_id: str
    state: ReceiptState
    received_at: datetime
    processed_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EncryptedTokenRecord:
    """Encrypted credential material; callers explicitly decrypt at the service edge."""

    ref: str
    provider: str
    ciphertext: str
    created_at: datetime
    rotated_at: datetime | None = None
