"""Resend adapter: outbound send, inbound fetch, and svix webhook verification.

Raw ``httpx``, no vendor SDK — the same rule the Meta adapter follows. A send is a
single JSON POST and the SDK would only add a dependency that has to be trusted
with an API key.

Resend is the provider because its webhooks are HMAC-signed (svix). A provider
whose "webhook security" is a shared secret in a query string cannot be verified
before parsing, and an unauthenticated inbound endpoint that starts an LLM turn is
a way to spend somebody else's money.

Verification is hand-rolled against the svix scheme for the same reason
``verify_meta_signature`` is: it is fifteen lines of HMAC and it keeps a signing
path free of a third-party package.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from app.core.breaker import guard
from app.core.errors import ProviderError, ProviderUnavailableError
from app.core.logging import get_logger
from app.domain.email_channel import (
    EmailAttachmentRef,
    EmailDeliveryStatus,
    EmailDeliveryUpdate,
)
from app.domain.email_text import mask_address
from app.domain.ports import OutboundEmail, ReceivedEmail

logger = get_logger(__name__)

RESEND_API_URL = "https://api.resend.com"
_PROVIDER = "resend"

# svix headers, on every Resend webhook.
SVIX_ID_HEADER = "svix-id"
SVIX_TIMESTAMP_HEADER = "svix-timestamp"
SVIX_SIGNATURE_HEADER = "svix-signature"

# Beyond this the message is a replay, not a delivery. svix's own recommendation.
DEFAULT_MAX_SIGNATURE_AGE_SECONDS = 300

# Resend event type to our delivery vocabulary. Anything absent is ignored rather
# than guessed: a status we do not understand must not overwrite one we do.
DELIVERY_EVENT_STATUS: dict[str, EmailDeliveryStatus] = {
    "email.sent": EmailDeliveryStatus.SENT,
    "email.delivered": EmailDeliveryStatus.DELIVERED,
    "email.delivery_delayed": EmailDeliveryStatus.QUEUED,
    "email.bounced": EmailDeliveryStatus.BOUNCED,
    "email.complained": EmailDeliveryStatus.COMPLAINED,
    "email.failed": EmailDeliveryStatus.FAILED,
}
INBOUND_EVENT_TYPES = frozenset({"email.received", "email.inbound"})


def verify_resend_signature(
    raw: bytes,
    svix_id: str,
    svix_timestamp: str,
    svix_signature: str,
    secret: str,
    *,
    max_age_seconds: int = DEFAULT_MAX_SIGNATURE_AGE_SECONDS,
) -> bool:
    """Verify an svix signature over ``{id}.{timestamp}.{body}`` in constant time.

    ``raw`` must be the exact bytes received. Re-serialising the parsed JSON
    changes key order and whitespace and the signature will never match — which is
    why the endpoint reads the body before it parses it.

    The timestamp window is checked as well as the MAC: a valid signature over a
    three-week-old body is a replay, and replaying an inbound email means
    answering it twice.
    """
    if not raw or not svix_id or not svix_timestamp or not svix_signature or not secret:
        return False

    try:
        sent_at = int(svix_timestamp.strip())
    except ValueError:
        return False
    if abs(int(time.time()) - sent_at) > max_age_seconds:
        return False

    key_material = secret.strip()
    if key_material.startswith("whsec_"):
        key_material = key_material[len("whsec_") :]
    try:
        key = base64.b64decode(key_material, validate=True)
    except ValueError:
        # A secret that is not base64 is used verbatim rather than rejected: svix
        # is the documented format, but a plain shared secret must not silently
        # verify everything. (binascii.Error subclasses ValueError.)
        key = key_material.encode("utf-8")
    if not key:
        return False

    signed = svix_id.encode("utf-8") + b"." + svix_timestamp.strip().encode("utf-8") + b"." + raw
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")

    for candidate in svix_signature.split():
        version, separator, value = candidate.partition(",")
        if separator != "," or version != "v1":
            continue
        if hmac.compare_digest(expected, value):
            return True
    return False


def _as_mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _as_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, list):
        return tuple(item for item in cast(list[object], value) if isinstance(item, str))
    return ()


def _flatten_headers(value: object) -> dict[str, str]:
    """Resend sends headers either as an object or as a list of name/value pairs."""
    headers: dict[str, str] = {}
    if isinstance(value, Mapping):
        for key, item in cast(Mapping[object, object], value).items():
            if isinstance(key, str):
                headers[key.lower()] = _as_str(item)
        return headers
    if isinstance(value, list):
        for entry in cast(list[object], value):
            pair = _as_mapping(entry)
            name = _as_str(pair.get("name"))
            if name:
                headers[name.lower()] = _as_str(pair.get("value"))
    return headers


def _parse_timestamp(value: object) -> datetime | None:
    raw = _as_str(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_attachments(value: object) -> tuple[EmailAttachmentRef, ...]:
    if not isinstance(value, list):
        return ()
    refs: list[EmailAttachmentRef] = []
    for entry in cast(list[object], value):
        item = _as_mapping(entry)
        filename = _as_str(item.get("filename")) or _as_str(item.get("name"))
        if not filename:
            continue
        size = item.get("size") or item.get("content_length") or 0
        refs.append(
            EmailAttachmentRef(
                attachment_id=_as_str(item.get("id")) or _as_str(item.get("attachment_id")),
                filename=filename,
                content_type=_as_str(item.get("content_type"), "application/octet-stream"),
                size_bytes=int(size) if isinstance(size, (int, float)) else 0,
            )
        )
    return tuple(refs)


def _received_for(headers: Mapping[str, str], fallback: tuple[str, ...]) -> tuple[str, ...]:
    """Addresses this message was actually delivered to.

    Under an MX-forwarding rule ``To:`` is still the tenant's own support address,
    so ``To:`` alone cannot identify the channel. The ``for <addr>`` clause of the
    Received trace is what names the mailbox that took delivery. Delivered-To is
    checked first because a forwarder that sets it is telling us directly.
    """
    found: list[str] = []
    delivered = headers.get("delivered-to") or headers.get("x-original-to") or ""
    for candidate in delivered.replace(",", " ").split():
        cleaned = candidate.strip("<>;")
        if "@" in cleaned:
            found.append(cleaned.lower())
    received = headers.get("received") or ""
    marker = " for "
    index = received.lower().find(marker)
    if index >= 0:
        tail = received[index + len(marker) :].strip()
        candidate = tail.split(";")[0].strip().strip("<>")
        if "@" in candidate and " " not in candidate:
            found.append(candidate.lower())
    for address in fallback:
        if address and address.lower() not in found:
            found.append(address.lower())
    return tuple(dict.fromkeys(found))


def parse_received_payload(data: Mapping[str, object]) -> ReceivedEmail:
    """Build a ReceivedEmail from an inbound webhook or a fetch response.

    Accepts either the whole webhook envelope (``{"type": ..., "data": {...}}``) or
    the inner object, because the fetch endpoint returns the inner one.
    """
    payload = _as_mapping(data.get("data")) or data
    headers = _flatten_headers(payload.get("headers"))
    from_value = payload.get("from")
    from_address = _as_str(from_value)
    from_name = ""
    if isinstance(from_value, Mapping):
        entry = cast(Mapping[str, object], from_value)
        from_address = _as_str(entry.get("email") or entry.get("address"))
        from_name = _as_str(entry.get("name"))
    if "<" in from_address and ">" in from_address:
        display = from_address[: from_address.rfind("<")].strip().strip('"')
        from_name = from_name or display

    to = _as_tuple(payload.get("to"))
    message_id = (
        _as_str(payload.get("message_id"))
        or headers.get("message-id", "")
        or f"<resend-{_as_str(payload.get('id'))}>"
    )
    references = _as_tuple(payload.get("references")) or tuple(
        item for item in headers.get("references", "").split() if item
    )
    spam = payload.get("spam_score")

    return ReceivedEmail(
        provider_id=_as_str(payload.get("email_id")) or _as_str(payload.get("id")),
        message_id=message_id,
        from_address=from_address,
        subject=_as_str(payload.get("subject")),
        in_reply_to=_as_str(payload.get("in_reply_to")) or headers.get("in-reply-to") or None,
        references=references,
        from_name=from_name,
        to=to,
        cc=_as_tuple(payload.get("cc")),
        received_for=_received_for(headers, to),
        text=_as_str(payload.get("text")) or None,
        html=_as_str(payload.get("html")) or None,
        headers=headers,
        attachments=_parse_attachments(payload.get("attachments")),
        received_at=_parse_timestamp(payload.get("created_at") or payload.get("received_at")),
        spam_score=float(spam) if isinstance(spam, (int, float)) else None,
    )


def parse_delivery_event(data: Mapping[str, object]) -> EmailDeliveryUpdate | None:
    """Turn a Resend status webhook into a delivery update, or None to ignore it.

    None for an unrecognised event type on purpose: ranking in the repository
    stops a stale status from winning, but only for statuses we can rank. An
    unknown event mapped to a guess would defeat that.
    """
    event_type = _as_str(data.get("type"))
    status = DELIVERY_EVENT_STATUS.get(event_type)
    if status is None:
        return None
    payload = _as_mapping(data.get("data")) or data
    provider_id = _as_str(payload.get("email_id")) or _as_str(payload.get("id"))
    if not provider_id:
        return None

    recipients = _as_tuple(payload.get("to"))
    bounce = _as_mapping(payload.get("bounce"))
    bounce_type = _as_str(bounce.get("type")) or _as_str(payload.get("bounce_type")) or None
    error = (
        _as_str(bounce.get("message"))
        or _as_str(payload.get("reason"))
        or _as_str(payload.get("error"))
        or None
    )
    return EmailDeliveryUpdate(
        provider_id=provider_id,
        status=status,
        recipient_masked=mask_address(recipients[0]) if recipients else None,
        bounce_type=bounce_type,
        error=error,
        provider_timestamp=_parse_timestamp(payload.get("created_at"))
        or datetime.now(tz=UTC),
    )


class ResendEmail:
    """EmailProvider over the Resend REST API. One breaker, no SDK."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = RESEND_API_URL,
        timeout: float = 20.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
                "accept": "application/json",
            },
        )

    @property
    def name(self) -> str:
        return _PROVIDER

    @staticmethod
    def _error_for_response(response: httpx.Response) -> ProviderError:
        detail = ""
        try:
            body = cast(object, response.json())
            if isinstance(body, Mapping):
                mapping = cast(Mapping[object, object], body)
                message = mapping.get("message") or mapping.get("error")
                if isinstance(message, str):
                    detail = f", {message[:160]}"
        except ValueError:
            pass
        text = f"Resend API returned HTTP {response.status_code}{detail}"
        # 429 and 5xx are the provider's problem and the breaker should count
        # them. A 422 is our malformed payload and retrying it forever is worse
        # than failing once.
        if response.status_code in (408, 429) or response.status_code >= 500:
            return ProviderUnavailableError(_PROVIDER, text)
        return ProviderError(_PROVIDER, text)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        with guard(_PROVIDER):
            try:
                response = await self._client.request(
                    method, path, json=payload, headers=extra_headers
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ProviderUnavailableError(_PROVIDER, "Resend API is unavailable") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(_PROVIDER, "Resend API request failed") from exc
            if response.status_code >= 400:
                raise self._error_for_response(response)
        if not response.content:
            return {}
        try:
            return cast(object, response.json())
        except ValueError as exc:
            raise ProviderError(_PROVIDER, "Resend API returned invalid JSON") from exc

    async def send(self, message: OutboundEmail) -> str:
        """Send one message and return the provider id.

        The idempotency key is passed through as a header, so a retry after a
        timeout cannot deliver the same reply twice — the one failure mode that is
        externally visible and cannot be taken back.
        """
        if not message.to:
            raise ValueError("to is required")
        if not message.from_address:
            raise ValueError("from_address is required")

        sender = (
            f"{message.from_name} <{message.from_address}>"
            if message.from_name
            else message.from_address
        )
        payload: dict[str, Any] = {
            "from": sender,
            "to": list(message.to),
            "subject": message.subject,
            "text": message.text,
        }
        if message.html:
            payload["html"] = message.html
        if message.cc:
            payload["cc"] = list(message.cc)
        if message.reply_to:
            payload["reply_to"] = list(message.reply_to)
        if message.headers:
            payload["headers"] = dict(message.headers)
        if message.tags:
            payload["tags"] = [
                {"name": key, "value": value} for key, value in message.tags.items()
            ]

        headers = (
            {"Idempotency-Key": message.idempotency_key} if message.idempotency_key else None
        )
        data = await self._request("POST", "/emails", payload=payload, extra_headers=headers)
        provider_id = _as_str(_as_mapping(data).get("id"))
        if not provider_id:
            raise ProviderError(_PROVIDER, "Resend response did not include an id")
        logger.info(
            "email_sent",
            provider=_PROVIDER,
            provider_id=provider_id,
            to=mask_address(message.to[0]),
        )
        return provider_id

    async def fetch_received(self, provider_id: str) -> ReceivedEmail:
        """Pull the parsed inbound message the webhook told us about.

        Received mail lives under its own namespace: `/emails/receiving/{id}`
        reads inbound, while `/emails/{id}` reads a message *this account sent*.
        The two ids are not interchangeable, so fetching a received email from
        the sent-mail path returns the wrong resource (or 404s).

        Still unexercised against a live account - we have no received email to
        read back yet. It is deliberately the only place that knows the URL
        shape, so correcting it is a one-line change and never touches the
        service.
        """
        if not provider_id:
            raise ValueError("provider_id is required")
        data = await self._request("GET", f"/emails/receiving/{provider_id}")
        return parse_received_payload(_as_mapping(data))

    async def fetch_attachment(self, provider_id: str, attachment_id: str) -> bytes:
        """Attachment bytes, base64 in the JSON body.

        Unverified against a live account, same as `fetch_received`, and the
        same receiving namespace applies. Returns empty bytes rather than
        raising when the payload carries no content: an attachment we cannot
        read is not a reason to drop the question.
        """
        if not provider_id or not attachment_id:
            raise ValueError("provider_id and attachment_id are required")
        data = await self._request(
            "GET", f"/emails/receiving/{provider_id}/attachments/{attachment_id}"
        )
        content = _as_str(_as_mapping(data).get("content"))
        if not content:
            return b""
        try:
            return base64.b64decode(content, validate=False)
        except ValueError as exc:
            raise ProviderError(_PROVIDER, "attachment content was not base64") from exc

    async def create_domain(self, name: str) -> dict[str, object]:
        """Register a sending domain and return the DNS records Resend wants.

        The whole point: the tenant connects a subdomain and gets the exact
        SPF/DKIM records to paste at their DNS host — verification is then a
        second call reading the status back, not a claim the card makes up.
        """
        if not name:
            raise ValueError("domain name is required")
        data = await self._request("POST", "/domains", payload={"name": name})
        if not isinstance(data, dict):
            raise ProviderError(_PROVIDER, "Resend returned an unexpected domain payload")
        return data

    async def get_domain(self, domain_id: str) -> dict[str, object]:
        """Read a domain's verification state back from Resend.

        The caller decides what `status`/`spf`/`dkim` mean for its own state
        machine; this returns the raw mapping so the provider's vocabulary is
        interpreted in exactly one place.
        """
        if not domain_id:
            raise ValueError("domain_id is required")
        data = await self._request("GET", f"/domains/{domain_id}")
        if not isinstance(data, dict):
            raise ProviderError(_PROVIDER, "Resend returned an unexpected domain payload")
        return data

    async def close(self) -> None:
        await self._client.aclose()
