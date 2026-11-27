"""`collect_data` — the one system action whose input schema the tenant defines.

The tenant declares the fields on the Actions screen; those become the
definition's `parameters`, and `ActionDefinition.tool_spec()` already turns them
into the JSON Schema the model sees. So this handler does not know the field
names in advance: it reads, coerces and validates whatever the definition
declares, then writes the result to the three places §2.3 names —
`contacts.attributes` when the conversation has a contact, `conversations.
metadata.collected` when it does not, and `procedure_runs.variables` when a run
is active, so a later branch guard can reference what was collected.

One `contact_events` row with `kind='data_collected'` carries the whole
submission, so the audit trail reads as one event rather than as a scatter of
attribute writes.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.core.db import tenant_scope
from app.core.logging import get_logger
from app.domain.actions import (
    ActionContext,
    ActionDefinition,
    ActionOutcome,
    ActionOutcomeCode,
    ActionParameter,
    ParamType,
)
from app.services.actions.handlers import contact_store
from app.services.actions.handlers.base import (
    Args,
    Elapsed,
    Handler,
    InvalidArgumentError,
    failed,
    invalid,
    settings_for,
    succeeded,
)

logger = get_logger(__name__)

SOURCE = "collect_data"


def _read(args: Args, parameter: ActionParameter) -> Any:
    """One tenant-declared field, coerced to its declared type or refused.

    Refusing is the right move rather than coercing loosely: the value is about
    to be written into the tenant's own record of the request, and a number field
    holding "about three" is worse than a re-ask.
    """
    name = parameter.name
    match parameter.type:
        case ParamType.EMAIL:
            return args.email(name, required=parameter.required)
        case ParamType.ENUM:
            raw = args.text(name, required=parameter.required, max_length=200)
            if raw is None:
                return None
            for option in parameter.enum:
                if option.lower() == raw.lower():
                    return option
            raise InvalidArgumentError(f"{name} must be one of {', '.join(parameter.enum)}")
        case ParamType.DATE:
            raw = args.text(name, required=parameter.required, max_length=32)
            if raw is None:
                return None
            try:
                return date.fromisoformat(raw[:10]).isoformat()
            except ValueError as exc:
                raise InvalidArgumentError(f"{name} must be a date like 2026-03-14") from exc
        case ParamType.INTEGER | ParamType.NUMBER:
            raw = args.text(name, required=parameter.required, max_length=40)
            if raw is None:
                return None
            try:
                number: float | int = (
                    int(raw) if parameter.type is ParamType.INTEGER else float(raw)
                )
            except ValueError as exc:
                raise InvalidArgumentError(f"{name} must be a number") from exc
            if parameter.minimum is not None and number < parameter.minimum:
                raise InvalidArgumentError(f"{name} must be at least {parameter.minimum}")
            if parameter.maximum is not None and number > parameter.maximum:
                raise InvalidArgumentError(f"{name} must be at most {parameter.maximum}")
            return number
        case ParamType.BOOLEAN:
            raw_value = args.raw(name)
            if raw_value is None:
                if parameter.required:
                    raise InvalidArgumentError(f"{name} is required")
                return None
            if isinstance(raw_value, bool):
                return raw_value
            text_value = str(raw_value).strip().lower()
            if text_value in ("true", "yes", "y", "1"):
                return True
            if text_value in ("false", "no", "n", "0"):
                return False
            raise InvalidArgumentError(f"{name} must be yes or no")
        case _:
            value = args.text(
                name, required=parameter.required, max_length=parameter.max_length
            )
            if (
                value is not None
                and parameter.pattern is not None
                # the pattern is tenant data, already compiled once at save time
                and not re.match(parameter.pattern, value)
            ):
                raise InvalidArgumentError(f"{name} is not in the expected format")
            return value


def build(service: Any) -> Handler:
    async def handle(
        *,
        action: ActionDefinition,
        arguments: dict[str, Any],
        ctx: ActionContext,
    ) -> ActionOutcome:
        clock = Elapsed()
        args = Args(arguments, ctx)
        if not action.parameters:
            # A collect_data action with no declared fields has nothing to record;
            # the Actions card shows it as needing setup.
            return failed(
                ActionOutcomeCode.NOT_CONFIGURED,
                f"{action.display_name} has no fields configured yet",
                duration_ms=clock.ms,
            )
        values: dict[str, Any] = {}
        try:
            for parameter in action.parameters:
                value = _read(args, parameter)
                if value is not None:
                    values[parameter.name] = value
        except InvalidArgumentError as exc:
            return invalid(str(exc), duration_ms=clock.ms)
        if not values:
            return invalid(
                "none of the details were filled in; ask the visitor for them one "
                "or two at a time and call this again",
                duration_ms=clock.ms,
            )

        settings = settings_for(service)
        max_keys = int(getattr(settings, "CONTACT_MAX_ATTRIBUTES", 25) or 25)
        max_length = int(getattr(settings, "CONTACT_ATTRIBUTE_MAX_LENGTH", 200) or 200)
        stored: list[str] = []
        try:
            async with tenant_scope(ctx.tenant_id) as session:
                contact: dict[str, Any] | None = None
                if ctx.conversation_id is not None:
                    contact = await contact_store.load_conversation_contact(
                        session, ctx.tenant_id, ctx.conversation_id
                    )
                if contact is not None:
                    attributes = dict(contact.get("attributes") or {})
                    merged = attributes | contact_store.sanitise_attributes(
                        values, max_keys=max_keys, max_length=max_length
                    )
                    await contact_store.set_attributes(
                        session,
                        ctx.tenant_id,
                        contact["id"],
                        contact_store.sanitise_attributes(
                            merged, max_keys=max_keys, max_length=max_length
                        ),
                    )
                    await contact_store.record_event(
                        session,
                        ctx.tenant_id,
                        contact_id=contact["id"],
                        conversation_id=ctx.conversation_id,
                        kind="data_collected",
                        payload={
                            "source": SOURCE,
                            "action": action.name,
                            "values": values,
                            "channel": str(ctx.channel),
                        },
                    )
                    stored.append("contact")
                elif ctx.conversation_id is not None:
                    await contact_store.store_conversation_collected(
                        session, ctx.tenant_id, ctx.conversation_id, values
                    )
                    stored.append("conversation")

                if ctx.procedure_run_id is not None:
                    await contact_store.store_run_variables(
                        session, ctx.tenant_id, ctx.procedure_run_id, values
                    )
                    stored.append("procedure_run")
        except Exception as exc:
            logger.warning("collect_data_failed", error=str(exc), action=action.name)
            return failed(
                ActionOutcomeCode.TRANSPORT_ERROR,
                "the details could not be recorded just now",
                duration_ms=clock.ms,
            )

        if not stored:
            # No conversation and no run: nowhere tenant-scoped to put it. Better a
            # legible refusal than a silent success that loses the submission.
            logger.warning("collect_data_no_destination", action=action.name)
            return failed(
                ActionOutcomeCode.NOT_CONFIGURED,
                f"{action.display_name} has nowhere to record this",
                duration_ms=clock.ms,
            )
        logger.info(
            "action_data_collected",
            tenant_id=str(ctx.tenant_id),
            action=action.name,
            fields=len(values),
            stored=",".join(stored),
        )
        return succeeded(
            {
                "status": "recorded",
                "fields": sorted(values),
                "message": "The details were recorded for the team.",
            },
            duration_ms=clock.ms,
        )

    return handle
