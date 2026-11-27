"""Speed-to-lead: an inbound enquiry, answered in minutes.

Somebody filled in a form. The single strongest predictor of whether that
becomes a customer is how long they wait for a reply, so this job does three
things in one pass and does the cheapest one first:

1. **Store the raw payload, then parse it.** ``growth_lead_raw`` is written
   before anything interprets the body, so a form the tenant redesigned last
   night produces a visible parse failure with the payload attached rather than
   a stack trace about a missing key.
2. **Qualify.** One utility-tier pass over what they wrote, pulling out quantity,
   location and budget signals into the lead's summary. Flagged as
   *inbound-qualified*, never as *researched* — the difference matters, because
   research is evidence we gathered and this is a stranger's own claim.
3. **Acknowledge, and ping the owner.** The acknowledgement is a template, not
   generated text: it has to be instant, it has to be free, and there is nothing
   an LLM can add to "we got your enquiry, here is what happens next" except
   latency and a chance to hallucinate a delivery date.

After that the lead enters exactly the same cadence as an outbound one — same
reply agent, same follow-ups — because from here on there is no difference.

**Channel choice is consent, not preference.** WhatsApp only when a
``growth_consents`` opt-in exists (the form's own tick box is what records it);
otherwise email. A form submission is not WhatsApp consent, and Meta agrees.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.domain.growth import (
    dedupe_lead,
    is_suppressed,
    lead_uid,
    normalise_domain,
    normalise_email,
    normalise_phone,
    resolve_language,
)
from app.services.agents.render import MissingFacts

logger = get_logger(__name__)

AGENT_KEY = "growth_inbound"
INBOUND_JOB = "growth_inbound_lead"
FEATURE = "inbound"

# Generous ceiling: Gemini 3 thinking (HIGH) shares the budget.
QUALIFY_MAX_TOKENS = 20000

# Field names people actually put on contact forms, in the order we trust them.
# A form is somebody else's HTML and we do not get to dictate its names, so the
# parser tries the common shapes rather than demanding one.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "full_name", "fullname", "your_name", "contact_name", "person"),
    "email": ("email", "email_address", "e_mail", "mail", "your_email"),
    "phone": ("phone", "mobile", "phone_number", "mobile_number", "contact", "whatsapp"),
    "company": ("company", "company_name", "organisation", "organization", "business"),
    "message": ("message", "enquiry", "inquiry", "requirement", "details", "comments", "query"),
    "website": ("website", "site", "url", "company_website"),
}

_QUALIFY_SYSTEM = (
    "You read one inbound sales enquiry and summarise only what it actually "
    "says, in at most three short lines. You never infer a quantity, a "
    "budget, a location or an urgency that is not stated, and you never "
    "quote a price, a lead time or a stock position — if the enquiry asks "
    "for one, the summary records that they asked. Missing information is "
    "written as 'not stated'."
)

_QUALIFY_PROMPT = """We sell: {sells}

THE ENQUIRY:
{message}

In at most three short lines, state:
- what they want
- quantity, location and budget, each only if stated
- how urgent it sounds, only if they said so

