"""`book_meeting` — the genuine write, or a scheduling link under Calendly.

`effect='write'` and `requires_confirmation=true`, so by the time this handler
runs the visitor has already said yes in their own turn: the executor parked the
call and resumed it. Nothing here re-asks, and nothing here re-checks a quota or a
rate limit — both are applied before dispatch.

Under `provider='calcom'` this creates the booking and returns the vendor's
reference. Under `provider='calendly'` it returns a scheduling link rendered as a
button, because Calendly does not offer booking on an invitee's behalf on the
tiers a small business is on. Under `provider='custom'` it posts to the tenant's
own endpoint.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.domain.actions import ActionContext, ActionDefinition, ActionOutcome
from app.services.actions.handlers import calendar_provider
from app.services.actions.handlers.base import (
    Args,
    Elapsed,
    Handler,
    InvalidArgumentError,
    failed,
    invalid,
    not_configured,
    rendered,
    succeeded,
)

logger = get_logger(__name__)

BUTTON_LABEL = "Choose your time"


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
            slot_start = args.text(
                "slot_start", required=True, max_length=40, restore=False
            )
            name = args.text("name", required=True, min_length=2, max_length=120)
            email = args.email("email", required=True)
            phone = args.phone("phone")
            notes = args.text("notes", max_length=500)
            requested = args.text("service", max_length=120, restore=False)
        except InvalidArgumentError as exc:
            return invalid(str(exc), duration_ms=clock.ms)
        if slot_start is None or name is None or email is None:
            # pragma: no cover - required=True already raised
            return invalid(
                "a time, a name and an email address are all needed to book",
                duration_ms=clock.ms,
            )

        config = await calendar_provider.load_config(service, action, ctx)
        if config is None:
            return not_configured(action, "no calendar is connected yet")
        try:
            result = await calendar_provider.create_booking(
                service,
                config,
                slot_start=slot_start,
                name=name,
                email=email,
                phone=phone,
                notes=notes,
                service_name=requested,
            )
        except calendar_provider.CalendarError as exc:
            logger.info(
                "book_meeting_failed",
                tenant_id=str(ctx.tenant_id),
                provider=config.provider,
                code=exc.code,
            )
            return failed(
                exc.code,
                exc.message,
                duration_ms=clock.ms,
                http_status=exc.http_status,
            )

        if result.get("mode") == "link":
            link = str(result["scheduling_url"])
            logger.info(
                "action_booking_link",
                tenant_id=str(ctx.tenant_id),
                provider=config.provider,
            )
            return rendered(
                {
                    "type": "button",
                    "label": BUTTON_LABEL,
                    "url": link,
                    "options": [],
                    "fields": [],
                },
                data={
                    "status": "link",
                    "scheduling_url": link,
                    "start": result.get("start"),
                    "message": (
                        "This calendar cannot be booked on the visitor's behalf. A "
                        "button to the booking page has been shown; tell them to "
                        "confirm the time there."
                    ),
                },
                duration_ms=clock.ms,
            )

        reference = str(result.get("reference") or "")
        logger.info(
            "action_meeting_booked",
            tenant_id=str(ctx.tenant_id),
            provider=config.provider,
            conversation_id=str(ctx.conversation_id or ""),
        )
        return succeeded(
            {
                "status": "confirmed",
                "reference": reference,
                "when": result.get("label"),
                "start": result.get("start"),
                "timezone": config.timezone,
                "message": (
                    f"The meeting is confirmed for {result.get('label')}. Give the "
                    f"visitor the reference {reference} and tell them a "
                    "confirmation email is on its way."
                ),
            },
            duration_ms=clock.ms,
        )

    return handle
