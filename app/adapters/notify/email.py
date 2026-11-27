"""Email notifier for escalations: tells the tenant's own people, by mail.

The default destination kind, and the only one that works with no setup beyond an
address. It reuses the same ``EmailProvider`` port the inbound channel sends
through, so a tenant on Resend gets escalation mail through Resend without a
second integration.

Two things it deliberately does not do:

* it does not mail the visitor. The recipient list is tenant-side only, validated
  at the API against tenant membership or a verified domain, because an
  unrestricted destination list is a spam relay.
* it does not raise. A bounce or an outage comes back as a ``NotifyReceipt`` with
  ``delivered=False``, because the caller has an escalation row to update and a
  visitor waiting on an answer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.domain.email_text import mask_address
from app.domain.ports import EmailProvider, NotifyReceipt, OutboundEmail

if TYPE_CHECKING:
    from app.domain.escalation import EscalationEvent

logger = get_logger(__name__)

_CHANNEL = "email"

# subject, text body, html body
type EscalationRenderer = Callable[["EscalationEvent"], tuple[str, str, str]]


class EmailEscalationNotifier:
    """EscalationNotifier that mails the tenant's escalation destinations."""

    def __init__(
        self,
        provider: EmailProvider,
        renderer: EscalationRenderer,
        *,
        destinations: Sequence[str],
        from_address: str,
        from_name: str = "",
        reply_to: str | None = None,
    ) -> None:
        if not from_address:
            raise ValueError("from_address is required")
        self._provider = provider
        self._render = renderer
        self._destinations = tuple(
            address.strip() for address in destinations if address and address.strip()
        )
        self._from_address = from_address
        self._from_name = from_name
        self._reply_to = reply_to

    @property
    def name(self) -> str:
        return _CHANNEL

    async def notify(self, event: EscalationEvent) -> NotifyReceipt:
        if not self._destinations:
            return NotifyReceipt(channel=_CHANNEL, error="no destination configured")

        subject, text, html = self._render(event)
        # The visitor's own address becomes Reply-To when we have one, so a support
        # agent can answer by hitting reply instead of copying an address out of
        # the body. It is never the From: address — that would forge the sender.
        reply_to = self._reply_to
        if event.contact is not None and event.contact.email:
            reply_to = event.contact.email

        message = OutboundEmail(
            to=self._destinations,
            subject=subject,
            text=text,
            html=html,
            from_address=self._from_address,
            from_name=self._from_name,
            reply_to=(reply_to,) if reply_to else (),
            # Escalation mail is machine-generated, so it carries the same
            # loop-suppression set as an answer. An out-of-office auto-reply from a
            # support inbox must not come back to us as a new question.
            headers={
                "Auto-Submitted": "auto-generated",
                "X-Auto-Response-Suppress": "All",
                "X-CA-Loop": f"{event.tenant_id}:escalation",
            },
            # One escalation id, one mail. A retry after a timeout must not page
            # the same team twice.
            idempotency_key=f"escalation:{event.id}",
            tags={"kind": "escalation", "trigger": str(event.trigger)},
        )

        try:
            provider_id = await self._provider.send(message)
        except (ProviderError, ValueError) as exc:
            logger.warning(
                "escalation_email_failed", escalation_id=str(event.id), error=str(exc)
            )
            return NotifyReceipt(channel=_CHANNEL, error=str(exc)[:200])

        logger.info(
            "escalation_email_sent",
            escalation_id=str(event.id),
            provider_id=provider_id,
            to=mask_address(self._destinations[0]),
            recipients=len(self._destinations),
        )
        return NotifyReceipt(channel=_CHANNEL, provider_message_id=provider_id, delivered=True)

    async def close(self) -> None:
        """No-op. The provider is shared with the outbound channel and is closed
        by the container that built it, not by one of its users."""
        return None
