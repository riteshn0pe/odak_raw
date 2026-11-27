"""Shared plumbing for the starter action handlers.

Every handler in this package is awaited as
`handler(action=..., arguments=..., ctx=...)` and returns an `ActionOutcome` on
every path, including its failure paths. The dispatcher does catch a raise, but
that safety net produces a generic refusal, so a handler that leans on it loses
the diagnosable outcome code the tenant is shown.

Three things live here rather than in each module. First, argument reading:
model-supplied values arrive as whatever JSON the provider emitted, and a string
that should be an email may still be a PII mask label, so reading an argument
means restoring it through `ctx.restore` before validating it. Second, the
outcome constructors, so the same failure never gets two different codes in two
handlers. Third, notifier delivery, which is duck-typed on purpose: no
`Notifier` protocol ships in this checkout and `ActionService.notifier` is
documented as possibly None, so a deployment without one loses a notification
rather than an action.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from app.core.logging import get_logger
from app.domain.actions import (
    ActionContext,
    ActionDefinition,
    ActionOutcome,
    ActionOutcomeCode,
)

logger = get_logger(__name__)

# Deliberately permissive: this is a shape check that catches "i dont have one"
# and a mask label that failed to restore, not an RFC 5322 parser. The tenant's
# own system is the authority on whether an address exists.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")
PHONE_RE = re.compile(r"^\+?\d[\d\s().-]{5,24}$")
# What `_mask` leaves behind ([EMAIL_1], [PHONE_2]). An argument still carrying
# one of these means the turn had no restore closure, and writing the label into
# a contacts row would persist a placeholder as a customer's email address.
MASK_LABEL_RE = re.compile(r"\[[A-Z][A-Z_]*_\d+\]")


class Handler(Protocol):
    """The dispatcher's handler shape, from `ActionService._handler_for`."""

    async def __call__(
        self,
        *,
        action: ActionDefinition,
        arguments: dict[str, Any],
        ctx: ActionContext,
    ) -> ActionOutcome: ...


class InvalidArgumentError(ValueError):
    """A model-supplied argument the handler will not act on.

    Carries the sentence the model reads, so it can ask the visitor again rather
    than apologising in the abstract.
    """


class Args:
    """Typed, restored reads over the model's raw argument object."""

    __slots__ = ("_ctx", "_raw")

    def __init__(self, arguments: Mapping[str, Any], ctx: ActionContext) -> None:
        self._raw = arguments
        self._ctx = ctx

    def _restore(self, value: str) -> str:
        restore = self._ctx.restore
        if restore is None:
            return value
        try:
            return restore(value)
        except Exception:  # pragma: no cover - a tenant closure must not kill a turn
            logger.warning("action_argument_restore_failed")
            return value

    def raw(self, name: str) -> Any:
        return self._raw.get(name)

    def text(
        self,
        name: str,
        *,
        required: bool = False,
        min_length: int = 0,
        max_length: int = 1000,
        restore: bool = True,
    ) -> str | None:
        value = self._raw.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise InvalidArgumentError(f"{name} is required")
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise InvalidArgumentError(f"{name} must be text")
        text = str(value).strip()
        if restore:
            text = self._restore(text).strip()
        if len(text) < min_length:
            raise InvalidArgumentError(f"{name} must be at least {min_length} characters")
        if len(text) > max_length:
            text = text[:max_length]
        return text

    def choice(self, name: str, options: Sequence[str], *, default: str) -> str:
        value = self._raw.get(name)
        if value is None:
            return default
        text = str(value).strip().lower()
        if text not in options:
            raise InvalidArgumentError(f"{name} must be one of {', '.join(options)}")
        return text

    def email(self, name: str, *, required: bool = False) -> str | None:
        value = self.text(name, required=required, max_length=254)
        if value is None:
            return None
        value = value.lower()
        if MASK_LABEL_RE.fullmatch(value.upper()):
            raise InvalidArgumentError(f"{name} did not resolve to a real address")
        if not EMAIL_RE.match(value):
            raise InvalidArgumentError(f"{name} is not a valid email address")
        return value

    def phone(self, name: str, *, required: bool = False) -> str | None:
        value = self.text(name, required=required, max_length=32)
        if value is None:
            return None
        if MASK_LABEL_RE.fullmatch(value.upper()):
            raise InvalidArgumentError(f"{name} did not resolve to a real number")
        if not PHONE_RE.match(value):
            raise InvalidArgumentError(f"{name} is not a valid phone number")
        return value


