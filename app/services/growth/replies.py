"""The reply agent: what to do when a lead writes back.

Every inbound message on a growth thread lands here, from either channel, and
the first question is always the same one — *what kind of reply is this* — because
the nine answers want nine different things done, and eight of them are not "draft
a reply":

    interested      → draft a reply, and ping the owner immediately. This is the
                      whole point of the system; a hot lead that waits for the
                      owner to check a dashboard is a lost lead.
    question        → draft a reply.
    not_interested  → mark lost, stop the cadence. No "just checking".
    unsubscribe     → suppress on every channel, cancel everything queued.
    out_of_office   → pause until the date they gave. Not a rejection, and it
                      must not read as one on the board.
    wrong_person    → ask once for the right contact, then stop.
    auto_reply      → record it and do nothing. An auto-responder is not a reply
                      and must not consume a follow-up or reset the clock.
    bounce          → suppress the address; it does not exist.
    other           → record it and hand it to the owner. An unclassifiable reply
                      is a human writing something we did not anticipate, which
                      is exactly when a person should read it.

The classification is one utility-tier call. Drafts are *drafts*: they go into
``growth_messages`` as queued outbound and are gated by the tenant's approval
level exactly like a cold mail, so level 0 sends them on the next tick and level
2 puts each in front of the owner.

**This module never routes a support conversation.** ``maybe_handle_*`` returns
``None`` for anyone who is not a lead with an open outbound thread, and the
existing channel service carries on to the RAG runner untouched. That is the
contract that keeps the growth branch from swallowing the support mailbox.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.domain.growth import (
    detect_language,
    normalise_email,
    normalise_phone,
    resolve_language,
)
from app.services.agents.render import MissingFacts

logger = get_logger(__name__)

AGENT_KEY = "growth_replies"
FEATURE = "inbound"

# The nine classes, and the lead status each one moves the lead to. `None` means
# the status is decided by the handler rather than by the class alone.
CLASSES: dict[str, str | None] = {
    "interested": "positive",
    "question": "replied",
    "not_interested": "lost",
    "unsubscribe": "suppressed",
    "out_of_office": None,
    "wrong_person": "replied",
    "auto_reply": None,
    "bounce": "suppressed",
    "other": "replied",
}

# Classes that get a written reply. Everything else is a state change.
REPLYING = frozenset({"interested", "question", "wrong_person"})

# Classes that stop the follow-up cadence for good.
TERMINAL = frozenset({"not_interested", "unsubscribe", "bounce"})

# Lead statuses this agent refuses to route, whatever the repository returned.
#
# A `won` lead is a *customer*. Their next email is a support question and
# belongs to the RAG agent — intercepting it would mean the sale permanently
# broke support for the person who made it, with no way back.
#
# The repository already excludes these in SQL, where the index can use the
# predicate. This is the second lock on the same door, for the reason the
# `growth_repo` docstring gives about the WHERE clause beside the RLS policy: a
# filter that is ever changed or mis-deployed should fail closed here too.
NOT_ROUTABLE: frozenset[str] = frozenset({"won"})

# Classifier answers one word, drafter ~200 words. Generous ceilings:
# Gemini 3 thinking (HIGH) shares the budget, provider floors at 20000.
CLASSIFY_MAX_TOKENS = 20000
DRAFT_MAX_TOKENS = 20000
MAX_INBOUND_CHARS = 4000

# How long an out-of-office pauses the cadence when the message names no return
# date. A week is the median corporate absence and, being wrong in the safe
# direction, costs a few days rather than a complaint.
DEFAULT_OOO_DAYS = 7
MAX_OOO_DAYS = 60

# STOP keywords, checked before the model. Cheaper, instant, and — more to the
# point — a deterministic opt-out cannot be lost to a classification error, which
# is the one mistake in this module with a regulator attached.
_STOP_WORDS: frozenset[str] = frozenset(
    {"stop", "unsubscribe", "optout", "opt-out", "remove", "band", "bandh", "hatao"}
)

# Dates in an out-of-office, in the shapes people actually write them.
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})[/](\d{1,2})[/](\d{4})\b"),
)

_CLASSIFY_SYSTEM = (
    "You label one inbound business email or message. You answer with exactly one "
    "label and nothing else."
)

_CLASSIFY_PROMPT = """We sent a cold outreach message. This is their reply.

