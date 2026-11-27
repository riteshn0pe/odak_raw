"""WhatsApp inbound processing: serialize threads, run the shared agent, and reply through Meta."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.adapters.cache.base import KeyedLock
from app.adapters.channels.whatsapp import WhatsAppClient
from app.api.deps import load_agent_config
from app.core.constants import Channel
from app.core.errors import AuthenticationError, NotFoundError
from app.core.logging import get_logger
from app.core.security import decrypt_secret, hash_identifier
from app.domain.channels import DeliveryStatus, DeliveryUpdate, ReceiptState
from app.domain.entities import TenantContext

logger = get_logger(__name__)

# platform-wide inbound rate limit, per tenant, per minute
WHATSAPP_RATE_LIMIT_RPM = 60


class WhatsAppService:
    """Consumes one encrypted queue payload and produces one durable WhatsApp turn."""

    def __init__(self, container: Any) -> None:
        self._container = container

    async def process_reply(self, tenant_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        message_id = _required(payload, "message_id")
        phone_number_id = _required(payload, "phone_number_id")
        sender = decrypt_secret(
            _required(payload, "sender_ciphertext"), self._container.settings.ENCRYPTION_KEY
        )
        question = decrypt_secret(
            _required(payload, "text_ciphertext"), self._container.settings.ENCRYPTION_KEY
        )

        integration = await self._container.whatsapp_repo.resolve_active_by_phone_number_id(
            phone_number_id
        )
        if integration is None or integration.tenant_id != tenant_id:
            raise NotFoundError("WhatsApp integration is unavailable")

        token_record = await self._container.whatsapp_repo.get_access_token_record(
            tenant_id, integration.id
        )
        if token_record is None or token_record.provider != "whatsapp":
            raise AuthenticationError("WhatsApp access token is unavailable")
        access_token = decrypt_secret(
            token_record.ciphertext, self._container.settings.ENCRYPTION_KEY
        )

        from app.adapters.postgres.platform_repo import get_tenant_by_id

        tenant_record = await get_tenant_by_id(tenant_id)
        if tenant_record is None or not tenant_record.is_active:
            raise AuthenticationError("tenant is unavailable")
        await self._container.rate_limiter.check(
            "whatsapp", str(tenant_id), WHATSAPP_RATE_LIMIT_RPM, 60
        )

        sender_ref = hash_identifier(
            sender, f"{self._container.settings.SECRET_KEY}:{tenant_id}"
        )

        # The growth branch, before the RAG agent and before the window is
        # noted. A message from a number this tenant is *mailing* is a sales
        # conversation: answering it with the support agent would be the wrong
        # voice, and for a "STOP" it would be the wrong action outright.
        #
        # `maybe_handle_whatsapp` returns None for anyone who is not a growth
        # lead, so every existing tenant's support path is untouched. The
        # inbound window is still noted first — an inbound message reopens the
        # 24-hour window whoever the sender turns out to be, and the growth
        # sender reads that same window when it queues a reply.
        await self._container.service_window.note_inbound(
            tenant_id, integration.id, sender_ref, datetime.now(UTC)
        )
        growth = await self._growth_reply(tenant_id, sender, question, message_id)
        if growth is not None:
            await self._container.whatsapp_repo.set_inbound_receipt_state(
                tenant_id, message_id, ReceiptState.PROCESSED
            )
            return growth

        lock_key = f"t:{tenant_id}:whatsapp-thread:{sender_ref}"

        try:
            async with KeyedLock(
                self._container.cache,
                lock_key,
                ttl_seconds=self._container.settings.WHATSAPP_LOCK_TTL_SECONDS,
                wait_timeout=self._container.settings.WHATSAPP_LOCK_WAIT_SECONDS,
            ):
                # this inbound message reopened the window. record it before the
                # answer runs, so a cached closed-window reading cannot survive
                # into the send below
                await self._container.service_window.note_inbound(
                    tenant_id, integration.id, sender_ref, datetime.now(UTC)
                )

                result = await self._answer(
                    tenant_id=tenant_id,
                    agent_id=integration.agent_id,
                    tenant_slug=tenant_record.slug,
                    sender_ref=sender_ref,
                    question=question,
                )

                window = await self._container.service_window.evaluate(
                    tenant_id,
                    integration.id,
                    sender_ref,
                    integration.service_window_hours,
                    integration.template_config,
                )

                provider_ids = await self._send(
                    access_token=access_token,
                    phone_number_id=phone_number_id,
                    recipient=sender,
                    reply_to_message_id=message_id,
                    answer=result.answer,
                    window=window,
                )

            for provider_id in provider_ids:
                await self._container.whatsapp_repo.update_delivery_status(
                    tenant_id,
                    integration.id,
                    DeliveryUpdate(
                        message_id=provider_id,
                        status=DeliveryStatus.SENT,
                        timestamp=datetime.now(UTC),
                        recipient_id=sender_ref,
                    ),
                )
            await self._container.whatsapp_repo.set_inbound_receipt_state(
                tenant_id, message_id, ReceiptState.PROCESSED
            )
            return {
                "status": "sent" if provider_ids else "undeliverable",
                "inbound_message_id": message_id,
                "outbound_messages": len(provider_ids),
            }
        except Exception as exc:
            await self._container.whatsapp_repo.set_inbound_receipt_state(
                tenant_id,
                message_id,
                ReceiptState.FAILED,
                error=type(exc).__name__,
            )
            raise

    async def _growth_reply(
        self, tenant_id: UUID, sender: str, question: str, message_id: str
    ) -> dict[str, Any] | None:
        """Route a growth-thread message to the reply agent, or None.

        Its own try/except, for the reason the email branch states: if the
        growth half is broken, an inbound customer message must still reach the
        support agent. Failing open to support is the safe direction.
        """
        service = getattr(self._container, "growth_replies", None)
        if service is None:
            return None
        try:
            return await service.maybe_handle_whatsapp(  # type: ignore[no-any-return]
                tenant_id, sender=sender, body=question, provider_id=message_id
            )
        except Exception:
            logger.warning(
                "growth_whatsapp_branch_failed", tenant_id=str(tenant_id), exc_info=True
            )
            return None

    async def _answer(
        self,
        *,
        tenant_id: UUID,
        agent_id: UUID,
        tenant_slug: str,
        sender_ref: str,
        question: str,
    ) -> Any:
        tenant = TenantContext(
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            channel=Channel.WHATSAPP,
            agent_id=agent_id,
            external_user_ref=sender_ref,
        )
        _, config = await load_agent_config(tenant, self._container, agent_id)
        return await self._container.agent.answer(
            tenant_id=tenant_id,
            config=config,
            question=question,
            agent_id=agent_id,
            channel=Channel.WHATSAPP,
            external_user_ref=sender_ref,
        )

    async def _send(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        recipient: str,
        reply_to_message_id: str,
        answer: str,
        window: Any,
    ) -> list[str]:
        """Send the reply, respecting Meta's 24-hour service window.

        Inside the window a free-form message is allowed. Outside it, Meta rejects
        free-form sends outright, so the only options are an approved template or
        nothing — and a template carries no answer text, which is why the answer
        is still persisted regardless of whether it could be delivered.
        """
        client = WhatsAppClient(
            access_token,
            phone_number_id,
            version=self._container.settings.WHATSAPP_GRAPH_VERSION,
            timeout=self._container.settings.HTTP_TIMEOUT_SECONDS,
            max_message_length=self._container.settings.WHATSAPP_MAX_MESSAGE_LENGTH,
        )
        try:
            if window.can_send_free_form:
                try:
                    await client.mark_read(reply_to_message_id)
                except Exception:
                    logger.debug("whatsapp_mark_read_failed", exc_info=True)
                return await client.send_text(recipient, answer, reply_to_message_id)

            if window.can_reopen:
                logger.info(
                    "whatsapp_window_closed_sending_template",
                    template=window.template_name,
                )
                message_id = await client.send_template(
                    recipient, window.template_name, window.template_language
                )
                return [message_id]

            # nothing can be delivered. the answer is already saved to the
            # transcript, so the tenant can see it in the dashboard
            logger.warning(
                "whatsapp_reply_undeliverable",
                reason="service window closed and no reopen template configured",
            )
            return []
        finally:
            await client.close()


def _required(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"WhatsApp job is missing {key}")
    return value
