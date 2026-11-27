"""The cold-mail agent: one mail per approved lead, and the gates around it.

Two entry points, and the split matters:

* ``release_batch`` runs when the owner approves a research batch. It moves each
  lead ``researched -> queued`` and enqueues one ``growth_compose`` job per lead.
  It writes no text and sends nothing.
* ``compose`` runs per lead. It writes one draft into ``growth_messages`` with
  status ``queued``; the Stage-1 dispatcher is what later sends it, inside the
  daily cap, inside working hours, with jittered gaps.

Nothing here sends. That separation is what makes the warmup ramp and the send
window real: the agent's job finishes at "a mail is written and waiting", and a
tenant on warmup day 3 has ninety drafts and a cap of thirty, which is correct.

The mail itself is short by policy — under 120 words, one specific reason taken
from the lead's own site, one clear ask, no throat-clearing. That is enforced in
three places rather than trusted to the prompt: the prompt asks for it, the word
count is checked and a long draft is re-cut, and the grounding judge sees the
final text before it can be queued.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.domain.growth import normalise_email
from app.services.agents.render import MissingFacts

logger = get_logger(__name__)

AGENT_KEY = "growth_compose"
COMPOSE_JOB = "growth_compose"
BATCH_RELEASE_JOB = "growth_batch_release"
FEATURE = "outbound"
CHANNEL = "email"

# The style ceiling, and the transport ceiling. A mail over the word limit is
# re-cut rather than rejected: the model wrote something usable and the fix is
# mechanical.
MAX_WORDS = 120
# Generous ceiling: Gemini 3 thinking (HIGH) shares the budget.
COMPOSE_MAX_TOKENS = 20000

# Consumer mailbox providers. A lead whose only address is one of these and who
# has no company domain is a person, not a business, and cold-mailing a person
# is a different legal question in every market this sells into. Skipped at
# compose time and logged, never silently dropped.
CONSUMER_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "ymail.com",
        "hotmail.com", "outlook.com", "live.com", "msn.com", "aol.com",
        "rediffmail.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
        "zoho.com", "mail.com", "gmx.com",
    }
)

# How much of the tenant's own knowledge base to put in front of the writer.
# Three chunks: enough to say what we actually sell in the tenant's own words,
# small enough that the prompt stays cheap on a per-lead call.
RAG_CHUNKS = 3
RAG_CHARS = 2000

_SYSTEM = (
    "You write short cold emails for a small business, as plain text — no "
    "Markdown of any kind, no bullet dashes, no tables. You write plainly, "
    "like a person, not like marketing. You never claim anything the RESEARCH "
    "or the SELLER NOTES do not support — if you are unsure of a fact, you "
    "leave it out rather than soften it. You never open with 'I hope this "
    "email finds you well', 'Hope you're doing well', 'I wanted to reach "
    "out', 'Quick question', or any variation, and you never write an "
    "unsubscribe line or a postal footer — the platform appends one. When the "
    "RESEARCH gives you no specific reason to write to this company, you "
    "reply with exactly: NO BASIS"
)

_PROMPT = """SELLER — what we do, in our own words:
{knowledge}

RESEARCH — what we know about them, and where it came from:
{research}

THEM: {company}{person_line}

Write one email of at most {max_words} words, in {language}, tone: {tone}.
Structure: one line naming the specific thing about THEM from the RESEARCH that
made us write; one or two lines on what we do that connects to it; one clear,
small ask (a reply, or a ten-minute call). No greeting flourish, no sign-off
block — a plain sign-off line is enough.

First output line: SUBJECT: <a subject line of at most 60 characters, lower-case,
no exclamation marks>
Then a blank line, then the email body.