REPLY:
{body}

Answer with exactly one of these labels:
interested — they want to talk, buy, or know the price
question — they ask something before deciding
not_interested — a clear no
unsubscribe — they ask to be removed or to stop being contacted
out_of_office — an automatic absence reply
wrong_person — they are not the right contact, or they suggest someone else
auto_reply — any other automatic response (ticket receipt, mailbox rules)
bounce — a delivery failure notice
other — anything else

Decide the close cases this way:
- a reply that shows interest and also asks a question is interested
- an automated "we received your message" is auto_reply, not question
- a delivery failure is bounce
- anything that asks to be removed, in any phrasing or language, is unsubscribe
- unsure between two labels: choose the one that stops outreach
"""

_DRAFT_SYSTEM = (
    "You write short replies on behalf of a small business, as plain text — "
    "no Markdown. You answer only what they asked: no pitch, no restating the "
    "last email, at most 100 words. You never invent prices, delivery times, "
    "capabilities or commitments — if the SELLER NOTES do not cover "
    "something, you say you will check and come back rather than guessing. A "
    "clear rejection or a stop request gets no draft at all."
)

_DRAFT_PROMPT = """SELLER — what we do, in our own words:
{knowledge}

WHAT WE SENT THEM:
{last_outbound}

THEIR REPLY:
{body}

