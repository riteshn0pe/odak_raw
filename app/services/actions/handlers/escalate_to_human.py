"""`escalate_to_human` — the escape hatch, and the one action that must never fail.

Writes the durable `escalations` row (007_escalation.sql) and hands the ticket
reference back to the model so it can tell the visitor a person is coming. The
row is the record; the notification is a courtesy layered on top, so a missing or
broken notifier downgrades the row's state to `pending` and still returns success.

The cooldown is a database guarantee rather than a cache-dependent one: the
unique `(tenant_id, dedupe_key)` constraint embeds a time bucket, so a second
escalation for the same conversation inside the window collapses onto the first
and the model is told the ticket is already open. That is also what enforces
one-escalation-per-turn without a counter.

Never rate limited and never metered as a billable action call
(`ACTION_QUOTA_EXEMPT_HANDLERS`), and the outcome repeats `quota_exempt` so
the metering path cannot lose it.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.db import tenant_scope
from app.core.logging import get_logger
from app.core.security import encrypt_secret
from app.domain.actions import (
    ActionContext,
    ActionDefinition,
    ActionOutcome,
    ActionOutcomeCode,
)
from app.services.actions.handlers.base import (
    Args,
    Elapsed,
    Handler,
    InvalidArgumentError,
    clip,
    deliver,
    failed,
    invalid,
    json_dump,
    settings_for,
    succeeded,
)

logger = get_logger(__name__)

# The cooldown window the dedupe key buckets on. `EscalationConfig` is where this
# belongs once the escalation stage lands; until then it is one constant in one
# place rather than a magic number at the call site.
COOLDOWN_SECONDS = 900
TRIGGER = "model_requested"
URGENCIES = ("normal", "high")


def _mask_contact(value: str) -> str:
    """The display form the tenant sees without opening the transcript."""
    if "@" in value:
        local, _, domain = value.partition("@")
        head = local[:2] if len(local) > 2 else local[:1]
        return f"{head}***@{domain}"
    digits = "".join(character for character in value if character.isdigit())
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"


def _ticket(escalation_id: UUID) -> str:
    """A reference short enough for a visitor to read back over the phone."""
    return f"ESC-{escalation_id.hex[:8].upper()}"


def build(service: Any) -> Handler:
    async def handle(
        *,
        action: ActionDefinition,
        arguments: dict[str, Any],
        ctx: ActionContext,
    ) -> ActionOutcome:
        clock = Elapsed()
        args = Args(arguments, ctx)
        try:
            reason = args.text("reason", required=True, min_length=10, max_length=500)
            urgency = args.choice("urgency", URGENCIES, default="normal")
            summary = args.text("summary", max_length=1000)
        except InvalidArgumentError as exc:
            return invalid(str(exc), duration_ms=clock.ms)
        if reason is None:  # pragma: no cover - required=True already raised
            return invalid("reason is required", duration_ms=clock.ms)

        settings = settings_for(service)
        contact_value = ""
        contact_source: str | None = None
        for field, source in (("email", "contact_email"), ("phone", "contact_phone")):
            raw = ctx.contact.get(field) if ctx.contact else None
            if raw:
                contact_value, contact_source = str(raw), source
                break
        ciphertext: str | None = None
        if contact_value and settings is not None:
            key = str(getattr(settings, "ENCRYPTION_KEY", "") or "")
            if key:
                try:
                    ciphertext = encrypt_secret(contact_value, key)
                except Exception:
                    # A bad key must not cost the visitor their escalation; the
                    # masked form is still written and support can read the thread.
                    logger.warning("escalation_contact_encrypt_failed")

        escalation_id = uuid4()
        bucket = int(time.time() // COOLDOWN_SECONDS)
        conversation_key = str(ctx.conversation_id or ctx.call_id or escalation_id)
        dedupe_key = f"{conversation_key}:{TRIGGER}:{bucket}"

        params = {
            "id": escalation_id,
            "tenant_id": ctx.tenant_id,
            "agent_id": ctx.agent_id,
            "conversation_id": ctx.conversation_id,
            "message_id": ctx.message_id,
            "trigger": TRIGGER,
            "channel": str(ctx.channel),
            "dedupe_key": dedupe_key,
            "question": clip(reason, 4000),
            "note": clip(summary, 4000) if summary else None,
            "contact_ciphertext": ciphertext,
            "contact_masked": _mask_contact(contact_value) if contact_value else None,
            "contact_source": contact_source,
            "external_user_ref": ctx.external_user_ref,
        }
        try:
            async with tenant_scope(ctx.tenant_id) as session:
                inserted = (
                    (
                        await session.execute(
                            text(
                                "INSERT INTO escalations ("
                                "id, tenant_id, agent_id, conversation_id, message_id, "
                                "trigger, channel, state, dedupe_key, question, note, "
                                "contact_ciphertext, contact_masked, contact_source, "
                                "external_user_ref) "
                                "VALUES (:id, :tenant_id, :agent_id, :conversation_id, "
                                "(SELECT m.id FROM messages m WHERE m.id = :message_id "
                                "AND m.tenant_id = :tenant_id), "
                                ":trigger, :channel, 'pending', :dedupe_key, :question, "
                                ":note, :contact_ciphertext, :contact_masked, "
                                ":contact_source, :external_user_ref) "
                                "ON CONFLICT (tenant_id, dedupe_key) DO NOTHING "
                                "RETURNING id"
                            ),
                            params,
                        )
                    )
                    .mappings()
                    .first()
                )
                if inserted is None:
                    # Inside the cooldown window: the first escalation stands.
                    existing = (
                        (
                            await session.execute(
                                text(
                                    "SELECT id, state FROM escalations "
                                    "WHERE tenant_id = :tenant_id "
                                    "AND dedupe_key = :dedupe_key"
                                ),
                                {
                                    "tenant_id": ctx.tenant_id,
                                    "dedupe_key": dedupe_key,
                                },
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if existing is not None:
                        open_id = existing["id"]
                        return succeeded(
                            {
                                "ticket": _ticket(
                                    open_id
                                    if isinstance(open_id, UUID)
                                    else UUID(str(open_id))
                                ),
                                "status": "already_open",
                                "state": str(existing["state"]),
                                "urgency": urgency,
                                "message": (
                                    "A person has already been asked to join this "
                                    "conversation. Tell the visitor someone will "
                                    "reply and do not escalate again."
                                ),
                            },
                            duration_ms=clock.ms,
                            quota_exempt=True,
                        )
                    logger.warning("escalation_dedupe_row_missing")
                    return succeeded(
                        {"status": "already_open", "urgency": urgency},
                        duration_ms=clock.ms,
                        quota_exempt=True,
                    )

                receipts = await deliver(
                    service.notifier,
                    subject=f"Escalation from the assistant ({urgency})",
                    body=summary or reason,
                    urgency=urgency,
                    meta={
                        "escalation_id": str(escalation_id),
                        "ticket": _ticket(escalation_id),
                        "conversation_id": str(ctx.conversation_id or ""),
                        "channel": str(ctx.channel),
                        "contact": params["contact_masked"],
                    },
                )
                delivered = any(receipt.get("delivered") for receipt in receipts)
                await session.execute(
                    text(
                        "UPDATE escalations SET "
                        "state = CASE WHEN :delivered THEN 'notified' ELSE 'pending' END, "
                        "notified_at = CASE WHEN :delivered THEN now() ELSE NULL END, "
                        "receipts = CAST(:receipts AS jsonb), updated_at = now() "
                        "WHERE id = :id AND tenant_id = :tenant_id"
                    ),
                    {
                        "delivered": delivered,
                        "receipts": json_dump(receipts),
                        "id": escalation_id,
                        "tenant_id": ctx.tenant_id,
                    },
                )
        except Exception as exc:
            # The escape hatch reports its own failure rather than raising, so the
            # model can still tell the visitor to email the team.
            logger.warning("escalation_write_failed", error=str(exc))
            return failed(
                ActionOutcomeCode.CHANNEL_UNAVAILABLE,
                "the request could not be passed to a person just now",
                duration_ms=clock.ms,
            )

        logger.info(
            "action_escalated",
            tenant_id=str(ctx.tenant_id),
            conversation_id=str(ctx.conversation_id or ""),
            urgency=urgency,
            notified=delivered,
        )
        return succeeded(
            {
                "ticket": _ticket(escalation_id),
                "status": "notified" if delivered else "queued",
                "urgency": urgency,
                "message": (
                    "The team has been notified. Tell the visitor a person will "
                    f"follow up and give them the reference {_ticket(escalation_id)}."
                ),
            },
            duration_ms=clock.ms,
            quota_exempt=True,
        )

    return handle
