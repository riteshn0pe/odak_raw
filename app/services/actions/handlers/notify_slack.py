"""`notify_slack` — post a short note to the team's channel.

Prebuilt rather than a generic HTTP action for one reason: a Slack incoming
webhook URL *is* the credential. Putting it in an HTTP action's URL field would
write a secret into a plain column and then show it in the Actions UI and in test
output. So it lives in `tenant_secrets` and the definition carries only a
`secret_ref`.

Two delivery paths, in order. A configured notifier on the service wins, because
a deployment that has one has already centralised destination policy and
retries. Otherwise the webhook is resolved from `tenant_secrets` and posted over
raw httpx. `ActionService.notifier` is documented as possibly None and no
`Notifier` protocol ships in this checkout, so None is the expected case rather
than an error — with neither path available the action reports NOT_CONFIGURED,
which is exactly what makes the Actions card read "Not configured" instead of
failing a turn.

The destination host is allowlisted. A tenant secret that is not a Slack webhook
URL is refused rather than fetched: a credential store that can name any host is
an SSRF primitive with extra steps.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from app.adapters.db.actions_repo import ActionSecretResolver
from app.core.logging import get_logger
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
    not_configured,
    settings_for,
    succeeded,
)

logger = get_logger(__name__)

WEBHOOK_HOSTS = frozenset({"hooks.slack.com"})
URGENCIES = ("normal", "high")
MAX_MESSAGE = 1000
TIMEOUT_SECONDS = 8.0


def _webhook_ok(url: str) -> bool:
    if any(character in url for character in ("\r", "\n", "\0", " ")):
        return False
    parsed = urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in WEBHOOK_HOSTS


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
            message = args.text(
                "message", required=True, min_length=10, max_length=MAX_MESSAGE
            )
            urgency = args.choice("urgency", URGENCIES, default="normal")
        except InvalidArgumentError as exc:
            return invalid(str(exc), duration_ms=clock.ms)
        if message is None:  # pragma: no cover - required=True already raised
            return invalid("message is required", duration_ms=clock.ms)

        label = action.display_name
        prefix = "*Urgent* " if urgency == "high" else ""
        body = clip(f"{prefix}{message}", MAX_MESSAGE)

        receipts = await deliver(
            service.notifier,
            subject=f"Assistant note ({urgency})",
            body=body,
            urgency=urgency,
            meta={
                "conversation_id": str(ctx.conversation_id or ""),
                "channel": str(ctx.channel),
                "action": action.name,
            },
        )
        if receipts:
            if any(receipt.get("delivered") for receipt in receipts):
                return succeeded(
                    {"status": "posted", "channel": label},
                    duration_ms=clock.ms,
                )
            return failed(
                ActionOutcomeCode.CHANNEL_UNAVAILABLE,
                f"{label} could not be reached just now",
                duration_ms=clock.ms,
            )

        settings = settings_for(service)
        secret_ref = action.auth.secret_ref
        if settings is None or not secret_ref:
            return not_configured(action, "no channel is connected yet")
        webhook = await ActionSecretResolver(settings).resolve(ctx.tenant_id, secret_ref)
        if not webhook:
            return not_configured(action, "the channel webhook is missing")
        if not _webhook_ok(webhook):
            logger.warning(
                "notify_slack_webhook_rejected", tenant_id=str(ctx.tenant_id)
            )
            return failed(
                ActionOutcomeCode.BLOCKED_SSRF,
                f"{label} is configured with a destination that is not a Slack webhook",
                duration_ms=clock.ms,
            )

        payload = {
            "text": body,
            "attachments": [
                {
                    "color": "#d93025" if urgency == "high" else "#1264a3",
                    "footer": f"via {label}",
                }
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.post(webhook, json=payload)
        except httpx.TimeoutException:
            return failed(
                ActionOutcomeCode.TIMEOUT,
                f"{label} did not respond in time",
                duration_ms=clock.ms,
            )
        except httpx.HTTPError as exc:
            logger.warning("notify_slack_transport_failed", error=str(exc))
            return failed(
                ActionOutcomeCode.TRANSPORT_ERROR,
                f"{label} could not be reached",
                duration_ms=clock.ms,
            )
        if response.status_code >= 400:
            # Slack answers a dead or revoked webhook with 404/403 and a one-word
            # body. Surfaced as an http error so the Actions card can say which.
            logger.warning("notify_slack_rejected", status=response.status_code)
            return failed(
                ActionOutcomeCode.HTTP_ERROR,
                f"{label} rejected the message",
                duration_ms=clock.ms,
                http_status=response.status_code,
            )
        logger.info(
            "action_slack_notified",
            tenant_id=str(ctx.tenant_id),
            urgency=urgency,
            action=action.name,
        )
        return succeeded(
            {"status": "posted", "channel": label},
            duration_ms=clock.ms,
        )

    return handle