If the RESEARCH gives you no specific reason to write to this company, reply with
exactly: NO BASIS
"""

NO_BASIS = "NO BASIS"


class ColdMailService:
    """Turns approved leads into queued drafts. Sends nothing."""

    def __init__(self, container: Any) -> None:
        self._c = container

    async def _system_prompt(self, tenant_id: UUID, *, language: str) -> str:
        """The persona, rendered from this tenant's facts at invocation.

        Only the persona is rendered: the NO BASIS refusal this service parses
        out of the model's output is instructed by the user prompt and checked
        against the ``NO_BASIS`` constant, both untouched here. Falls back to
        the module constant when the agent layer is absent or a required fact
        is missing — the card names the gap, the run is never broken on it.
        """
        prompts = getattr(self._c, "agent_prompts", None)
        if prompts is None:
            return _SYSTEM
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
            return _SYSTEM

    # -------------------------------------------------------- batch release

    async def release_batch(
        self, tenant_id: UUID, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """The owner approved a research batch: queue its leads for composing.

        Reads the lead ids from the approval row rather than the job payload.
        The payload is what the queue carries and could be replayed; the
        approval row is what the owner actually decided on, including any edits
        they made — so an owner who removed three leads before approving gets a
        batch of the size they approved, not the size that was proposed.
        """
        approval_id = payload.get("approval_id")
        if approval_id is None:
            return {"status": "skipped", "reason": "no approval attached"}

        approval = await self._c.growth.approvals.get(tenant_id, UUID(str(approval_id)))
        if approval is None:
            logger.warning(
                "growth_batch_release_missing_approval",
                tenant_id=str(tenant_id),
                approval_id=str(approval_id),
            )
            return {"status": "skipped", "reason": "approval not found"}
        if approval["status"] not in ("approved", "edited"):
            return {"status": "skipped", "reason": f"approval is {approval['status']}"}

        stored = approval.get("payload") or {}
        draft = stored.get("draft")
        if isinstance(draft, dict) and str(draft.get("body") or "").strip():
            # a level-2 approval, not a batch: the mail is already written
            return await self._release_draft(tenant_id, stored, draft)

        lead_ids = _lead_ids(approval.get("payload") or {})
        if not lead_ids:
            return {"status": "ok", "queued": 0}

        # only the leads still sitting at `researched` move. The predicate is the
        # idempotency: a redelivered release callback, or an owner who pressed
        # approve twice, queues each lead exactly once
        moved = await self._c.growth.leads.advance_status(
            tenant_id, lead_ids, from_status="researched", to_status="queued"
        )
        if moved == 0:
            return {"status": "ok", "queued": 0, "reason": "already released"}

        # CRM push per released lead. advance_status reports a count, not which
        # rows moved, so a redelivered release re-pushes a few leads that stayed
        # put — harmless, the push just re-syncs (on_lead_changed never raises)
        for lead_id in lead_ids:
            await self._c.growth_crm.on_lead_changed(tenant_id, lead_id)

        from app.adapters.queue.base import Job

        queued = 0
        for lead_id in lead_ids:
            try:
                await self._c.queue.enqueue(
                    Job(
                        kind=COMPOSE_JOB,
                        tenant_id=tenant_id,
                        payload={
                            "lead_id": str(lead_id),
                            "batch_id": str(approval["batch_id"]),
                        },
                    )
                )
                queued += 1
            except Exception:
                logger.warning(
                    "growth_compose_enqueue_failed",
                    tenant_id=str(tenant_id),
                    lead_id=str(lead_id),
                    exc_info=True,
                )

        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="batch_released",
            input={"approval_id": str(approval_id), "leads": len(lead_ids)},
            output={"advanced": moved, "queued": queued},
            reason="owner approved the outbound batch",
        )
        return {"status": "ok", "queued": queued, "advanced": moved}

    async def _release_draft(
        self, tenant_id: UUID, stored: dict[str, Any], draft: dict[str, Any]
    ) -> dict[str, Any]:
        """Queue the one mail a level-2 approval carries.

        Both HITL levels use ``feature='outbound_batch'``, so both decisions
        release through this job — but they release different things. Level 1
        approves a *batch* of researched leads, and the path below moves each
        one ``researched -> queued``. Level 2 approves a *written mail*, and by
        then its lead is long past ``researched`` (the batch that produced it
        was released before the mail was written), so the batch path advanced
        nothing, reported "already released", and dropped the draft on the
        floor: the one message an owner explicitly said yes to was the only one
        that never went out.

        The stored payload is the source of the text, which is what makes an
        edit real — ``decide('edited', ...)`` replaced it, so an owner who
        rewrote the mail on the panel gets their words sent, not the model's.
        """
        lead_ids = _lead_ids(stored)
        if not lead_ids:
            return {"status": "skipped", "reason": "no lead on this approval"}

        lead = await self._c.growth.leads.get(tenant_id, lead_ids[0])
        if lead is None:
            return {"status": "skipped", "reason": "lead no longer exists"}
        # the same idempotency record the compose path uses: a redelivered
        # release callback must not queue the mail a second time
        if await self._c.growth.messages.has_outbound_for_lead(
            tenant_id, UUID(str(lead["id"]))
        ):
            return {"status": "ok", "queued": 0, "reason": "already released"}

        subject = str(draft.get("subject") or "").strip() or fallback_subject(lead)
        body = str(draft.get("body") or "").strip()
        result = await self._queue(
            tenant_id, lead, subject, body, _StoredVerdict(stored)
        )
        return {"status": "ok", "queued": 1, "message": result}

    # -------------------------------------------------------------- compose

    async def compose(self, tenant_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """Write one lead's cold mail into the send queue."""
        lead_id = payload.get("lead_id")
        if lead_id is None:
            return {"status": "skipped", "reason": "no lead"}
        lead = await self._c.growth.leads.get(tenant_id, UUID(str(lead_id)))
        if lead is None:
            return {"status": "skipped", "reason": "lead no longer exists"}

        # a redelivered callback must not queue a second cold mail. The message
        # log is the idempotency record, not a flag on the lead: the lead's
        # status moves for several reasons and this question is only about
        # whether anything was ever written
        if await self._c.growth.messages.has_outbound_for_lead(
            tenant_id, UUID(str(lead["id"]))
        ):
            return {"status": "skipped", "reason": "already written"}

        settings = await self._c.growth_profile.get(tenant_id)
        profile = settings.get("profile") or {}
        skip = _entry_check(profile, lead)
        if skip:
            await self._skip(tenant_id, lead, skip)
            return {"status": "skipped", "reason": skip}

        research = str(lead.get("research_summary") or "").strip()
        knowledge = await self._knowledge(tenant_id, profile)
        subject, body = await self._write(tenant_id, lead, profile, research, knowledge)
        if not body:
            await self._skip(tenant_id, lead, "no supported reason to write")
            return {"status": "skipped", "reason": "no basis"}

        verdict = await self._c.growth_grounding.check(
            tenant_id,
            agent_key=AGENT_KEY,
            draft=body,
            evidence=f"{research}\n\n{knowledge}",
        )
        if not verdict.may_send:
            await self._block(tenant_id, lead, body, verdict)
            return {"status": "held", "reason": "grounding check failed"}

        level = int((settings.get("hitl_levels") or {}).get("outbound_batch", 1))
        if level >= 2:
            # level 2 is per-message approval. The draft is *not* queued: it is
            # put in front of the owner, and the approval's release is what
            # queues it. A draft queued "pending approval" would be one
            # scheduler bug away from going out unapproved
            return await self._ask_per_message(tenant_id, lead, subject, body, verdict)

        return await self._queue(tenant_id, lead, subject, body, verdict)

    # ---------------------------------------------------------------- private

    async def _write(
        self,
        tenant_id: UUID,
        lead: dict[str, Any],
        profile: dict[str, Any],
        research: str,
        knowledge: str,
    ) -> tuple[str, str]:
        """One LLM call. Returns ``("", "")`` when there is no basis to write."""
        from app.adapters.llm.base import Message
        from app.domain.growth import resolve_language

        language = resolve_language(
            None,
            str(lead.get("language") or "") or None,
            str(lead.get("phone") or "") or None,
            list((await self._c.growth_profile.get(tenant_id))
                 .get("languages", {}).get("allowed") or ["en"]),
        )
        person = str(lead.get("person") or "").strip()
        prompt = _PROMPT.format(
            knowledge=knowledge or str(profile.get("sells") or ""),
            research=research or "(nothing specific was found about this company)",
            company=str(lead.get("company") or lead.get("domain") or "this company"),
            person_line=f"\nCONTACT: {person}, {lead.get('role') or 'unknown role'}"
            if person
            else "",
            max_words=MAX_WORDS,
            language=language,
            tone=str(profile.get("tone") or "formal"),
        )

        llm = await self._c.agent_models.llm_for(tenant_id, AGENT_KEY, "primary")
        system = await self._system_prompt(tenant_id, language=language)
        try:
            completion = await llm.complete(
                [
                    Message(role="system", content=system),
                    Message(role="user", content=prompt),
                ],
                max_tokens=COMPOSE_MAX_TOKENS,
            )
        except Exception:
            logger.warning(
                "growth_compose_llm_failed",
                tenant_id=str(tenant_id),
                lead_id=str(lead["id"]),
                exc_info=True,
            )
            return "", ""

        from app.services.growth.metering import meter_llm

        await meter_llm(
            tenant_id,
            completion.usage,
            feature=FEATURE,
            agent_key=AGENT_KEY,
            model=getattr(completion, "model", ""),
        )

        text = (completion.text or "").strip()
        if not text or text.upper().startswith(NO_BASIS):
            return "", ""
        subject, body = split_subject(text)
        return subject or fallback_subject(lead), trim_words(body, MAX_WORDS)

    async def _knowledge(self, tenant_id: UUID, profile: dict[str, Any]) -> str:
        """A few lines of the tenant's own product knowledge, from their RAG index.

        The profile's ``sells`` is one sentence a human typed at onboarding; the
        index is everything they have uploaded since. Using both means the mail
        describes the product the way the tenant describes it, which is the
        difference between a mail that sounds like them and one that sounds like
        a model.

        Retrieval failure is not an error here — the profile sentence is a
        perfectly good fallback, and a search outage must not stop outbound.
        """
        query = str(profile.get("sells") or "").strip()
        if not query:
            return ""
        try:
            from app.domain.agent_config import RetrievalConfig

            result = await self._c.retrieval.search(
                tenant_id, query, RetrievalConfig(top_k=RAG_CHUNKS)
            )
        except Exception:
            logger.info("growth_compose_retrieval_failed", exc_info=True)
            return query

        # context_block is the same renderer the chat path uses, so the writer
        # sees the tenant's knowledge in exactly the shape everything else does
        text = result.context_block(max_chars=RAG_CHARS)
        return text.strip() or query

    async def _queue(
        self,
        tenant_id: UUID,
        lead: dict[str, Any],
        subject: str,
        body: str,
        verdict: Any,
    ) -> dict[str, Any]:
        """Write the draft into the send queue and move the lead to ``mailed``.

        ``mailed`` on queueing rather than on sending, deliberately: the
        follow-up clock starts from ``last_outbound_at``, which the *sender*
        stamps, so the status and the clock cannot drift into a state where a
        lead is chased before it was written to.
        """
        row = await self._c.growth.messages.add(
            tenant_id,
            lead_id=UUID(str(lead["id"])),
            channel=CHANNEL,
            direction="out",
            content=body,
            language=str(lead.get("language") or "") or None,
            # the subject rides in template_id: that column is unused on email,
            # and a second content column on growth_messages would be a schema
            # change to carry one short string only one channel has
            template_id=subject,
            status="queued",
        )
        await self._c.growth.leads.set_status(tenant_id, UUID(str(lead["id"])), "mailed")
        # CRM push is queued, never blocking this path (on_lead_changed swallows all errors)
        await self._c.growth_crm.on_lead_changed(tenant_id, UUID(str(lead["id"])))
        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="mail_drafted",
            input={"lead_id": str(lead["id"])},
            output={"message_id": str(row.get("id") if row else ""), **verdict.as_meta()},
            reason="queued for the next send window",
        )
        return {"status": "queued", "message_id": str(row.get("id") if row else "")}

    async def _ask_per_message(
        self,
        tenant_id: UUID,
        lead: dict[str, Any],
        subject: str,
        body: str,
        verdict: Any,
    ) -> dict[str, Any]:
        """Level 2: the owner reads this exact mail before it is queued."""
        await self._c.approvals.request_approval(
            tenant_id,
            feature="outbound_batch",
            payload={
                "items": [{"lead_id": str(lead["id"]), "label": f"{subject} — {body[:200]}"}],
                "lead_ids": [str(lead["id"])],
                "draft": {"subject": subject, "body": body},
                **verdict.as_meta(),
            },
            summary=f"Approve this mail to {lead.get('company') or lead.get('email')}?",
        )
        return {"status": "awaiting_approval"}

    async def _block(
        self, tenant_id: UUID, lead: dict[str, Any], body: str, verdict: Any
    ) -> None:
        """Block mode refused this mail: hold it and tell the owner.

        Deliberately *not* routed through ``EscalationService``. That service
        gates on the RAG agent's own trigger list and needs an ``AgentConfig``
        this agent does not have — so a tenant who had turned `ungrounded` off
        for their support agent would silently disable the outbound gate too,
        which is the opposite of what turning on block mode means. The owner
        alert goes out through the Stage-1 fan-out, which is the same notifier
        set an escalation would have reached anyway.
        """
        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="mail_blocked_ungrounded",
            input={"lead_id": str(lead["id"])},
            output={"draft": body[:1000], **verdict.as_meta()},
            reason=verdict.reason or "the grounding judge refused this draft",
        )
        await self._c.approvals.alert(
            tenant_id,
            (
                f"A mail to {lead.get('company') or lead.get('email') or 'a lead'} was "
                "held: the grounding check could not confirm it against your research. "
                "Nothing was sent."
            ),
            [],
        )
        logger.info(
            "growth_compose_blocked",
            tenant_id=str(tenant_id),
            lead_id=str(lead["id"]),
            state=verdict.state,
        )

    async def _skip(self, tenant_id: UUID, lead: dict[str, Any], reason: str) -> None:
        """Record a lead we chose not to write to, and why.

        ``later`` rather than ``lost``: the reason is usually something that can
        change — a missing address, a consumer mailbox with no company behind it
        yet — and a lead parked with a reason is one the owner can act on.
        """
        await self._c.growth.leads.set_status(tenant_id, UUID(str(lead["id"])), "later")
        # CRM push is queued, never blocking this path (on_lead_changed swallows all errors)
        await self._c.growth_crm.on_lead_changed(tenant_id, UUID(str(lead["id"])))
        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="lead_skipped",
            input={"lead_id": str(lead["id"])},
            reason=reason,
        )
        logger.info(
            "growth_compose_skipped",
            tenant_id=str(tenant_id),
            lead_id=str(lead["id"]),
            reason=reason,
        )


