"""Authenticated Meta webhook that records delivery updates and queues inbound text replies."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import PlainTextResponse, Response

from app.adapters.channels.whatsapp import verify_meta_signature
from app.adapters.queue.base import Job
from app.api.deps import ContainerDep, client_ip
from app.core.logging import get_logger
from app.core.security import encrypt_secret
from app.domain.channels import DeliveryStatus, DeliveryUpdate

logger = get_logger(__name__)
router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhooks"])


@router.get("")
async def verify_webhook(
    container: ContainerDep,
    hub_mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    hub_challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> Response:
    if (
        hub_mode == "subscribe"
        and hub_verify_token == container.settings.WHATSAPP_VERIFY_TOKEN
        and hub_challenge is not None
        and container.settings.WHATSAPP_VERIFY_TOKEN
    ):
        return PlainTextResponse(hub_challenge)
    return PlainTextResponse("verification failed", status_code=403)


@router.post("")
async def receive_webhook(
    request: Request,
    container: ContainerDep,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> Response:
    """Verify before parsing, acknowledge quickly, and defer answer generation."""
    raw = await request.body()
    if not verify_meta_signature(
        raw,
        x_hub_signature_256 or "",
        container.settings.WHATSAPP_APP_SECRET,
    ):
        # Attributed but detail-free: a rejected signature is worth seeing (a
        # misconfigured provider, or someone probing the public URL), and the
        # caller still gets the same opaque 401 as before.
        logger.warning("webhook_signature_rejected", provider="whatsapp", ip=client_ip(request))
        return PlainTextResponse("invalid signature", status_code=401)

    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return Response(status_code=200)
    if not isinstance(body, Mapping) or body.get("object") != "whatsapp_business_account":
        return Response(status_code=200)

    for field, entry_id, value in _changes(body):
        try:
            if field == "messages":
                await _process_value(container, value)
            elif field == TEMPLATE_STATUS_FIELD:
                await _process_template_status(container, entry_id, value)
        except Exception:
            # Meta batches independent notifications. A bad event must not cause
            # redelivery of every valid event in the same signed callback.
            logger.exception("whatsapp_webhook_event_failed", field=field)
    return Response(status_code=200)


# Meta's template review verdict arrives on its own subscription field, on the
# same signed callback as inbound messages. Without a branch for it a submitted
# campaign template stays 'pending' forever and the WhatsApp leg never sends.
TEMPLATE_STATUS_FIELD = "message_template_status_update"

# Meta's event vocabulary, mapped onto the growth_wa_templates status CHECK
# (migration 017). Anything unlisted — a new event name, or one we have no
# column value for — is treated as "not sendable yet" rather than guessed into
# approved: the send gate reads this column, so an optimistic guess here is a
# message Meta will refuse.
_TEMPLATE_STATUS: dict[str, str] = {
    "APPROVED": "approved",
    "REJECTED": "rejected",
    "PAUSED": "paused",
    "DISABLED": "disabled",
    "PENDING_DELETION": "disabled",
    "PENDING": "pending",
    "FLAGGED": "paused",
}


def _changes(body: Mapping[str, Any]) -> list[tuple[str, str, Mapping[str, Any]]]:
    """Every ``(field, entry id, value)`` in the callback, whatever the field.

    The entry id is carried out because it is the only routing handle a
    template-status change has: those events name the WhatsApp Business Account,
    not a phone number, so the messages path's ``metadata.phone_number_id``
    lookup cannot resolve them.
    """
    changes: list[tuple[str, str, Mapping[str, Any]]] = []
    entries = body.get("entry")
    if not isinstance(entries, list):
        return changes
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        entry_id = str(entry.get("id") or "")
        raw_changes = entry.get("changes")
        if not isinstance(raw_changes, list):
            continue
        for change in raw_changes:
            if not isinstance(change, Mapping):
                continue
            field = change.get("field")
            value = change.get("value")
            if isinstance(field, str) and isinstance(value, Mapping):
                changes.append((field, entry_id, value))
    return changes


async def _process_template_status(
    container: Any, waba_id: str, value: Mapping[str, Any]
) -> None:
    """Meta approved (or refused) a template: flip the catalogue row.

    This is the other half of ``CampaignService._submit_template``. The row is
    written ``pending`` at submission and the WhatsApp sender refuses to send
    outside the 24-hour window without an ``approved`` one, so until this
    handler runs a campaign's WhatsApp leg is correctly stuck. An unroutable
    event is logged rather than raised: Meta will not stop redelivering a 5xx,
    and there is nothing here a retry would fix.
    """
    from app.adapters.postgres.growth_repo import tenant_for_waba

    event = str(value.get("event") or "").upper()
    status = _TEMPLATE_STATUS.get(event)
    if status is None:
        logger.info("whatsapp_template_status_unknown", event=event[:40], waba=waba_id)
        return

    tenant_id = await tenant_for_waba(waba_id)
    if tenant_id is None:
        logger.info("whatsapp_template_status_unknown_waba", waba=waba_id)
        return

    template_id = value.get("message_template_id")
    name = str(value.get("message_template_name") or "")
    # Meta answers in its own locale dialect (``en_US``); the row stores the
    # language (``en``). The id match below is the exact one — this prefix is
    # only the fallback path's key.
    locale = str(value.get("message_template_language") or "")
    language = locale.split("_")[0].lower()

    touched = await container.growth.wa_templates.mark_meta_status(
        tenant_id,
        status=status,
        external_ref=str(template_id) if template_id is not None else "",
        name=name,
        language=language,
    )
    logger.info(
        "whatsapp_template_status_update",
        tenant_id=str(tenant_id),
        template=name,
        language=language,
        status=status,
        rows=touched,
    )


async def _process_value(container: Any, value: Mapping[str, Any]) -> None:
    metadata = value.get("metadata")
    phone_number_id = metadata.get("phone_number_id") if isinstance(metadata, Mapping) else None
    if not isinstance(phone_number_id, str) or not phone_number_id:
        return

    integration = await container.whatsapp_repo.resolve_active_by_phone_number_id(
        phone_number_id
    )
    if integration is None:
        logger.info("whatsapp_webhook_unknown_number", phone_number_id=phone_number_id)
        return

    statuses = value.get("statuses")
    if isinstance(statuses, list):
        for status in statuses:
            if isinstance(status, Mapping):
                await _record_status(container, integration, status)

    messages = value.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, Mapping):
                await _enqueue_message(container, integration, message)


def _digits(value: str) -> str:
    """Digit-stripped phone number, because Meta sends the country code and
    the panel field is typed by a human."""
    return "".join(c for c in value if c.isdigit())


async def _owner_report_phrase(
    container: Any, tenant_id: Any, sender: str
) -> bool:
    """Whether this sender is the owner, for the one recognised phrase.

    The owner's number is the growth `whatsapp` integration's `external_ref`
    (the same row the approvals notifier sends owner pings to).
    """
    try:
        row = await container.growth.integrations.get(tenant_id, "whatsapp")
    except Exception:
        return False
    owner = _digits(str((row or {}).get("external_ref") or ""))
    sender_digits = _digits(sender)
    if not owner or not sender_digits:
        return False
    return (
        owner == sender_digits
        or owner.endswith(sender_digits)
        or sender_digits.endswith(owner)
    )


async def _enqueue_message(container: Any, integration: Any, message: Mapping[str, Any]) -> None:
    text = message.get("text")
    body = text.get("body") if isinstance(text, Mapping) else None
    message_id = message.get("id")
    sender = message.get("from")
    if (
        message.get("type") != "text"
        or not isinstance(body, str)
        or not body.strip()
        or not isinstance(message_id, str)
        or not message_id
        or not isinstance(sender, str)
        or not sender
    ):
        return

    # The one thing WhatsApp recognises before the support-agent enqueue
    # (P6, audit R3): the owner texting "report" from their own number gets
    # the same deduped on-demand run the panel's Run now and the bot's
    # /report use. Nothing else is a command here — WhatsApp's 24-hour window
    # and template rules make it report-only, and every other inbound message
    # is a customer's, destined for the support agent.
    if body.strip().lower() == "report" and await _owner_report_phrase(
        container, integration.tenant_id, sender
    ):
        from app.core.errors import ValidationError

        try:
            await container.growth_reports.request_on_demand(
                integration.tenant_id, via="whatsapp"
            )
        except ValidationError:
            # on-demand reports are off for this tenant: silence, not a
            # support-agent ticket about a word the owner typed
            logger.info(
                "whatsapp_report_phrase_off", tenant_id=str(integration.tenant_id)
            )
        return

    receipt = await container.whatsapp_repo.reserve_inbound_message(
        integration.tenant_id,
        integration.id,
        message_id,
        received_at=_timestamp(message.get("timestamp")),
    )
    if receipt is None:
        return

    key = container.settings.ENCRYPTION_KEY
    await container.queue.enqueue(
        Job(
            kind="whatsapp_reply",
            tenant_id=integration.tenant_id,
            payload={
                "message_id": message_id,
                "phone_number_id": integration.phone_number_id,
                "sender_ciphertext": encrypt_secret(sender, key),
                "text_ciphertext": encrypt_secret(body.strip(), key),
            },
        )
    )


async def _record_status(container: Any, integration: Any, raw: Mapping[str, Any]) -> None:
    message_id = raw.get("id")
    status = raw.get("status")
    if not isinstance(message_id, str) or status not in {item.value for item in DeliveryStatus}:
        return

    errors = raw.get("errors")
    first_error = errors[0] if isinstance(errors, list) and errors else None
    error_code = None
    error_message = None
    if isinstance(first_error, Mapping):
        if first_error.get("code") is not None:
            error_code = str(first_error["code"])
        if first_error.get("title") is not None:
            error_message = str(first_error["title"])

    await container.whatsapp_repo.update_delivery_status(
        integration.tenant_id,
        integration.id,
        DeliveryUpdate(
            message_id=message_id,
            status=DeliveryStatus(status),
            timestamp=_timestamp(raw.get("timestamp")) or datetime.now(UTC),
            recipient_id=(
                str(raw["recipient_id"]) if raw.get("recipient_id") is not None else None
            ),
            error_code=error_code,
            error=error_message,
        ),
    )


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except (TypeError, ValueError, OverflowError):
        return None
