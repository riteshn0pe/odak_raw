"""Slack incoming-webhook notifier for escalations.

A Slack incoming-webhook URL *is* the credential — anyone holding it can post into
that channel — so it is stored in ``tenant_secrets`` and only ever arrives here as
a resolved string, never as agent config.

The URL is also tenant-supplied, which makes this an SSRF surface: a tenant that
sets its "Slack webhook" to ``http://169.254.169.254/latest/meta-data/`` would have
us POST our own escalation payload at a cloud metadata endpoint. Two controls,
both mandatory: the host must be exactly ``hooks.slack.com``, and the socket's peer
address is checked on the open response before anything is read.

Failure is returned as a ``NotifyReceipt``, never raised. A Slack outage must not
stop the escalation email, and neither must fail the visitor's answer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from app.core.breaker import guard
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.core.urlguard import assert_fetchable, assert_peer_public
from app.domain.ports import NotifyReceipt

if TYPE_CHECKING:
    from app.domain.escalation import EscalationEvent

logger = get_logger(__name__)

SLACK_WEBHOOK_HOST = "hooks.slack.com"
_CHANNEL = "slack"


def assert_slack_webhook_url(url: str) -> None:
    """Raise ``ValidationError`` unless this is a real Slack webhook URL.

    An allowlist of one host, checked before the DNS and address rules in
    ``assert_fetchable``. A generic "is this public" check would happily post a
    tenant's escalation payload to any server on the internet, which is an
    exfiltration primitive dressed up as a feature.
    """
    if not url:
        raise ValidationError("slack webhook url is required")
    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        raise ValidationError("slack webhook url must be https")
    if (parsed.hostname or "").lower() != SLACK_WEBHOOK_HOST:
        raise ValidationError(f"slack webhook url host must be {SLACK_WEBHOOK_HOST}")
    if not parsed.path.startswith("/services/"):
        raise ValidationError("slack webhook url must be an incoming-webhook path")
    assert_fetchable(url, allowed_ports=frozenset({443}))


class SlackWebhookNotifier:
    """EscalationNotifier that posts Block Kit JSON to one incoming webhook.

    The renderer is injected rather than imported: rendering lives in
    ``app/services/escalation/render.py``, and an adapter that imported a service
    would invert the dependency this project is built on.
    """

    def __init__(
        self,
        webhook_url: str,
        renderer: Callable[[EscalationEvent], dict[str, Any]],
        *,
        timeout: float = 10.0,
    ) -> None:
        assert_slack_webhook_url(webhook_url)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._url = webhook_url.strip()
        self._render = renderer
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            # a redirect from a validated host to an arbitrary one would undo the
            # allowlist, so redirects are refused rather than followed
            follow_redirects=False,
            headers={"content-type": "application/json"},
        )

    @property
    def name(self) -> str:
        return _CHANNEL

    async def notify(self, event: EscalationEvent) -> NotifyReceipt:
        payload = self._render(event)
        try:
            with guard(_CHANNEL):
                response = await self._client.post(self._url, json=payload)
                assert_peer_public(response)
        except ValidationError as exc:
            return NotifyReceipt(channel=_CHANNEL, error=f"blocked: {exc}")
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning("slack_notify_failed", error=str(exc))
            return NotifyReceipt(channel=_CHANNEL, error=str(exc)[:200])

        if response.status_code >= 400:
            # Slack answers a revoked webhook with 404 plus a plain-text body; the
            # body is what a tenant needs to see, so it is kept verbatim and short
            body = (response.text or "").strip()[:120]
            return NotifyReceipt(
                channel=_CHANNEL, error=f"HTTP {response.status_code}: {body}"
            )
        return NotifyReceipt(channel=_CHANNEL, delivered=True)

    async def close(self) -> None:
        await self._client.aclose()
