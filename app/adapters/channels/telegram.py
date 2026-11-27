"""Telegram Bot API notifier for approval requests, plus reply formatting.

Telegram is two surfaces now. The owner-facing one — approvals, alerts, the
interactive menus — is unchanged: its credentials are per tenant in
``tenant_secrets`` and nothing about it appears in ``growth_messages``, because
the consent, suppression and window machinery there governs *outreach*, which
this is not. The customer-facing one (``app/services/channels/telegram.py``)
answers a customer's chat message with the tenant's default agent; it too never
writes ``growth_messages``, for the same reason — a support reply is not a
campaign message.

Two credentials, both per tenant, both in ``tenant_secrets``: the bot token,
which *is* the bot — anyone holding it can send as it and read its updates — and
the chat id, which is only an address but is stored beside the token because
splitting a credential pair across two stores is how half a pair goes missing.
Neither is ever cached on the container; they are resolved per notification, the
same rule the Slack webhook follows.

Unlike Slack this is not an SSRF surface, because the tenant supplies no URL —
the host is our constant and only the path segment carries the token. The host is
still asserted rather than merely formatted in, so a future refactor that makes
the base URL configurable cannot quietly turn this into one, and the peer address
is still checked on the open response.

Failure is returned as a ``NotifyReceipt``, never raised. A Telegram outage must
leave the WhatsApp ping, the Slack ping and the web panel working; an approval
nobody received expires and the batch is held, which is the safe direction.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from app.core.breaker import guard
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.core.urlguard import assert_peer_public
from app.domain.ports import NotifyReceipt

if TYPE_CHECKING:
    from app.domain.ports import ApprovalRequest

logger = get_logger(__name__)

TELEGRAM_API_HOST = "api.telegram.org"
TELEGRAM_API_BASE = f"https://{TELEGRAM_API_HOST}"
_CHANNEL = "telegram"

# Telegram rejects a sendMessage body over 4096 characters outright, so the
# renderer truncates to this and the adapter enforces it again. A notification
# that fails to send because it was one line too long is the worst possible
# failure mode for an approval: the money action silently never gets approved.
MAX_MESSAGE_CHARS = 4096

# The chunk ceiling for a customer reply, deliberately below the hard cap above
# for the same reason the WhatsApp chunker sits at 3500 under Meta's 4096: the
# formatting pass below can grow a chunk (escaping, link tags), and the
# truncation guard is a last resort rather than a plan.
TELEGRAM_MAX_MESSAGE_LENGTH = 3500


def assert_telegram_bot_token(token: str) -> None:
    """Raise ``ValidationError`` unless this looks like a bot token.

    ``<digits>:<35-ish urlsafe chars>``. The check is shape-only — Telegram is
    the only authority on whether a token is live — but it is worth doing here
    because the token goes into a URL *path*, and a value containing a slash or a
    space would silently retarget the request at a different endpoint on the same
    host. That is a smaller hole than SSRF and it is still a hole.
    """
    if not token or not token.strip():
        raise ValidationError("telegram bot token is required")
    candidate = token.strip()
    if any(c.isspace() for c in candidate) or "/" in candidate or "?" in candidate:
        raise ValidationError("telegram bot token contains invalid characters")
    head, _, tail = candidate.partition(":")
    if not head.isdigit() or len(tail) < 20:
        raise ValidationError("telegram bot token must look like <bot_id>:<secret>")


def assert_telegram_chat_id(chat_id: str) -> None:
    """Raise ``ValidationError`` unless this is a chat id or an @channel name.

    A numeric id (negative for groups) or an ``@public_name``. Anything else is a
    typo, and a typo here means the owner is never asked.
    """
    value = (chat_id or "").strip()
    if not value:
        raise ValidationError("telegram chat id is required")
    if value.startswith("@"):
        if len(value) < 6 or any(c.isspace() for c in value):
            raise ValidationError("telegram channel name is not valid")
        return
    if not value.lstrip("-").isdigit():
        raise ValidationError("telegram chat id must be numeric or an @channel name")


class TelegramApprovalNotifier:
    """Telegram Bot API sender for the owner's chat.

    The renderer is injected rather than imported, for the reason the Slack
    adapter states: what an approval *says* is a product decision that lives in
    ``app/services/approvals/render.py``, and an adapter importing a service
    would invert the dependency this project is built on.

    Approval buttons are Telegram callback buttons (``ap:<id>:y``), not URL
    buttons: the webhook is reachable (the tenant pointed the bot's webhook at
    the ingress), and the presser is authenticated by the sender gate — the
    chat id recorded on the integration row — before any callback is acted on
    (P6, ``app/services/growth/telegram_bot.py``). The panel remains the
    fallback surface with its own authenticated decide endpoint.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        renderer: Callable[[ApprovalRequest], dict[str, Any]],
        *,
        timeout: float = 10.0,
        base_url: str = TELEGRAM_API_BASE,
    ) -> None:
        assert_telegram_bot_token(bot_token)
        assert_telegram_chat_id(chat_id)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != TELEGRAM_API_HOST:
            raise ValidationError(f"telegram base url host must be {TELEGRAM_API_HOST}")

        self._chat_id = chat_id.strip()
        self._url = f"{base_url.rstrip('/')}/bot{bot_token.strip()}/sendMessage"
        self._render = renderer
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            # the token is in the path, so a redirect we followed would hand the
            # credential to whatever host answered it
            follow_redirects=False,
            headers={"content-type": "application/json"},
        )

    @property
    def name(self) -> str:
        return _CHANNEL

    async def request(self, approval: ApprovalRequest) -> NotifyReceipt:
        payload = dict(self._render(approval))
        payload["chat_id"] = self._chat_id
        text = str(payload.get("text") or "")
        if len(text) > MAX_MESSAGE_CHARS:
            payload["text"] = text[: MAX_MESSAGE_CHARS - 1] + "…"

        response = await self._send("sendMessage", payload)
        if response is None:
            return NotifyReceipt(channel=_CHANNEL, error="send failed")
        if isinstance(response, NotifyReceipt):
            return response
        return self._receipt(response)

    async def send_payload(self, payload: dict[str, Any]) -> NotifyReceipt:
        """Send one pre-rendered sendMessage body to this notifier's chat.

        The approval path renders through the injected renderer; the report
        agent renders its own message (summary plus panel buttons, no decision
        deadline) and sends it here, so both share the host assertion, the
        peer check and the receipt-not-exception contract without sharing copy.
        """
        body = dict(payload)
        body["chat_id"] = self._chat_id
        text = str(body.get("text") or "")
        if len(text) > MAX_MESSAGE_CHARS:
            body["text"] = text[: MAX_MESSAGE_CHARS - 1] + "…"
        response = await self._send("sendMessage", body)
        if isinstance(response, NotifyReceipt):
            return response
        return self._receipt(response)

    async def send_message(
        self,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        *,
        parse_mode: str = "HTML",
    ) -> NotifyReceipt:
        """Send one text message to this notifier's chat, with any markup.

        The interactive surfaces (P6) render their own copy and keyboards; this
        is the same wire path as ``send_payload`` — host assertion, peer check,
        receipt-not-exception — without forcing every caller to build a full
        sendMessage body by hand.
        """
        body: dict[str, Any] = {"text": text, "parse_mode": parse_mode}
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        return await self.send_payload(body)

    async def edit_message(
        self,
        chat_id: str,
        message_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        *,
        parse_mode: str = "HTML",
    ) -> NotifyReceipt:
        """editMessageText on one earlier message, so a message becomes its own
        receipt: the keyboard it carries is the whole state machine (P6).

        ``chat_id`` is passed rather than reused from the credential, because a
        callback names the chat it came from and the edit must land in that
        same chat — which is the owner's chat, but that is the caller's gate to
        enforce, not the adapter's to assume.
        """
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        response = await self._send("editMessageText", body)
        if response is None:
            return NotifyReceipt(channel=_CHANNEL, error="edit failed")
        if isinstance(response, NotifyReceipt):
            return response
        return self._receipt(response)

    async def answer_callback(self, callback_query_id: str, text: str = "") -> NotifyReceipt:
        """answerCallbackQuery: the fast ack Telegram requires on a press.

        Fire-and-forget by contract: the ack window is seconds, so the caller
        sends it before acting and ignores a late-ack error the same way it
        ignores a notification outage — the action, not the ack, is the work.
        """
        body: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            body["text"] = text[:200]
        response = await self._send("answerCallbackQuery", body)
        if response is None:
            return NotifyReceipt(channel=_CHANNEL, error="ack failed")
        if isinstance(response, NotifyReceipt):
            return response
        return self._receipt(response)

    async def send_document(
        self, filename: str, data: bytes, caption: str = ""
    ) -> NotifyReceipt:
        """Send one PDF (the report) as a document to this notifier's chat.

        Multipart upload rather than JSON: the bytes ride in the same request
        as the caption, so a sent document and its description cannot part
        ways. Size is capped at 10MB — a report PDF is kilobytes, and an
        unbounded upload on a per-tenant credential is a cost primitive.
        """
        if not data or len(data) > 10 * 1024 * 1024:
            return NotifyReceipt(channel=_CHANNEL, error="document is empty or too large")
        try:
            with guard(_CHANNEL):
                response = await self._client.post(
                    self._url.replace("/sendMessage", "/sendDocument"),
                    data={
                        "chat_id": self._chat_id,
                        "caption": caption[:1024],
                    },
                    files={
                        "document": (
                            filename or "report.pdf",
                            data,
                            "application/pdf",
                        )
                    },
                )
                assert_peer_public(response)
        except ValidationError as exc:
            return NotifyReceipt(channel=_CHANNEL, error=f"blocked: {exc}")
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning("telegram_notify_failed", error=type(exc).__name__)
            return NotifyReceipt(channel=_CHANNEL, error=str(type(exc).__name__)[:200])
        return self._receipt(response)

    async def _send(self, method: str, payload: dict[str, Any]) -> Any:
        """POST one JSON body. Returns the response, or a receipt on failure.

        A receipt rather than an exception, because a Telegram outage must
        leave every other channel working — the caller checks `delivered`
        instead of catching.
        """
        try:
            with guard(_CHANNEL):
                response = await self._client.post(
                    self._url.replace("/sendMessage", f"/{method}"), json=payload
                )
                assert_peer_public(response)
                return response
        except ValidationError as exc:
            return NotifyReceipt(channel=_CHANNEL, error=f"blocked: {exc}")
        except (httpx.HTTPError, RuntimeError) as exc:
            # the token is in the URL, so the exception string is never logged:
            # httpx puts the request URL in its message
            logger.warning("telegram_notify_failed", error=type(exc).__name__)
            return NotifyReceipt(channel=_CHANNEL, error=str(type(exc).__name__)[:200])

    def _receipt(self, response: Any) -> NotifyReceipt:
        if response.status_code >= 400:
            # Telegram answers errors as JSON with a `description`; the raw body
            # is a fallback for a proxy failure that is not Telegram at all
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("description") or "")[:120]
            except ValueError:
                detail = (response.text or "").strip()[:120]
            return NotifyReceipt(
                channel=_CHANNEL, error=f"HTTP {response.status_code}: {detail}"
            )

        message_id: str | None = None
        try:
            payload = response.json()
            # `result` is a Message object for the send/edit methods but a plain
            # bool for the acknowledgement ones — answerCallbackQuery answers
            # `true`, and calling `.get` on that crashed the webhook on every
            # inline-button press. Only a mapping can carry a message id.
            result = payload.get("result") if isinstance(payload, dict) else None
            raw_id = result.get("message_id") if isinstance(result, dict) else None
            message_id = str(raw_id) if raw_id is not None else None
        except ValueError:
            # a 200 with an unparseable body still delivered; losing the id only
            # costs us the ability to edit the message later
            logger.warning("telegram_response_unparseable")

        return NotifyReceipt(
            channel=_CHANNEL, delivered=True, provider_message_id=message_id
        )

    async def close(self) -> None:
        await self._client.aclose()