class Elapsed:
    """Monotonic stopwatch. Every outcome carries a duration for the ledger."""

    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = time.monotonic()

    @property
    def ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)


def invalid(message: str, *, duration_ms: int = 0) -> ActionOutcome:
    return ActionOutcome.failure(
        ActionOutcomeCode.INVALID_ARGUMENTS, message, duration_ms=duration_ms
    )


def not_configured(action: ActionDefinition, detail: str = "") -> ActionOutcome:
    """The one message shape the Actions card's "Not configured" state mirrors."""
    tail = f": {detail}" if detail else " on this deployment"
    return ActionOutcome.failure(
        ActionOutcomeCode.NOT_CONFIGURED,
        f"{action.display_name} is not available{tail}",
    )


def failed(
    code: str,
    message: str,
    *,
    duration_ms: int = 0,
    http_status: int | None = None,
) -> ActionOutcome:
    return ActionOutcome.failure(
        code, message, duration_ms=duration_ms, http_status=http_status
    )


def succeeded(
    data: dict[str, Any],
    *,
    duration_ms: int = 0,
    quota_exempt: bool = False,
    render: dict[str, Any] | None = None,
) -> ActionOutcome:
    return ActionOutcome(
        ok=True,
        outcome=ActionOutcomeCode.OK,
        data=data,
        duration_ms=duration_ms,
        render=render,
        quota_exempt=quota_exempt,
    )


def rendered(
    render: dict[str, Any],
    *,
    data: dict[str, Any] | None = None,
    duration_ms: int = 0,
) -> ActionOutcome:
    return ActionOutcome(
        ok=True,
        outcome=ActionOutcomeCode.RENDERED,
        data=data if data is not None else {"rendered": render.get("type", "button")},
        render=render,
        duration_ms=duration_ms,
    )


def settings_for(service: Any) -> Any:
    """The `Settings` the service was built with, or None.

    `ActionService` keeps settings private; handlers need them for the encryption
    key and the web-search backend. Read through a getattr pair so a service
    stub without either attribute degrades to "not configured" instead of
    raising an AttributeError inside a turn.
    """
    return getattr(service, "settings", None) or getattr(service, "_settings", None)


def json_dump(value: Any) -> str:
    """JSONB parameter serialisation. `default=str` because UUIDs and datetimes
    reach here through model-supplied and row-derived payloads alike."""
    return json.dumps(value, default=str, ensure_ascii=False)


def clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


async def deliver(
    notifier: Any,
    *,
    subject: str,
    body: str,
    urgency: str = "normal",
    meta: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Best-effort notification, returned as `escalations.receipts` entries.

    Duck-typed across `notify` and `send` because no `Notifier` protocol ships
    here yet; an object with neither is treated as no notifier at all. A failure
    is recorded as an undelivered receipt, never raised: the durable row is the
    record of the escalation, and the notification is a courtesy on top of it.
    """
    if notifier is None:
        return []
    call = getattr(notifier, "notify", None) or getattr(notifier, "send", None)
    if not callable(call):
        logger.info("action_notifier_shape_unknown")
        return []
    try:
        result = call(
            subject=subject, body=body, urgency=urgency, meta=dict(meta or {})
        )
        if hasattr(result, "__await__"):
            result = await result
    except Exception as exc:
        logger.warning("action_notify_failed", error=str(exc))
        return [{"channel": "notifier", "delivered": False, "error": str(exc)[:200]}]
    if isinstance(result, list):
        return [dict(item) for item in result if isinstance(item, Mapping)]
    message_id = None
    if isinstance(result, Mapping):
        message_id = result.get("id") or result.get("provider_message_id")
    elif isinstance(result, str):
        message_id = result
    return [
        {
            "channel": "notifier",
            "delivered": True,
            "provider_message_id": str(message_id) if message_id else None,
            "error": None,
        }
    ]
