"""Authenticated Resend webhook: routes inbound mail to a tenant and defers the answer.

Follows `app/api/webhooks/whatsapp.py` beat for beat — verify before parsing,
acknowledge fast, do nothing expensive inline. Two extra concerns are specific to
email:

* **Loop screening happens here**, before the job is enqueued, because a rejected
  auto-responder should not cost a job. Without it our bot and the customer's bot
  converse until somebody notices the bill.
* **The job payload carries no content.** Resend's webhook has no body, so the
  worker re-fetches it. That is strictly better: nothing sensitive traverses the
  queue.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import PlainTextResponse, Response

from app.adapters.channels.email_resend import (
    INBOUND_EVENT_TYPES,
    parse_delivery_event,
    parse_received_payload,
    verify_resend_signature,
)
from app.adapters.queue.base import Job
from app.api.deps import ContainerDep, client_ip
from app.core.logging import get_logger
from app.domain.email_channel import EmailReceiptState
from app.domain.email_text import (
    IGNORED_SENDER_NOT_ALLOWED,
    is_auto_submitted,
    is_sender_allowed,
    normalise_address,
)
from app.services.audit import AUDIT_EMAIL_IGNORED, record
from app.services.channels.email import EMAIL_JOB_KIND

logger = get_logger(__name__)
router = APIRouter(prefix="/webhooks/email", tags=["webhooks"])


@router.post("")
async def receive_webhook(
    request: Request,
    container: ContainerDep,
    svix_id: Annotated[str | None, Header()] = None,
    svix_timestamp: Annotated[str | None, Header()] = None,
    svix_signature: Annotated[str | None, Header()] = None,
) -> Response:
    """Verify before parsing, acknowledge fast, defer everything else."""
    raw = await request.body()
    if not verify_resend_signature(
        raw,
        svix_id or "",
        svix_timestamp or "",
        svix_signature or "",
        container.settings.RESEND_WEBHOOK_SECRET,
    ):
        # Nothing beyond this line reads the body of an unauthenticated request.
        logger.warning("webhook_signature_rejected", provider="resend", ip=client_ip(request))
        return PlainTextResponse("invalid signature", status_code=401)

    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return Response(status_code=200)
    if not isinstance(body, Mapping):
        return Response(status_code=200)

    event_type = str(body.get("type") or "")
    data = body.get("data")
    if not isinstance(data, Mapping):
        return Response(status_code=200)

    try:
        if event_type in INBOUND_EVENT_TYPES:
            return await _process_inbound(container, data)
        return await _process_delivery(container, data, event_type)
    except Exception:
        # A non-2xx makes the provider redeliver forever, and the message will
        # route no better on the tenth attempt than the first. The same reasoning
        # already written at app/api/internal/jobs.py:57-60.
        logger.exception("email_webhook_event_failed", event_type=event_type)
        return Response(status_code=200)


async def _process_inbound(container: Any, data: Mapping[str, Any]) -> Response:
    received = parse_received_payload(data)
    channel = await _resolve_channel(container, _routing_candidates(data, received))
    if channel is None:
        # Never 404: that turns the endpoint into an address-enumeration oracle,
        # and a non-2xx makes the provider retry a message that cannot route.
        logger.info("email_webhook_unknown_address", provider_id=received.provider_id)
        return Response(status_code=200)

    message_id = received.message_id or f"provider:{received.provider_id}"
    receipt = await container.email_repo.reserve_inbound_message(
        channel.tenant_id,
        channel.id,
        message_id,
        provider_id=received.provider_id,
        received_at=received.received_at,
    )
    if receipt is None:
        # Replay: a webhook redelivery carries the same Message-ID. Dropping a
        # duplicate is better than answering the same customer twice.
        return Response(status_code=200)

    if await _maybe_justdial(container, channel, received, message_id):
        # Routed to growth as a lead source, not to the reply agent. Returns
        # before the loop screening because a JustDial notification is machine
        # generated and would be screened as a bot, and before the forwarder
        # allowlist because justdial.com is not the tenant's forwarder — it is
        # the original sender the forwarder preserved. The receipt stays
        # RESERVED: its job was the replay guard, and no reply will follow.
        return Response(status_code=200)

    ignored_reason = is_auto_submitted(
        received.headers,
        received.subject,
        received.from_address,
        inbound_domain=str(container.settings.EMAIL_INBOUND_DOMAIN),
        channel_address=channel.address,
        spam_score=received.spam_score,
    )
    if ignored_reason is not None:
        await container.email_repo.set_inbound_receipt_state(
            channel.tenant_id,
            message_id,
            EmailReceiptState.IGNORED,
            ignored_reason=ignored_reason,
        )
        await record(
            AUDIT_EMAIL_IGNORED,
            actor_type="system",
            target_tenant=channel.tenant_id,
            target_type="email_channel",
            target_id=str(channel.id),
            detail={"ignored_reason": ignored_reason},
        )
        return Response(status_code=200)

    if not is_sender_allowed(received.from_address, channel.allowed_forwarders):
        await container.email_repo.set_inbound_receipt_state(
            channel.tenant_id,
            message_id,
            EmailReceiptState.IGNORED,
            ignored_reason=IGNORED_SENDER_NOT_ALLOWED,
        )
        await record(
            AUDIT_EMAIL_IGNORED,
            actor_type="system",
            target_tenant=channel.tenant_id,
            target_type="email_channel",
            target_id=str(channel.id),
            detail={"ignored_reason": IGNORED_SENDER_NOT_ALLOWED},
        )
        return Response(status_code=200)

    await container.queue.enqueue(
        Job(
            kind=EMAIL_JOB_KIND,
            tenant_id=channel.tenant_id,
            payload={
                "provider_id": received.provider_id,
                "address": channel.address,
                "message_id": message_id,
            },
        )
    )
    return Response(status_code=200)


async def _process_delivery(
    container: Any, data: Mapping[str, Any], event_type: str
) -> Response:
    """Bounces, complaints and delivery confirmations land on the same endpoint."""
    update = parse_delivery_event({**data, "type": event_type})
    if update is None:
        # An unhandled kind is a 200, never a non-2xx.
        return Response(status_code=200)

    recipients = _address_list(data.get("to")) or _address_list(data.get("email"))
    channel = await _resolve_channel(container, _address_list(data.get("from")))
    if channel is None:
        logger.info("email_webhook_delivery_unrouted", event_type=event_type)
        return Response(status_code=200)

    await container.email_service.record_delivery(
        channel.tenant_id,
        channel,
        update,
        recipient=recipients[0] if recipients else "",
    )
    return Response(status_code=200)


async def _resolve_channel(container: Any, candidates: list[str]) -> Any:
    """First address that resolves to an active channel wins.

    Routing happens before any tenant context exists, which is why the repository
    does the first hop under `admin_scope`.
    """
    seen: set[str] = set()
    for candidate in candidates:
        address = normalise_address(candidate)
        if not address or address in seen:
            continue
        seen.add(address)
        channel = await container.email_repo.resolve_active_by_address(address)
        if channel is not None:
            return channel
    return None


def _routing_candidates(data: Mapping[str, Any], received: Any) -> list[str]:
    """`received_for` first: under a forwarding rule `to` is still the customer's own
    address, so `to` alone cannot resolve the channel."""
    candidates = list(received.received_for)
    candidates.extend(received.to)
    candidates.extend(received.cc)
    candidates.extend(_address_list(data.get("received_for")))
    return candidates


async def _maybe_justdial(
    container: Any, channel: Any, received: Any, message_id: str
) -> bool:
    """Route a JustDial enquiry mail to the inbound lead pipeline.

    True means "handled, stop here". The check is three gates, cheapest first:
    the From domain is JustDial's own (one regex, no database), the mail is not
    spam-flagged (Resend scores it on the way in; a spoofed From is the one
    attack this cheap gate otherwise admits, and a spoof has no SPF pass behind
    it), and only then does this tenant have the JustDial source connected.
    A spam-scored JustDial mail falls through to the screening below, which
    drops it as machine-generated — logged and audited as ignored, not read by
    the owner. A JustDial mail to a tenant who never connected it falls
    through the same way, for the same reason: routing it into growth for a
    source the tenant never turned on would be writing to a board they opted
    out of, and the enquiry is still visible in the receipts the retention
    sweep keeps.

    Enqueues the same job kind the form ingress uses, with a ``source`` marker
    the dispatcher reads: the payload still carries no content, the worker
    re-fetches the mail from Resend.
    """
    from app.services.growth.inbound import INBOUND_JOB
    from app.services.growth.justdial import PROVIDER, is_justdial_sender

    if not is_justdial_sender(received.from_address):
        return False
    # the same 5.0 threshold is_auto_submitted applies on the reply path,
    # stated inline here because this gate runs before that screening
    if received.spam_score is not None and received.spam_score >= 5.0:
        logger.info(
            "justdial_mail_spam_scored",
            spam_score=received.spam_score,
        )
        return False
    integration = await container.growth.integrations.get(channel.tenant_id, PROVIDER)
    if integration is None or integration.get("status") != "active":
        return False
    await container.queue.enqueue(
        Job(
            kind=INBOUND_JOB,
            tenant_id=channel.tenant_id,
            payload={
                "source": PROVIDER,
                "provider_id": received.provider_id,
                "address": channel.address,
                "message_id": message_id,
            },
        )
    )
    return True


def _address_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        address = value.get("address") or value.get("email")
        return [str(address)] if isinstance(address, str) else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_address_list(item))
        return out
    return []
