"""`collect_lead` — save the visitor's contact details for follow-up.

Upserts one `contacts` row, writes one `contact_events` row, and attaches the
contact to the conversation if nothing was attached yet. All three in a single
transaction, because a lead that lands as an event with no contact row is a lead
the tenant cannot call back.

The rule that matters here is the last one in §2.2: **a collected lead never
overwrites a verified contact's `email`, `name` or `phone`.** Unverified data
merging into a verified row is an identity-spoofing vector — a visitor who says
"my email is founder@theircompany.com" would otherwise rewrite the real
customer's address. On a verified row the collected values land in `attributes`
under `lead_*` keys and in the event payload's `conflicts` list instead, which is
what the dashboard renders as the conflict.

Which fields are required is read off the definition's `parameters`, so the
tenant's edit on the Actions screen is what the model is held to.
"""

from __future__ import annotations

from typing import Any

from app.core.db import tenant_scope
from app.core.logging import get_logger
from app.domain.actions import (
    ActionContext,
    ActionDefinition,
    ActionOutcome,
    ActionOutcomeCode,
)
from app.services.actions.handlers import contact_store
from app.services.actions.handlers.base import (
    Args,
    Elapsed,
    Handler,
    InvalidArgumentError,
    deliver,
    failed,
    invalid,
    settings_for,
    succeeded,
)

logger = get_logger(__name__)

SOURCE = "collect_lead"
# Guarded columns: never written onto a verified row.
IDENTITY_FIELDS = ("email", "phone", "name")


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
            name = args.text("name", max_length=120)
            email = args.email("email")
            phone = args.phone("phone")
            company = args.text("company", max_length=120)
            note = args.text("note", max_length=500)
            # The tenant's own required-field policy, as edited on the Actions
            # screen. Checked before the reachability rule so the model gets the
            # more specific complaint first.
            for parameter in action.parameters:
                if parameter.required and not arguments.get(parameter.name):
                    raise InvalidArgumentError(f"{parameter.name} is required")
        except InvalidArgumentError as exc:
            return invalid(str(exc), duration_ms=clock.ms)

        if not email and not phone:
            return invalid(
                "an email address or a phone number is needed before this can be "
                "saved; ask the visitor for one and read it back to them",
                duration_ms=clock.ms,
            )

        settings = settings_for(service)
        max_keys = int(getattr(settings, "CONTACT_MAX_ATTRIBUTES", 25) or 25)
        max_length = int(getattr(settings, "CONTACT_ATTRIBUTE_MAX_LENGTH", 200) or 200)
        external_id = None
        if ctx.identity_verified and ctx.contact:
            raw_external = ctx.contact.get("external_id")
            external_id = str(raw_external) if raw_external else None

        collected: dict[str, str] = {}
        if company:
            collected["company"] = company
        if note:
            collected["lead_note"] = note

        try:
            async with tenant_scope(ctx.tenant_id) as session:
                existing: dict[str, Any] | None = None
                if ctx.conversation_id is not None:
                    existing = await contact_store.load_conversation_contact(
                        session, ctx.tenant_id, ctx.conversation_id
                    )
                if existing is None:
                    existing = await contact_store.find_contact(
                        session,
                        ctx.tenant_id,
                        external_id=external_id,
                        email=email,
                        phone=phone,
                    )

                conflicts: list[str] = []
                if existing is None:
                    contact_id = await contact_store.insert_contact(
                        session,
                        ctx.tenant_id,
                        external_id=external_id,
                        email=email,
                        phone=phone,
                        name=name,
                        source=SOURCE,
                        attributes=contact_store.sanitise_attributes(
                            collected, max_keys=max_keys, max_length=max_length
                        ),
                    )
                    created = True
                else:
                    contact_id = existing["id"]
                    created = False
                    verified = existing.get("verified_at") is not None
                    attributes = dict(existing.get("attributes") or {})
                    if verified:
                        # Record, never merge. `lead_*` keys keep the claim next to
                        # the real value so support can compare them.
                        claimed = {"email": email, "phone": phone, "name": name}
                        for field in IDENTITY_FIELDS:
                            value = claimed[field]
                            if value and str(existing.get(field) or "") != value:
                                conflicts.append(field)
                                collected[f"lead_{field}"] = value
                    else:
                        await contact_store.fill_unverified_contact(
                            session,
                            ctx.tenant_id,
                            contact_id,
                            email=email,
                            phone=phone,
                            name=name,
                        )
                    merged = attributes | contact_store.sanitise_attributes(
                        collected, max_keys=max_keys, max_length=max_length
                    )
                    await contact_store.set_attributes(
                        session,
                        ctx.tenant_id,
                        contact_id,
                        contact_store.sanitise_attributes(
                            merged, max_keys=max_keys, max_length=max_length
                        ),
                    )

                await contact_store.record_event(
                    session,
                    ctx.tenant_id,
                    contact_id=contact_id,
                    conversation_id=ctx.conversation_id,
                    kind="lead_collected",
                    payload={
                        "source": SOURCE,
                        "name": name,
                        "email": email,
                        "phone": phone,
                        "company": company,
                        "note": note,
                        "conflicts": conflicts,
                        "identity_verified": ctx.identity_verified,
                        "channel": str(ctx.channel),
                    },
                )
                attached = False
                if ctx.conversation_id is not None:
                    attached = await contact_store.attach_contact(
                        session, ctx.tenant_id, ctx.conversation_id, contact_id
                    )
        except Exception as exc:
            logger.warning("collect_lead_failed", error=str(exc))
            return failed(
                ActionOutcomeCode.TRANSPORT_ERROR,
                "the details could not be saved just now",
                duration_ms=clock.ms,
            )

        saved = [
            field
            for field, value in (
                ("name", name),
                ("email", email),
                ("phone", phone),
                ("company", company),
            )
            if value and field not in conflicts
        ]
        receipts = await deliver(
            service.notifier,
            subject="New lead from the assistant",
            body=note or "The assistant collected new contact details.",
            urgency="normal",
            meta={
                "contact_id": str(contact_id),
                "name": name,
                "email": email,
                "phone": phone,
                "company": company,
                "conversation_id": str(ctx.conversation_id or ""),
            },
        )
        logger.info(
            "action_lead_collected",
            tenant_id=str(ctx.tenant_id),
            contact_id=str(contact_id),
            created=created,
            attached=attached,
            conflicts=len(conflicts),
            notified=any(receipt.get("delivered") for receipt in receipts),
        )
        data: dict[str, Any] = {
            "status": "saved" if created else "updated",
            "saved_fields": saved,
        }
        if conflicts:
            # The model has to be told, or it will claim it changed something it did
            # not. The visitor is already signed in as someone with these details.
            data["not_changed"] = conflicts
            data["message"] = (
                "The details were noted for the team, but this visitor is already "
                "signed in, so their account details were left as they are."
            )
        else:
            data["message"] = "The details were saved for the team to follow up."
        return succeeded(data, duration_ms=clock.ms)

    return handle
