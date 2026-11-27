"""Outbound growth messages on WhatsApp, inside Meta's rules.

WhatsApp is not email with a different transport, and the difference is the
whole module:

* **Consent is required before the first message, not after.** Email tolerates
  cold outreach with an unsubscribe link; WhatsApp does not, and Meta enforces
  it with the quality rating that decides whether a tenant's number keeps
  working. So this sender refuses to send to anyone without a
  ``growth_consents`` opt-in — no exception, no setting.
* **The 24-hour service window decides the message *shape*.** Inside it, free
  text. Outside it, an approved template or nothing at all. That is Meta's rule
  and the existing ``ServiceWindow`` already computes it, so it is reused rather
  than re-derived.
* **Conversations are priced, not messages.** The usage event is
  ``whatsapp_conversation_utility`` or ``..._marketing`` depending on the
  template's category, and the rate rides on the event because Meta prices by
  the recipient's country.

A refused send raises, and the dispatcher marks that one row failed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.domain.growth import is_suppressed, normalise_phone
from app.domain.ports import SendReceipt

logger = get_logger(__name__)

CHANNEL = "whatsapp"
FEATURE = "outbound"

# Meta's two conversation categories that a growth message can open. The kind
# decides the ledger line; the category comes off the template row rather than
# being guessed, because guessing marketing as utility understates the cost.
KIND_UTILITY = "whatsapp_conversation_utility"
KIND_MARKETING = "whatsapp_conversation_marketing"

# Meta's per-conversation list prices, in micros — millionths of a major
# currency unit — keyed by the recipient's country and then the conversation
# category. This is the table ``metering.py`` defers to when it says "the price
# rides on the event": one platform-wide number would be wrong for every tenant
# whose customers are not where the last tenant's customers were.
#
# The numbers are Meta's published list rates in INR, and they change — Meta
# revises the price list roughly yearly and has already moved from conversation
# to per-message pricing for some categories. They are a floor for the cost
# line, not a contract: keep them current, and treat a tenant on a negotiated
# Meta rate the way the cost screen treats a negotiated plan.
#
# ``service`` is the free-form leg. Meta stopped charging for service
# conversations in November 2024, so it is priced at zero rather than metered as
# utility — charging for it would overstate the campaign's cost.
WHATSAPP_RATE_MICROS: dict[str, dict[str, int]] = {
    "IN": {"marketing": 784600, "utility": 114600, "service": 0},
    # Everyone else, at the highest of Meta's regional bands converted to INR.
    # Erring high is the safe direction for a cost line: an overstated cost
    # makes a campaign look worse than it was, an understated one bills the
    # platform for the difference.
    "": {"marketing": 8000000, "utility": 2500000, "service": 0},
}

# Phone country codes we price separately, longest prefix first. Everything
# else falls to the default band above rather than being guessed at.
_COUNTRY_PREFIXES: tuple[tuple[str, str], ...] = (("91", "IN"),)


class GrowthWhatsAppSender:
    """The ``ChannelSender`` port for WhatsApp."""

    def __init__(self, container: Any) -> None:
        self._c = container

    @property
    def channel(self) -> str:
        return CHANNEL

    async def send(self, tenant_id: UUID, message: Any) -> SendReceipt:
        lead = await self._lead(tenant_id, message)
        phone = normalise_phone(str(lead.get("phone") or ""))
        if not phone:
            raise ValidationError("this lead has no usable phone number")

        await self._assert_contactable(tenant_id, phone)

        integration = await self._c.whatsapp_repo.active_for_tenant(tenant_id)
        if integration is None:
            raise ValidationError("connect WhatsApp before sending on this channel")
        token = await self._token(tenant_id, integration)

        from app.adapters.channels.whatsapp import WhatsAppClient
        from app.core.security import hash_identifier

        sender_ref = hash_identifier(phone, f"{self._c.settings.SECRET_KEY}:{tenant_id}")
        window = await self._c.service_window.evaluate(
            tenant_id,
            integration.id,
            sender_ref,
            integration.service_window_hours,
            integration.template_config,
        )

        client = WhatsAppClient(
            token,
            integration.phone_number_id,
            version=self._c.settings.WHATSAPP_GRAPH_VERSION,
            timeout=self._c.settings.HTTP_TIMEOUT_SECONDS,
            max_message_length=self._c.settings.WHATSAPP_MAX_MESSAGE_LENGTH,
        )
        try:
            provider_id, kind, template_id = await self._deliver(
                tenant_id, client, phone, message, lead, window
            )
        finally:
            await client.close()

        from app.services.growth.metering import meter

        # what this conversation costs, from the country/category table. The
        # same number goes on the ledger row and on the receipt, so the usage
        # ledger and the campaign's cost line can never disagree about one send.
        cost_micros = conversation_cost_micros(phone, kind, has_template=template_id is not None)
        await meter(
            tenant_id,
            kind,
            # campaign sends attribute to the campaign feature; the price still
            # rides on the event (Meta prices by country and category).
            feature="campaign" if message.get("campaign_id") else FEATURE,
            quantity=1,
            unit_cost_micros=cost_micros,
            channel=CHANNEL,
            lead_id=str(lead.get("id") or ""),
            template=template_id or "",
        )
        if message.get("lead_id") is not None:
            await self._c.growth.leads.mark_contacted(tenant_id, UUID(str(lead["id"])))
        return SendReceipt(
            provider_id=provider_id,
            cost_micros=cost_micros,
            usage_kind=kind,
            template_id=template_id,
        )

    # ---------------------------------------------------------------- private

    async def _deliver(
        self,
        tenant_id: UUID,
        client: Any,
        phone: str,
        message: Any,
        lead: dict[str, Any],
        window: Any,
    ) -> tuple[str, str, str | None]:
        """Free text inside the window, an approved template outside it."""
        if window.can_send_free_form:
            body = str(message.get("content") or "").strip()
            if not body:
                raise ValidationError("this message has no content")
            ids = await client.send_text(phone, body)
            if not ids:
                raise ValidationError("WhatsApp accepted no part of this message")
            # a free-form reply lands inside a conversation the customer opened,
            # which Meta prices as a service conversation — the utility line
            return str(ids[0]), KIND_UTILITY, None

        name = str(message.get("template_id") or "").strip()
        if not name:
            raise ValidationError(
                "the 24-hour window is closed and this message names no template"
            )
        language = str(message.get("language") or lead.get("language") or "en")
        template = await self._c.growth.wa_templates.approved(tenant_id, name, language)
        if template is None:
            raise ValidationError(
                f"template {name!r} is not approved for this tenant in {language!r}"
            )

        provider_id = await client.send_template(
            phone, str(template["name"]), _meta_language(str(template["language"]))
        )
        kind = (
            KIND_MARKETING
            if str(template.get("category")) == "marketing"
            else KIND_UTILITY
        )
        return str(provider_id), kind, str(template["name"])

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

    async def _assert_contactable(self, tenant_id: UUID, phone: str) -> None:
        """Suppression, then consent. Both, and consent is the strict one.

        On email an absent opt-in is normal — cold mail is lawful with an
        unsubscribe link and a postal address. On WhatsApp it is not, and the
        penalty falls on the tenant's number rather than on us, so the gate is
        hard rather than configurable.
        """
        rows = await self._c.growth.consent.suppressions(tenant_id, [phone])
        if is_suppressed(rows, CHANNEL, phone):
            raise ValidationError("this number has opted out")
        if not await self._c.growth.consent.has_consent(tenant_id, CHANNEL, phone):
            raise ValidationError(
                "no WhatsApp opt-in is recorded for this number"
            )

    async def _token(self, tenant_id: UUID, integration: Any) -> str:
        record = await self._c.whatsapp_repo.get_access_token_record(
            tenant_id, integration.id
        )
        if record is None or record.provider != "whatsapp":
            raise ValidationError("the WhatsApp access token is unavailable")
        from app.core.security import decrypt_secret

        return decrypt_secret(record.ciphertext, self._c.settings.ENCRYPTION_KEY)


def conversation_cost_micros(phone: str, kind: str, *, has_template: bool) -> int:
    """What Meta charges for the conversation this send opened.

    The category comes off the usage kind, which itself came off the template
    row, so the price follows the same decision the ledger line does. A
    free-form send carries no template and lands inside a window the customer
    opened — a service conversation, which Meta no longer charges for.
    """
    if kind == KIND_MARKETING:
        category = "marketing"
    elif has_template:
        category = "utility"
    else:
        category = "service"
    band = WHATSAPP_RATE_MICROS.get(_country_for(phone)) or WHATSAPP_RATE_MICROS[""]
    return int(band.get(category, 0))


def _country_for(phone: str) -> str:
    """The recipient's country from an E.164 number, or "" for the default band."""
    digits = phone.lstrip("+")
    for prefix, country in _COUNTRY_PREFIXES:
        if digits.startswith(prefix):
            return country
    return ""


def _meta_language(code: str) -> str:
    """Our language code, in Meta's dialect.

    The mapping itself lives on the WhatsApp adapter, because the template
    *management* path needs the identical dialect and two copies would drift.
    This stays as the sender's name for it.
    """
    from app.adapters.channels.whatsapp import meta_language

    return meta_language(code)
