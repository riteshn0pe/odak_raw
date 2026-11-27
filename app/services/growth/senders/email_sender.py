"""Outbound growth mail, over the tenant's own verified sending domain.

This module is the last thing that runs before a stranger is contacted, so it is
the place every compliance rule is *enforced* rather than assumed:

* **Both suppression lists** are checked — ``growth_suppressions`` (cross-channel,
  growth-owned) and ``email_suppressions`` (the email channel's own, which the
  support mailbox also honours). Either one blocks the send.
* **Every mail carries an unsubscribe link and the tenant's postal address.**
  Not optional, not a template the tenant can edit them out of: the footer is
  appended here, after the agent's text, so no agent can produce a mail without
  one.
* **The sending identity must be verified.** The `resend_domain` integration has
  to be `active` — meaning SPF/DKIM/DMARC checked out — before a single mail
  leaves. Sending cold mail from an unverified domain is how a tenant's whole
  domain gets blocked, and it is unrecoverable.
* **One-click unsubscribe headers** (RFC 8058) are set, so the recipient's mail
  client can offer the native button instead of a spam report. A spam report
  costs the tenant reputation; an unsubscribe costs them one lead.

A refused send raises. The dispatcher catches it per message, marks that one row
failed and continues the batch — so one bad address never costs a tenant the
other twenty-four.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.domain.growth import is_suppressed, normalise_email
from app.domain.ports import OutboundEmail, SendReceipt

logger = get_logger(__name__)

CHANNEL = "email"
USAGE_KIND = "email_sent"
FEATURE = "outbound"

# The integration whose `external_ref` is the verified sending subdomain.
DOMAIN_PROVIDER = "resend_domain"

# Cold mail is short by policy — under 120 words, per the plan — but a follow-up
# quoting a thread can legitimately be longer. This is the transport ceiling, not
# the style rule; the style rule lives in the compose prompt.
MAX_BODY_CHARS = 8000

# Loop suppression. The same header set the support mailbox uses, for the same
# reason: our automation must not end up in a conversation with somebody else's.
_LOOP_HEADERS: dict[str, str] = {
    "Auto-Submitted": "auto-generated",
    "X-Auto-Response-Suppress": "All",
}


class GrowthEmailSender:
    """The ``ChannelSender`` port for email."""

    def __init__(self, container: Any) -> None:
        self._c = container

    @property
    def channel(self) -> str:
        return CHANNEL

    async def send(self, tenant_id: UUID, message: Any) -> SendReceipt:
        """Send one queued ``growth_messages`` row.

        Every refusal raises with a message a human can read, because these all
        end up in the dispatcher's log line and in the panel's failure reason,
        and "send failed" helps nobody diagnose an unverified DKIM record.
        """
        provider = getattr(self._c, "email", None)
        if provider is None:
            raise ValidationError("no email provider is configured on this deployment")

        lead = await self._lead(tenant_id, message)
        address = normalise_email(str(lead.get("email") or ""))
        if not address:
            raise ValidationError("this lead has no usable email address")

        await self._assert_contactable(tenant_id, address)
        sender_address, sender_name = await self._identity(tenant_id)

        settings = await self._c.growth_profile.get(tenant_id)
        postal = str((settings.get("profile") or {}).get("postal_address") or "").strip()
        if not postal:
            # refused, not silently sent without one. A mail with no physical
            # address is a CAN-SPAM violation the tenant would be liable for, and
            # the fix is one field on a screen they already have open
            raise ValidationError(
                "add your business postal address on the growth profile before "
                "sending outbound mail"
            )

        body = str(message.get("content") or "").strip()
        if not body:
            raise ValidationError("this message has no content")

        unsubscribe_url = self._unsubscribe_url(tenant_id, address)
        text = _with_footer(body[:MAX_BODY_CHARS], postal, unsubscribe_url, sender_name)

        outbound = OutboundEmail(
            to=(address,),
            subject=_subject(message, lead, sender_name),
            text=text,
            from_address=sender_address,
            from_name=sender_name,
            headers={
                **_LOOP_HEADERS,
                # RFC 2369 + RFC 8058. The mail client's own unsubscribe button
                # is the cheapest possible outcome of a mail somebody does not
                # want — the alternative they reach for is the spam button
                "List-Unsubscribe": f"<{unsubscribe_url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
            # the message id is the idempotency key, so a provider timeout that
            # is retried cannot deliver the same cold mail twice
            idempotency_key=f"growth:{message['id']}",
            tags={"feature": FEATURE, "channel": CHANNEL},
        )

        provider_id = await provider.send(outbound)

        from app.services.growth.metering import meter

        await meter(
            tenant_id,
            USAGE_KIND,
            # campaign sends attribute to the campaign feature; everything else
            # stays outbound. The row knows: a campaign send always carries its
            # campaign_id.
            feature="campaign" if message.get("campaign_id") else FEATURE,
            quantity=1,
            channel=CHANNEL,
            lead_id=str(lead.get("id") or ""),
        )
        if message.get("lead_id") is not None:
            await self._c.growth.leads.mark_contacted(tenant_id, UUID(str(lead["id"])))
        return SendReceipt(provider_id=str(provider_id), usage_kind=USAGE_KIND)

    # ---------------------------------------------------------------- private

    async def _lead(self, tenant_id: UUID, message: Any) -> dict[str, Any]:
        """The recipient behind one queued row: a lead, or a store customer.

        Campaign rows carry ``customer_id`` (a D2C buyer must never enter the
        B2B lead ladder); everything else carries ``lead_id``. Exactly one is
        set — a row with neither is a bug in the queuer, refused loudly.
        """
        lead_id = message.get("lead_id")
        if lead_id is not None:
            lead: dict[str, Any] | None = await self._c.growth.leads.get(
                tenant_id, UUID(str(lead_id))
            )
            if lead is None:
                raise ValidationError("the lead behind this message no longer exists")
            return lead
        customer_id = message.get("customer_id")
        if customer_id is not None:
            customer: dict[str, Any] | None = await self._c.growth.customers.get(
                tenant_id, UUID(str(customer_id))
            )
            if customer is None:
                raise ValidationError("the customer behind this message no longer exists")
            return customer
        raise ValidationError("this message is not attached to a lead")

    async def _assert_contactable(self, tenant_id: UUID, address: str) -> None:
        """Both lists, and either one is a veto.

        Checked here rather than only at compose time because a message can sit
        queued for hours between the two, and the opt-out that arrived in
        between is exactly the one that matters.
        """
        rows = await self._c.growth.consent.suppressions(tenant_id, [address])
        if is_suppressed(rows, CHANNEL, address):
            raise ValidationError("this address has opted out")

        repo = getattr(self._c, "email_repo", None)
        if repo is not None:
            from app.core.security import hash_identifier

            digest = hash_identifier(
                address, f"{self._c.settings.SECRET_KEY}:{tenant_id}"
            )
            if await repo.is_suppressed(tenant_id, digest):
                raise ValidationError("this address is on the email suppression list")

    async def _identity(self, tenant_id: UUID) -> tuple[str, str]:
        """The verified From address, or a refusal.

        The domain comes off the integration row rather than the profile because
        it carries a verification *status*, and a profile field cannot. `active`
        here means a human completed SPF/DKIM/DMARC — the whole point of the
        integration state machine.
        """
        row = await self._c.growth.integrations.get_by_provider(
            tenant_id, DOMAIN_PROVIDER
        )
        if row is None or row.get("status") != "active":
            raise ValidationError(
                "connect and verify your sending domain before sending outbound mail"
            )
        domain = str(row.get("external_ref") or "").strip().lower()
        if not domain:
            raise ValidationError("the sending domain integration has no domain set")

        settings = await self._c.growth_profile.get(tenant_id)
        profile = settings.get("profile") or {}
        local = str(profile.get("sender_local") or "hello").strip() or "hello"
        name = str(profile.get("sender_name") or "").strip()
        return f"{local}@{domain}", name

    def _unsubscribe_url(self, tenant_id: UUID, address: str) -> str:
        from app.services.growth.optout import unsubscribe_url

        return unsubscribe_url(self._c.settings, tenant_id, address)


def _subject(message: Any, lead: dict[str, Any], sender_name: str) -> str:
    """The subject line, from the message row or a plain fallback.

    The compose agent writes the subject into ``template_id`` — that column is
    otherwise unused on email, and a second content column on
    ``growth_messages`` would be a schema change to carry one short string that
    only email has.
    """
    subject = str(message.get("template_id") or "").strip()
    if subject:
        return subject[:200]
    company = str(lead.get("company") or "").strip()
    who = sender_name or "us"
    return f"Quick question about {company}" if company else f"A quick note from {who}"


def _with_footer(body: str, postal: str, unsubscribe_url: str, sender_name: str) -> str:
    """Append the legally required footer, always, after the agent's text.

    Appended here and nowhere else. If the footer were part of the prompt, an
    agent could drop it, reword it, or put a plausible-looking fake address in
    it — and the failure would be invisible until a regulator or a mailbox
    provider noticed.
    """
    parts = [body.rstrip(), "", "--"]
    if sender_name:
        parts.append(sender_name)
    parts.append(postal)
    parts.append("")
    parts.append(f"Don't want these emails? Unsubscribe: {unsubscribe_url}")
    return "\n".join(parts)
