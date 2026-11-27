"""Postgres persistence for WhatsApp integrations, replay receipts, and delivery states.

Only inbound routing runs before the tenant is known and therefore uses
``admin_scope``. Every subsequent read and write uses ``tenant_scope`` so forced
RLS remains the final isolation boundary.
"""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID, uuid4

from app.core.db import admin_scope, tenant_scope
from app.domain.channels import (
    DeliveryUpdate,
    EncryptedTokenRecord,
    InboundReceipt,
    IntegrationStatus,
    JSONValue,
    ReceiptState,
    WhatsAppIntegration,
)
from sqlalchemy import text
from sqlalchemy.engine import RowMapping

_SELECT_ACTIVE_SQL = """
    SELECT integration.id, integration.tenant_id, integration.agent_id,
           integration.waba_id, integration.phone_number_id,
           integration.display_number, integration.token_ref, integration.status,
           integration.service_window_hours, integration.template_config,
           integration.created_at, integration.updated_at,
           integration.disconnected_at
      FROM whatsapp_integrations integration
      JOIN tenants tenant ON tenant.id = integration.tenant_id
     WHERE integration.phone_number_id = :phone_number_id
       AND integration.status = 'active'
       AND tenant.status = 'active'
"""
_GET_INTEGRATION_SQL = """
    SELECT id, tenant_id, agent_id, waba_id, phone_number_id, display_number,
           token_ref, status, service_window_hours, template_config, created_at,
           updated_at, disconnected_at
      FROM whatsapp_integrations
     WHERE id = :integration_id
"""
_CREATE_INTEGRATION_SQL = """
    INSERT INTO whatsapp_integrations (
        id, tenant_id, agent_id, waba_id, phone_number_id, display_number,
        token_ref, status, service_window_hours, template_config
    ) VALUES (
        :id, :tenant_id, :agent_id, :waba_id, :phone_number_id, :display_number,
        :token_ref, :status, :service_window_hours, CAST(:template_config AS jsonb)
    )
    RETURNING id, tenant_id, agent_id, waba_id, phone_number_id, display_number,
              token_ref, status, service_window_hours, template_config, created_at,
              updated_at, disconnected_at
"""
_UPDATE_INTEGRATION_SQL = """
    UPDATE whatsapp_integrations
       SET agent_id = COALESCE(:agent_id, agent_id),
           waba_id = COALESCE(:waba_id, waba_id),
           phone_number_id = COALESCE(:phone_number_id, phone_number_id),
           display_number = COALESCE(:display_number, display_number),
           token_ref = COALESCE(:token_ref, token_ref),
           status = COALESCE(:status, status),
           service_window_hours = COALESCE(:service_window_hours, service_window_hours),
           template_config = CASE
               WHEN :has_template_config THEN CAST(:template_config AS jsonb)
               ELSE template_config
           END,
           disconnected_at = CASE
               WHEN :status = 'disconnected' THEN now()
               WHEN :status = 'active' THEN NULL
               ELSE disconnected_at
           END,
           updated_at = now()
     WHERE id = :integration_id
    RETURNING id, tenant_id, agent_id, waba_id, phone_number_id, display_number,
              token_ref, status, service_window_hours, template_config, created_at,
              updated_at, disconnected_at
"""