# ------------------------------------------------------------------- helpers


def _entry_check(profile: dict[str, Any], lead: dict[str, Any]) -> str:
    """Why this lead must not be cold-mailed, or "" when it may be."""
    # Unchosen ("") counts as applicable — only an explicit choice opts out.
    if str(profile.get("tenant_type") or "") not in ("", "A", "both"):
        return "this tenant is not set up for B2B outbound"
    email = normalise_email(str(lead.get("email") or ""))
    if not email:
        return "no email address"
    if not str(profile.get("postal_address") or "").strip():
        return "the business profile has no postal address yet"

    mailbox_domain = email.rsplit("@", 1)[-1]
    company_context = bool(
        str(lead.get("domain") or "").strip() or str(lead.get("company") or "").strip()
    )
    if mailbox_domain in CONSUMER_DOMAINS and not company_context:
        return "consumer mailbox with no company behind it"
    return ""


class _StoredVerdict:
    """The grounding verdict as it stood when the draft was written.

    ``_queue`` only ever asks a verdict for ``as_meta()``, and re-judging on
    release would pay a second LLM call to re-answer a question about text the
    judge has already seen. What the owner decided on is what gets recorded.
    """

    __slots__ = ("_meta",)

    def __init__(self, payload: dict[str, Any]) -> None:
        self._meta = {
            key: value
            for key, value in payload.items()
            if isinstance(key, str) and key.startswith("grounding")
        }

    def as_meta(self) -> dict[str, Any]:
        return dict(self._meta)