Write 'not stated' for anything the enquiry does not say.
"""

# The acknowledgement. Deterministic, instant, free, and it promises only the one
# thing we can actually guarantee: that a human will read it.
_ACK_TEMPLATES: dict[str, str] = {
    "en": (
        "Thanks for getting in touch{name}. We've got your enquiry and someone "
        "from {company} will come back to you shortly.\n\n"
        "If it's urgent, just reply to this message."
    ),
    "hi": (
        "संपर्क करने के लिए धन्यवाद{name}। आपकी पूछताछ हमें मिल गई है और "
        "{company} की ओर से कोई जल्द ही आपसे संपर्क करेगा।\n\n"
        "अगर ज़रूरी है, तो इसी संदेश का उत्तर दें।"
    ),
    "hinglish": (
        "Aapke message ke liye dhanyavaad{name}. Humein aapki enquiry mil gayi hai, "
        "{company} se koi jaldi hi aapse contact karega.\n\n"
        "Urgent ho to isi message ka reply kar dijiye."
    ),
}

# The WhatsApp template name a tenant must have approved for acknowledgements.
# Named as a constant because the Integrations screen tells the tenant to create
# exactly this one, and a typo between the two would be a silent failure.
ACK_TEMPLATE_NAME = "lead_acknowledgement"


class InboundService:
    """One inbound enquiry, from raw payload to acknowledged lead."""

    def __init__(self, container: Any) -> None:
        self._c = container

    async def _system_prompt(self, tenant_id: UUID) -> str:
        """The persona, rendered from this tenant's facts at invocation.

        Falls back to the module constant whenever the agent layer is absent or
        the tenant's template wants a fact the profile does not carry — the card
        names the missing fact; the run must not break on it.
        """
        prompts = getattr(self._c, "agent_prompts", None)
        if prompts is None:
            return _QUALIFY_SYSTEM
        try:
            return await prompts.system_for(tenant_id, AGENT_KEY)
        except MissingFacts:
            logger.warning(
                "agent_prompt_missing_facts",
                agent_key=AGENT_KEY,
                tenant_id=str(tenant_id),
            )
            return _QUALIFY_SYSTEM

    async def handle(self, tenant_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """The ``growth_inbound_lead`` job."""
        raw_id = payload.get("raw_id")
        source = str(payload.get("source") or "website_form")
        if raw_id is None:
            return {"status": "skipped", "reason": "no archived payload"}

        raw = await self._c.growth.raw.get(tenant_id, UUID(str(raw_id)))
        if raw is None:
            logger.warning(
                "growth_inbound_raw_missing", tenant_id=str(tenant_id), raw_id=str(raw_id)
            )
            return {"status": "skipped", "reason": "archived payload not found"}
        if raw.get("lead_id") is not None:
            # already processed: a queue redelivery, not a second enquiry
            return {"status": "duplicate", "lead_id": str(raw["lead_id"])}

        fields = parse_form(dict(raw.get("payload") or {}))
        return await self.ingest(
            tenant_id, source=source, fields=fields, raw_id=UUID(str(raw_id))
        )

    async def ingest(
        self,
        tenant_id: UUID,
        source: str,
        fields: dict[str, str],
        raw_id: UUID,
    ) -> dict[str, Any]:
        """One parsed enquiry, from fields to acknowledged lead.

        The shared tail of every inbound source. A website form, a JustDial
        mail and an IndiaMART enquiry differ only in how their fields were
        extracted; from here on they are the same lead — same validation, same
        dedupe by ``lead_uid``, same qualification, same owner ping, same
        acknowledgement. Callers that grew their own pipeline past this point
        are how per-source drift starts, and per-source drift is how one
        source quietly stops pinging the owner.
        """
        problem = _validate(fields)
        if problem:
            await self._c.growth.raw.resolve(tenant_id, raw_id, parse_error=problem)
            logger.warning(
                "growth_inbound_unparseable",
                tenant_id=str(tenant_id),
                source=source,
                reason=problem,
            )
            return {"status": "failed", "reason": problem}

        lead = await self._upsert(tenant_id, fields, source)
        await self._c.growth.raw.resolve(tenant_id, raw_id, lead_id=UUID(str(lead["id"])))

        summary = await self._qualify(tenant_id, fields)
        if summary:
            merged = dict(lead)
            merged["research_summary"] = summary
            merged["research_links"] = [{"kind": "inbound_form", "source": source}]
            lead = await self._c.growth.leads.upsert(tenant_id, merged)
            # CRM push is queued, never blocking this path (on_lead_changed swallows all errors)
            await self._c.growth_crm.on_lead_changed(tenant_id, UUID(str(lead["id"])))

        await self._ping_owner(tenant_id, lead, fields, summary)
        ack = await self._acknowledge(tenant_id, lead, fields)

        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="inbound_lead",
            input={"source": source, "raw_id": str(raw_id)},
            output={"lead_id": str(lead["id"]), "acknowledged": ack},
            reason="inbound enquiry qualified and acknowledged",
        )
        return {
            "status": "ok",
            "lead_id": str(lead["id"]),
            "qualified": bool(summary),
            "acknowledged": ack,
        }

    # ---------------------------------------------------------------- private

    async def _upsert(
        self, tenant_id: UUID, fields: dict[str, str], source: str
    ) -> dict[str, Any]:
        """Write the lead, merging into an existing row when we know them.

        The same ``lead_uid`` and ``dedupe_lead`` the research agent uses. A
        prospect we cold-mailed last month who now fills in the form is one lead
        with a thread, not a stranger — and getting that wrong is how somebody
        receives a cold mail and an acknowledgement on the same afternoon.
        """
        email = normalise_email(fields.get("email", ""))
        phone = normalise_phone(fields.get("phone", ""))
        domain = normalise_domain(fields.get("website", "")) or (
            email.rsplit("@", 1)[-1] if email else ""
        )
        uid = lead_uid(str(tenant_id), phone=phone, email=email, domain=domain)

        incoming: dict[str, Any] = {
            "uid": uid,
            "company": fields.get("company", ""),
            "person": fields.get("name", ""),
            "email": email,
            "phone": phone,
            "domain": domain,
            "source": source,
            "status": "new",
        }
        existing = await self._c.growth.leads.get_by_uid(tenant_id, uid)
        merged = dedupe_lead(existing, incoming) if existing else incoming
        merged["uid"] = uid
        row: dict[str, Any] = await self._c.growth.leads.upsert(tenant_id, merged)
        # a brand-new lead or merged-in content: the CRM mirror wants either way
        # (on_lead_changed swallows all errors, so inbound never fails on it)
        await self._c.growth_crm.on_lead_changed(tenant_id, UUID(str(row["id"])))
        return row

    async def _qualify(self, tenant_id: UUID, fields: dict[str, str]) -> str:
        """Three lines of what they actually asked for. Never inferred."""
        message = fields.get("message", "").strip()
        if not message:
            return ""

        from app.adapters.llm.base import Message

        settings = await self._c.growth_profile.get(tenant_id)
        sells = str((settings.get("profile") or {}).get("sells") or "our product")

        llm = await self._c.agent_models.llm_for(tenant_id, AGENT_KEY, "utility")
        system = await self._system_prompt(tenant_id)
        try:
            completion = await llm.complete(
                [
                    Message(role="system", content=system),
                    Message(
                        role="user",
                        content=_QUALIFY_PROMPT.format(sells=sells, message=message[:3000]),
                    ),
                ],
                max_tokens=QUALIFY_MAX_TOKENS,
            )
        except Exception:
            # the acknowledgement is the part with a clock on it. A qualification
            # outage costs the owner a summary, not the lead
            logger.warning(
                "growth_inbound_qualify_failed", tenant_id=str(tenant_id), exc_info=True
            )
            return ""

        from app.services.growth.metering import meter_llm

        await meter_llm(
            tenant_id,
            completion.usage,
            feature=FEATURE,
            agent_key=AGENT_KEY,
            model=getattr(completion, "model", ""),
        )
        return (completion.text or "").strip()[:2000]

    async def _ping_owner(
        self,
        tenant_id: UUID,
        lead: dict[str, Any],
        fields: dict[str, str],
        summary: str,
    ) -> None:
        """The hot-lead alert, in the owner's own language, immediately."""
        who = fields.get("name") or fields.get("company") or "Someone"
        contact = lead.get("email") or lead.get("phone") or "no contact given"
        items = [line for line in (summary or fields.get("message", ""))[:600].splitlines() if line]
        try:
            await self._c.approvals.alert(
                tenant_id,
                f"New enquiry from {who} ({contact}).",
                items[:5],
            )
        except Exception:
            logger.warning(
                "growth_inbound_alert_failed", tenant_id=str(tenant_id), exc_info=True
            )

    async def _acknowledge(
        self, tenant_id: UUID, lead: dict[str, Any], fields: dict[str, str]
    ) -> str:
        """Queue the acknowledgement on the channel consent allows.

        Queued rather than sent inline, so it goes through the same sender, the
        same suppression gate and the same metering as everything else — a second
        send path would be a second place for the compliance checks to be
        forgotten. The dispatcher runs every five minutes, which is inside the
        "minutes" target.
        """
        settings = await self._c.growth_profile.get(tenant_id)
        allowed = list((settings.get("languages") or {}).get("allowed") or ["en"])
        language = resolve_language(
            fields.get("message"), None, fields.get("phone"), allowed
        )
        company = str((settings.get("profile") or {}).get("sender_name") or "").strip()

        phone = normalise_phone(str(lead.get("phone") or ""))
        email = normalise_email(str(lead.get("email") or ""))
        suppressions = await self._c.growth.consent.suppressions(
            tenant_id, [value for value in (phone, email) if value]
        )

        channel = ""
        # a form submission is not WhatsApp consent. Only an explicit opt-in —
        # the form's own tick box, recorded by the endpoint — unlocks it
        if (
            phone
            and not is_suppressed(suppressions, "whatsapp", phone)
            and await self._c.growth.consent.has_consent(tenant_id, "whatsapp", phone)
        ):
            channel = "whatsapp"
        if not channel and email and not is_suppressed(suppressions, "email", email):
            channel = "email"
        if not channel:
            logger.info(
                "growth_inbound_no_ack_channel",
                tenant_id=str(tenant_id),
                lead_id=str(lead["id"]),
            )
            return ""

        body = _ack_text(language, fields.get("name", ""), company)
        await self._c.growth.messages.add(
            tenant_id,
            lead_id=UUID(str(lead["id"])),
            channel=channel,
            direction="out",
            content=body,
            language=language,
            template_id=(
                ACK_TEMPLATE_NAME if channel == "whatsapp" else "We got your enquiry"
            ),
            status="queued",
        )
        return channel