class WhatsAppRepository:
    async def resolve_active_by_phone_number_id(
        self, phone_number_id: str
    ) -> WhatsAppIntegration | None:
        """Resolve webhook routing before a tenant context exists."""

        async with admin_scope() as session:
            row = (
                (
                    await session.execute(
                        text(_SELECT_ACTIVE_SQL),
                        {"phone_number_id": phone_number_id},
                    )
                )
                .mappings()
                .first()
            )
        return _to_integration(row) if row else None

    async def active_for_tenant(self, tenant_id: UUID) -> WhatsAppIntegration | None:
        """This tenant's live WhatsApp connection, without needing its id.

        Every caller so far knew the integration id because a webhook told them.
        The owner-facing paths — approval alerts, growth acknowledgements — start
        from a tenant instead, and had no way to ask. Inside `tenant_scope`, so
        the RLS policy is what confines it; ordered so a tenant who somehow has
        two active rows gets a stable answer rather than a coin flip.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT id, tenant_id, agent_id, waba_id, phone_number_id, "
                            "display_number, token_ref, status, service_window_hours, "
                            "template_config, created_at, updated_at, disconnected_at "
                            "FROM whatsapp_integrations "
                            "WHERE tenant_id = :tenant_id AND status = 'active' "
                            "ORDER BY created_at LIMIT 1"
                        ),
                        {"tenant_id": tenant_id},
                    )
                )
                .mappings()
                .first()
            )
        return _to_integration(row) if row else None

    async def get_integration(
        self, tenant_id: UUID, integration_id: UUID
    ) -> WhatsAppIntegration | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(_GET_INTEGRATION_SQL),
                        {"integration_id": integration_id},
                    )
                )
                .mappings()
                .first()
            )
        return _to_integration(row) if row else None

    async def create_integration(
        self,
        tenant_id: UUID,
        agent_id: UUID,
        waba_id: str,
        phone_number_id: str,
        token_ref: str,
        *,
        display_number: str | None = None,
        status: IntegrationStatus = IntegrationStatus.ACTIVE,
        service_window_hours: int = 24,
        template_config: dict[str, JSONValue] | None = None,
    ) -> WhatsAppIntegration:
        integration_id = uuid4()
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(_CREATE_INTEGRATION_SQL),
                        {
                            "id": integration_id,
                            "tenant_id": tenant_id,
                            "agent_id": agent_id,
                            "waba_id": waba_id,
                            "phone_number_id": phone_number_id,
                            "display_number": display_number,
                            "token_ref": token_ref,
                            "status": status.value,
                            "service_window_hours": service_window_hours,
                            "template_config": json.dumps(template_config or {}),
                        },
                    )
                )
                .mappings()
                .one()
            )
        return _to_integration(row)

    async def update_integration(
        self,
        tenant_id: UUID,
        integration_id: UUID,
        *,
        agent_id: UUID | None = None,
        waba_id: str | None = None,
        phone_number_id: str | None = None,
        display_number: str | None = None,
        token_ref: str | None = None,
        status: IntegrationStatus | None = None,
        service_window_hours: int | None = None,
        template_config: dict[str, JSONValue] | None = None,
    ) -> WhatsAppIntegration | None:
        """Update mutable Embedded Signup data, including WABA reassociation."""

        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(_UPDATE_INTEGRATION_SQL),
                        {
                            "integration_id": integration_id,
                            "agent_id": agent_id,
                            "waba_id": waba_id,
                            "phone_number_id": phone_number_id,
                            "display_number": display_number,
                            "token_ref": token_ref,
                            "status": status.value if status else None,
                            "service_window_hours": service_window_hours,
                            "has_template_config": template_config is not None,
                            "template_config": json.dumps(template_config or {}),
                        },
                    )
                )
                .mappings()
                .first()
            )
        return _to_integration(row) if row else None

    async def disconnect_integration(self, tenant_id: UUID, integration_id: UUID) -> bool:
        """Disable routing immediately and detach the credential reference."""

        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        """
                        UPDATE whatsapp_integrations
                           SET status = 'disconnected', token_ref = NULL,
                               disconnected_at = now(), updated_at = now()
                         WHERE id = :integration_id
                        RETURNING id
                        """
                    ),
                    {"integration_id": integration_id},
                )
            ).first()
        return row is not None

    async def reserve_inbound_message(
        self,
        tenant_id: UUID,
        integration_id: UUID,
        message_id: str,
        *,
        received_at: datetime | None = None,
    ) -> InboundReceipt | None:
        """Atomically reserve a Meta message id; ``None`` means it is a replay."""

        receipt_id = uuid4()
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                        INSERT INTO whatsapp_inbound_receipts (
                            id, tenant_id, integration_id, message_id, received_at
                        ) VALUES (
                            :id, :tenant_id, :integration_id, :message_id,
                            COALESCE(:received_at, now())
                        )
                        ON CONFLICT (tenant_id, message_id) DO NOTHING
                        RETURNING id, tenant_id, integration_id, message_id, state,
                                  received_at, processed_at, error
                        """
                        ),
                        {
                            "id": receipt_id,
                            "tenant_id": tenant_id,
                            "integration_id": integration_id,
                            "message_id": message_id,
                            "received_at": received_at,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return _to_inbound_receipt(row) if row else None

    async def set_inbound_receipt_state(
        self,
        tenant_id: UUID,
        message_id: str,
        state: ReceiptState,
        *,
        error: str | None = None,
    ) -> bool:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        """
                        UPDATE whatsapp_inbound_receipts
                           SET state = :state,
                               processed_at = CASE
                                   WHEN :state IN ('processed', 'failed') THEN now()
                                   ELSE NULL
                               END,
                               error = :error
                         WHERE message_id = :message_id
                        RETURNING id
                        """
                    ),
                    {"message_id": message_id, "state": state.value, "error": error},
                )
            ).first()
        return row is not None

    async def update_delivery_status(
        self,
        tenant_id: UUID,
        integration_id: UUID,
        update: DeliveryUpdate,
    ) -> bool:
        """Upsert a delivery callback without regressing delivered/read state."""

        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        """
                        INSERT INTO whatsapp_delivery_receipts (
                            tenant_id, integration_id, message_id, recipient_id,
                            status, error_code, error, provider_timestamp
                        ) VALUES (
                            :tenant_id, :integration_id, :message_id, :recipient_id,
                            :status, :error_code, :error, :provider_timestamp
                        )
                        ON CONFLICT (tenant_id, message_id) DO UPDATE SET
                            recipient_id = COALESCE(
                                EXCLUDED.recipient_id,
                                whatsapp_delivery_receipts.recipient_id
                            ),
                            status = CASE
                                WHEN EXCLUDED.status IN ('failed', 'deleted')
                                    THEN EXCLUDED.status
                                WHEN whatsapp_delivery_receipts.status IN ('failed', 'deleted')
                                    THEN whatsapp_delivery_receipts.status
                                WHEN CASE EXCLUDED.status
                                         WHEN 'sent' THEN 1
                                         WHEN 'delivered' THEN 2
                                         WHEN 'read' THEN 3
                                         ELSE 0
                                     END >= CASE whatsapp_delivery_receipts.status
                                         WHEN 'sent' THEN 1
                                         WHEN 'delivered' THEN 2
                                         WHEN 'read' THEN 3
                                         ELSE 0
                                     END
                                    THEN EXCLUDED.status
                                ELSE whatsapp_delivery_receipts.status
                            END,
                            error_code = COALESCE(
                                EXCLUDED.error_code,
                                whatsapp_delivery_receipts.error_code
                            ),
                            error = COALESCE(
                                EXCLUDED.error,
                                whatsapp_delivery_receipts.error
                            ),
                            provider_timestamp = GREATEST(
                                whatsapp_delivery_receipts.provider_timestamp,
                                EXCLUDED.provider_timestamp
                            ),
                            updated_at = now()
                        RETURNING id
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "integration_id": integration_id,
                        "message_id": update.message_id,
                        "recipient_id": update.recipient_id,
                        "status": update.status.value,
                        "error_code": update.error_code,
                        "error": update.error,
                        "provider_timestamp": update.timestamp,
                    },
                )
            ).first()
        return row is not None

    async def last_inbound_at(
        self, tenant_id: UUID, integration_id: UUID, sender_ref: str
    ) -> datetime | None:
        """When this customer last messaged us.

        Read from the conversation, not from the receipt table: receipts are keyed
        by Meta message id and carry no sender, while the conversation is already
        keyed by the hashed sender reference. `last_activity` advances on both
        directions of the exchange, so inbound turns are queried explicitly.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT MAX(message.created_at) AS last_inbound_at
                          FROM messages message
                          JOIN conversations conversation
                            ON conversation.id = message.conversation_id
                         WHERE conversation.channel = 'whatsapp'
                           AND conversation.external_user_ref = :sender_ref
                           AND message.role = 'user'
                        """
                    ),
                    {"sender_ref": sender_ref},
                )
            ).mappings().first()
        return row["last_inbound_at"] if row else None

    async def get_access_token_record(
        self, tenant_id: UUID, integration_id: UUID
    ) -> EncryptedTokenRecord | None:
        """Return encrypted material only; the calling service owns decryption."""

        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT secret.ref, secret.provider, secret.ciphertext,
                               secret.created_at, secret.rotated_at
                          FROM whatsapp_integrations integration
                          JOIN tenant_secrets secret
                            ON secret.tenant_id = integration.tenant_id
                           AND secret.ref = integration.token_ref
                         WHERE integration.id = :integration_id
                           AND integration.status = 'active'
                        """
                        ),
                        {"integration_id": integration_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return EncryptedTokenRecord(
            ref=row["ref"],
            provider=row["provider"],
            ciphertext=row["ciphertext"],
            created_at=row["created_at"],
            rotated_at=row["rotated_at"],
        )


def _to_integration(row: RowMapping) -> WhatsAppIntegration:
    config = row["template_config"]
    if isinstance(config, str):
        config = json.loads(config)
    return WhatsAppIntegration(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        waba_id=row["waba_id"],
        phone_number_id=row["phone_number_id"],
        token_ref=row["token_ref"],
        status=IntegrationStatus(row["status"]),
        display_number=row["display_number"],
        service_window_hours=int(row["service_window_hours"]),
        template_config=dict(config or {}),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        disconnected_at=row["disconnected_at"],
    )


def _to_inbound_receipt(row: RowMapping) -> InboundReceipt:
    return InboundReceipt(
        id=row["id"],
        tenant_id=row["tenant_id"],
        integration_id=row["integration_id"],
        message_id=row["message_id"],
        state=ReceiptState(row["state"]),
        received_at=row["received_at"],
        processed_at=row["processed_at"],
        error=row["error"],
    )
