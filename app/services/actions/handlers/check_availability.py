"""`check_availability` — `book_meeting`'s companion, auto-installed alongside it.

A separate action rather than a step folded into `book_meeting`, because the model
needs real slots in front of it before it can book anything; folding availability
into the booking call would force that call to guess a time. It is `effect='read'`
and no agent template references it directly.

Returns up to ten normalised slots, in the tenant's timezone, or an empty list —
never an invented time.
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
    succeeded,
)

logger = get_logger(__name__)


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
            requested = args.text("service", max_length=120, restore=False)
            from_date = args.text("from_date", required=True, max_length=32)
            to_date = args.text("to_date", max_length=32)
        except InvalidArgumentError as exc:
            return invalid(str(exc), duration_ms=clock.ms)
        if from_date is None:  # pragma: no cover - required=True already raised
            return invalid("from_date is required", duration_ms=clock.ms)

        config = await calendar_provider.load_config(service, action, ctx)
        if config is None:
            return not_configured(action, "no calendar is connected yet")
        try:
            slots = await calendar_provider.fetch_slots(
                service,
                config,
                service_name=requested,
                from_date=from_date,
                to_date=to_date or "",
            )
        except calendar_provider.CalendarError as exc:
            logger.info(
                "check_availability_failed",
                tenant_id=str(ctx.tenant_id),
                code=exc.code,
            )
            return failed(
                exc.code,
                exc.message,
                duration_ms=clock.ms,
                http_status=exc.http_status,
            )

        if not slots:
            return succeeded(
                {
                    "slots": [],
                    "timezone": config.timezone,
                    "message": (
                        "There are no openings in that range. Ask the visitor for a "
                        "different date range; do not suggest a time yourself."
                    ),
                },
                duration_ms=clock.ms,
            )
        return succeeded(
            {
                "slots": [slot.as_dict() for slot in slots],
                "timezone": config.timezone,
                "message": (
                    "Offer some of these exact times. Only book a slot the visitor "
                    "picks from this list, using its `start` value."
                ),
            },
            duration_ms=clock.ms,
        )

    return handle