def _lead_ids(payload: dict[str, Any]) -> list[UUID]:
    """The approved lead ids, from either shape the payload can take.

    ``lead_ids`` is what the research agent writes; ``items`` is what an owner's
    edit leaves behind when they removed some. Reading both means an edited
    approval releases what the owner actually kept.
    """
    raw = payload.get("lead_ids")
    if not isinstance(raw, list) or not raw:
        items = payload.get("items")
        raw = [
            item.get("lead_id")
            for item in items
            if isinstance(item, dict) and item.get("lead_id")
        ] if isinstance(items, list) else []

    out: list[UUID] = []
    for value in raw:
        try:
            out.append(UUID(str(value)))
        except (ValueError, TypeError):
            continue
    return out


def split_subject(text: str) -> tuple[str, str]:
    """Pull the ``SUBJECT:`` line off the front of the completion."""
    lines = text.splitlines()
    subject = ""
    start = 0
    for index, line in enumerate(lines[:3]):
        stripped = line.strip()
        if stripped.upper().startswith("SUBJECT:"):
            subject = stripped[len("SUBJECT:") :].strip()[:200]
            start = index + 1
            break
    body = "\n".join(lines[start:]).strip()
    return subject, body


def trim_words(body: str, limit: int) -> str:
    """Cut an over-long draft at a sentence boundary, not mid-word.

    The model was asked for 120 words and sometimes writes 140. Truncating
    mid-sentence would produce a mail that reads as broken; cutting back to the
    last full sentence produces one that reads as short, which is what was
    wanted anyway.
    """
    words = body.split()
    if len(words) <= limit:
        return body
    cut = " ".join(words[:limit])
    for terminator in (". ", "! ", "? ", ".\n"):
        index = cut.rfind(terminator)
        if index > len(cut) // 2:
            return cut[: index + 1].strip()
    return cut.rstrip(",;:") + "."


def fallback_subject(lead: dict[str, Any]) -> str:
    company = str(lead.get("company") or "").strip()
    return f"quick question about {company}"[:200] if company else "quick question"