# ------------------------------------------------------------------- helpers


def parse_form(payload: dict[str, Any]) -> dict[str, str]:
    """Pull the fields we need out of somebody else's form post.

    Pure and exported so it can be tested exhaustively against real form shapes
    without a database. Nested payloads are flattened one level, because most
    form builders post ``{"data": {...}}`` or ``{"fields": {...}}`` and demanding
    a flat body would fail on the majority of real installations.
    """
    flat: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, dict) and str(key).lower() in ("data", "fields", "form", "answers"):
            for inner_key, inner_value in value.items():
                flat.setdefault(_slug(inner_key), inner_value)
        else:
            flat.setdefault(_slug(key), value)

    out: dict[str, str] = {}
    for name, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            if alias in flat and flat[alias] not in (None, ""):
                out[name] = str(flat[alias]).strip()[:2000]
                break
    return out


def _slug(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_").replace(" ", "_")


def _validate(fields: dict[str, str]) -> str:
    """Why this submission cannot become a lead, or "".

    One reachable identifier is the whole requirement. A form with a name and a
    message and no way to answer is not a lead, it is a note — and storing it as
    a lead would put a row on the board that nobody can ever action.
    """
    if not normalise_email(fields.get("email", "")) and not normalise_phone(
        fields.get("phone", "")
    ):
        return "the submission carries no usable email address or phone number"
    return ""


def _ack_text(language: str, name: str, company: str) -> str:
    """The acknowledgement, in the language they wrote in.

    English is the floor, and an unknown language falls back to it rather than
    to nothing: a reply in the wrong language is a small discourtesy, and no
    reply at all is the failure this whole module exists to prevent.
    """
    template = _ACK_TEMPLATES.get(language) or _ACK_TEMPLATES["en"]
    first = name.strip().split(" ")[0] if name.strip() else ""
    return template.format(
        name=f", {first}" if first else "",
        company=company or "our team",
    )