Write a reply of at most 100 words in {language}, tone: {tone}.
{instruction}
Answer only what they asked. No greeting flourish. No sign-off block.
"""

_INSTRUCTIONS: dict[str, str] = {
    "interested": (
        "They are interested. Confirm you can help, answer anything concrete they "
        "asked, and propose one specific next step."
    ),
    "question": (
        "Answer their question from the SELLER NOTES. If the notes do not cover it, "
        "say you will confirm and come back — do not guess."
    ),
    "wrong_person": (
        "They are not the right contact. Apologise briefly and ask, once, who the "
        "right person would be. Do not pitch."
    ),
}


class ReplyService:
    """Classifies inbound growth messages and acts on them."""

    def __init__(self, container: Any) -> None:
        self._c = container

    async def _system_prompt(self, tenant_id: UUID, *, language: str) -> str:
        """The draft persona, rendered from this tenant's facts at invocation.

        Only the *draft* persona is rendered. The classification prompt
        (``_CLASSIFY_SYSTEM``) is a parser contract — its output is matched
        against the fixed label set in ``CLASSES`` — and stays a byte-identical
        module constant. Falls back to the module constant when the agent layer
        is absent or a required fact is missing — the card names the gap, the
        run is never broken on it.
        """
        prompts = getattr(self._c, "agent_prompts", None)
        if prompts is None:
            return _DRAFT_SYSTEM
        try:
            return await prompts.system_for(
                tenant_id, AGENT_KEY, runtime={"language": language}
            )
        except MissingFacts:
            logger.warning(
                "agent_prompt_missing_facts",
                agent_key=AGENT_KEY,
                tenant_id=str(tenant_id),
            )
            return _DRAFT_SYSTEM

    # --------------------------------------------------------- entry points

    async def maybe_handle_email(
        self,
        tenant_id: UUID,
        *,
        sender: str,
        body: str,
        subject: str = "",
        provider_id: str = "",
    ) -> dict[str, Any] | None:
        """Handle an inbound email if it belongs to a growth thread, else None.

        None is the important return value: it means "not ours", and the email
        service carries on to the support runner exactly as before. A growth
        branch that guessed wrong here would silently stop answering a tenant's
        customers.
        """
        address = normalise_email(sender)
        if not address:
            return None
        lead = await self._c.growth.leads.get_by_address(tenant_id, email=address)
        if lead is None or not _routable(lead):
            return None
        return await self._handle(
            tenant_id,
            lead,
            channel="email",
            address=address,
            body=body,
            subject=subject,
            provider_id=provider_id,
        )

    async def maybe_handle_whatsapp(
        self,
        tenant_id: UUID,
        *,
        sender: str,
        body: str,
        provider_id: str = "",
    ) -> dict[str, Any] | None:
        """Handle an inbound WhatsApp message if it is on a growth thread."""
        phone = normalise_phone(sender)
        if not phone:
            return None
        lead = await self._c.growth.leads.get_by_address(tenant_id, phone=phone)
        if lead is None or not _routable(lead):
            return None
        return await self._handle(
            tenant_id,
            lead,
            channel="whatsapp",
            address=phone,
            body=body,
            provider_id=provider_id,
        )

    # -------------------------------------------------------------- handling

    async def _handle(
        self,
        tenant_id: UUID,
        lead: dict[str, Any],
        *,
        channel: str,
        address: str,
        body: str,
        subject: str = "",
        provider_id: str = "",
    ) -> dict[str, Any]:
        lead_id = UUID(str(lead["id"]))
        text = (body or "").strip()[:MAX_INBOUND_CHARS]
        language = detect_language(text) or str(lead.get("language") or "") or None

        stored = await self._c.growth.messages.add(
            tenant_id,
            lead_id=lead_id,
            channel=channel,
            direction="in",
            content=text,
            language=language,
            provider_id=provider_id or None,
            status="delivered",
        )
        if stored is None and provider_id:
            # the provider id collided: this exact message was already recorded,
            # so this is a redelivery. Classifying it again would cost a call and
            # could send a second reply to one inbound message
            return {"status": "duplicate", "lead_id": str(lead_id)}

        # the thread has a human in it now, whatever they said. The follow-up
        # cadence stops here rather than after classification, so a
        # classification failure cannot leave a replying lead being chased
        await self._c.growth.messages.mark_thread_replied(tenant_id, lead_id)
        await self._c.growth.messages.cancel_queued_for_lead(tenant_id, lead_id)

        label = await self._classify(tenant_id, text)
        if language:
            await self._remember_language(tenant_id, lead_id, language)

        result = await self._act(
            tenant_id,
            lead,
            label=label,
            channel=channel,
            address=address,
            body=text,
            language=language,
        )
        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="reply_classified",
            input={"lead_id": str(lead_id), "channel": channel, "subject": subject[:200]},
            output={"label": label, **result},
            reason=f"inbound {channel} reply classified as {label}",
        )
        return {"status": "handled", "label": label, "lead_id": str(lead_id), **result}

    async def _act(
        self,
        tenant_id: UUID,
        lead: dict[str, Any],
        *,
        label: str,
        channel: str,
        address: str,
        body: str,
        language: str | None,
    ) -> dict[str, Any]:
        lead_id = UUID(str(lead["id"]))

        if label in TERMINAL:
            return await self._terminal(tenant_id, lead_id, label, address, channel)

        if label == "out_of_office":
            until = _return_date(body)
            await self._c.growth.leads.pause_until(tenant_id, lead_id, until)
            return {"paused_until": until.isoformat()}

        if label == "auto_reply":
            # recorded, and nothing else. An auto-responder is not a human and
            # must not consume one of the three follow-ups
            return {"action": "recorded"}

        status = CLASSES.get(label)
        if status:
            await self._c.growth.leads.set_status(tenant_id, lead_id, status)
            # CRM push is queued, never blocking this path (on_lead_changed swallows all errors)
            await self._c.growth_crm.on_lead_changed(tenant_id, lead_id)

        if label == "interested":
            await self._ping_owner(tenant_id, lead, body)

        if label not in REPLYING:
            return {"action": "recorded"}

        drafted = await self._draft(
            tenant_id, lead, label=label, body=body, channel=channel, language=language
        )
        return {"action": "drafted" if drafted else "no_draft"}

    async def _terminal(
        self, tenant_id: UUID, lead_id: UUID, label: str, address: str, channel: str
    ) -> dict[str, Any]:
        """A no, an opt-out or a bounce: stop everything, on every channel."""
        if label == "not_interested":
            await self._c.growth.leads.set_status(tenant_id, lead_id, "lost")
            # CRM push is queued, never blocking this path (on_lead_changed swallows all errors)
            await self._c.growth_crm.on_lead_changed(tenant_id, lead_id)
            return {"action": "closed"}

        reason = "unsubscribed" if label == "unsubscribe" else "bounced"
        await self._c.growth_compliance.unsubscribe(
            tenant_id, address, reason=reason, source=f"{channel}_reply"
        )
        await self._c.growth.leads.set_status(tenant_id, lead_id, "suppressed")
        # CRM push is queued, never blocking this path (on_lead_changed swallows all errors)
        await self._c.growth_crm.on_lead_changed(tenant_id, lead_id)
        return {"action": "suppressed", "reason": reason}

    async def _ping_owner(
        self, tenant_id: UUID, lead: dict[str, Any], body: str
    ) -> None:
        """The hot-lead alert. Immediate, and in the owner's own language.

        Owner-facing text never goes through the language resolver: the customer
        wrote in Hindi, and that decides the *reply*, not what the owner reads on
        their phone at nine at night.
        """
        who = str(lead.get("person") or "").strip()
        company = str(lead.get("company") or lead.get("email") or "a lead").strip()
        head = f"{who} at {company}" if who else company
        try:
            await self._c.approvals.alert(
                tenant_id,
                f"Interested reply from {head}.",
                [body[:280]],
            )
        except Exception:
            # a notification outage must not lose the classification we just did
            logger.warning(
                "growth_hot_lead_alert_failed", tenant_id=str(tenant_id), exc_info=True
            )

    # ---------------------------------------------------------------- models

    async def _classify(self, tenant_id: UUID, body: str) -> str:
        """One label. Keyword opt-outs are decided before the model is asked."""
        if _looks_like_stop(body):
            return "unsubscribe"
        if not body.strip():
            return "other"

        from app.adapters.llm.base import Message

        llm = await self._c.agent_models.llm_for(tenant_id, AGENT_KEY, "utility")
        try:
            completion = await llm.complete(
                [
                    Message(role="system", content=_CLASSIFY_SYSTEM),
                    Message(role="user", content=_CLASSIFY_PROMPT.format(body=body)),
                ],
                max_tokens=CLASSIFY_MAX_TOKENS,
            )
        except Exception:
            # `other` routes to the owner, which is the right place for a message
            # nobody could classify — including because the classifier was down
            logger.warning(
                "growth_reply_classify_failed", tenant_id=str(tenant_id), exc_info=True
            )
            return "other"

        from app.services.growth.metering import meter_llm

        await meter_llm(
            tenant_id,
            completion.usage,
            feature=FEATURE,
            agent_key=AGENT_KEY,
            model=getattr(completion, "model", ""),
        )
        answer = (completion.text or "").strip().lower().replace(" ", "_")
        for label in CLASSES:
            if answer.startswith(label):
                return label
        return "other"

    async def _draft(
        self,
        tenant_id: UUID,
        lead: dict[str, Any],
        *,
        label: str,
        body: str,
        channel: str,
        language: str | None,
    ) -> bool:
        """Write a reply into the send queue. The approval level gates the send."""
        from app.adapters.llm.base import Message

        settings = await self._c.growth_profile.get(tenant_id)
        profile = settings.get("profile") or {}
        allowed = list((settings.get("languages") or {}).get("allowed") or ["en"])
        reply_language = resolve_language(
            body, str(lead.get("language") or "") or None, str(lead.get("phone") or ""), allowed
        )

        last = await self._c.growth.messages.last_outbound(
            tenant_id, UUID(str(lead["id"]))
        )
        knowledge = await self._knowledge(tenant_id, profile, body)

        llm = await self._c.agent_models.llm_for(tenant_id, AGENT_KEY, "primary")
        system = await self._system_prompt(tenant_id, language=reply_language)
        try:
            completion = await llm.complete(
                [
                    Message(role="system", content=system),
                    Message(
                        role="user",
                        content=_DRAFT_PROMPT.format(
                            knowledge=knowledge,
                            last_outbound=str((last or {}).get("content") or "(none)")[:2000],
                            body=body,
                            language=reply_language,
                            tone=str(profile.get("tone") or "formal"),
                            instruction=_INSTRUCTIONS.get(label, ""),
                        ),
                    ),
                ],
                max_tokens=DRAFT_MAX_TOKENS,
            )
        except Exception:
            logger.warning(
                "growth_reply_draft_failed", tenant_id=str(tenant_id), exc_info=True
            )
            return False

        from app.services.growth.metering import meter_llm

        await meter_llm(
            tenant_id,
            completion.usage,
            feature=FEATURE,
            agent_key=AGENT_KEY,
            model=getattr(completion, "model", ""),
        )
        draft = (completion.text or "").strip()
        if not draft:
            return False

        verdict = await self._c.growth_grounding.check(
            tenant_id, agent_key=AGENT_KEY, draft=draft, evidence=f"{knowledge}\n\n{body}"
        )
        if not verdict.may_send:
            await self._c.approvals.alert(
                tenant_id,
                (
                    f"A reply to {lead.get('company') or lead.get('email') or 'a lead'} "
                    "was held by the grounding check. Nothing was sent."
                ),
                [draft[:280]],
            )
            return False

        await self._c.growth.messages.add(
            tenant_id,
            lead_id=UUID(str(lead["id"])),
            channel=channel,
            direction="out",
            content=draft,
            language=reply_language,
            template_id=_reply_subject(lead) if channel == "email" else None,
            status="queued",
        )
        return True

    async def _knowledge(
        self, tenant_id: UUID, profile: dict[str, Any], question: str
    ) -> str:
        """The tenant's own answer to what was asked, from their RAG index.

        Retrieval keyed on the customer's question rather than on what we sell:
        a reply agent is answering *them*, and the chunks that matter are the
        ones about the thing they asked about.
        """
        query = question.strip() or str(profile.get("sells") or "")
        if not query:
            return ""
        try:
            from app.domain.agent_config import RetrievalConfig

            result = await self._c.retrieval.search(
                tenant_id, query, RetrievalConfig(top_k=4)
            )
        except Exception:
            logger.info("growth_reply_retrieval_failed", exc_info=True)
            return str(profile.get("sells") or "")
        return result.context_block(max_chars=2500).strip() or str(
            profile.get("sells") or ""
        )

    async def _remember_language(
        self, tenant_id: UUID, lead_id: UUID, language: str
    ) -> None:
        """Store what they actually wrote in, so later messages match it.

        Evidence beats every guess, permanently — the region hint that produced
        the first message's language is dropped the moment a human writes back,
        and this is where that happens.
        """
        lead = await self._c.growth.leads.get(tenant_id, lead_id)
        if lead is None or str(lead.get("language") or "") == language:
            return
        merged = dict(lead)
        merged["language"] = language
        await self._c.growth.leads.upsert(tenant_id, merged)
        # CRM push is queued, never blocking this path (on_lead_changed swallows all errors)
        await self._c.growth_crm.on_lead_changed(tenant_id, lead_id)


# ------------------------------------------------------------------- helpers


def _routable(lead: Mapping[str, Any]) -> bool:
    """Whether this lead's inbound message belongs to the growth agent.

    Pure, so the rule can be proved without a database — which the SQL predicate
    that also enforces it cannot be.
    """
    return str(lead.get("status") or "") not in NOT_ROUTABLE


def _looks_like_stop(body: str) -> bool:
    """A deterministic opt-out check, run before any model.

    Requires the keyword to be effectively the whole message. "Please remove me"
    is an opt-out; "we had to remove the old supplier" is not, and a substring
    match would suppress a perfectly interested lead.
    """
    words = [w for w in re.split(r"[^\w-]+", (body or "").lower()) if w]
    if not words or len(words) > 4:
        return False
    return any(word in _STOP_WORDS for word in words)


def _return_date(body: str) -> datetime:
    """When an out-of-office says they are back, or a default a week out.

    Bounded at both ends. A date in the past is a stale auto-reply and a date
    years out is a parse error, and either would silently park a lead forever.
    """
    now = datetime.now(UTC)
    for pattern in _DATE_PATTERNS:
        match = pattern.search(body or "")
        if not match:
            continue
        try:
            if len(match.group(1)) == 4:
                year, month, day = (int(g) for g in match.groups())
            else:
                day, month, year = (int(g) for g in match.groups())
            found = datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            continue
        if now < found <= now + timedelta(days=MAX_OOO_DAYS):
            return found
    return now + timedelta(days=DEFAULT_OOO_DAYS)


def _reply_subject(lead: dict[str, Any]) -> str:
    company = str(lead.get("company") or "").strip()
    return f"Re: {company}"[:200] if company else "Re: your message"
