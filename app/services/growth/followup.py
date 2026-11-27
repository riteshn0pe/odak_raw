"""The follow-up agent: day 3, day 7, day 14, then stop.

Three rules, and the third is the one that makes the feature worth having:

1. **At most three.** The ceiling is the tenant's ``follow_up.max_follow_ups``
   and it is enforced in the SQL predicate, not in a loop — a fourth follow-up
   is not a message we choose not to send, it is a row the query cannot return.
2. **Stop on any signal.** A reply, a suppression, a pause, an owner marking the
   lead closed: all of them move the lead off ``mailed``, and ``mailed`` is the
   only status this agent reads.
3. **Each one adds something new.** The prompt is given the whole thread and is
   told what it may not repeat. "Just checking in" is worse than silence — it
   teaches the recipient that our mail carries no information, which is a
   lesson they apply to every future mail including the good one.

Like the cold-mail agent, this writes drafts and sends nothing: the Stage-1
dispatcher drains the queue under the same cap, window and jitter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.services.agents.render import MissingFacts

logger = get_logger(__name__)

AGENT_KEY = "growth_compose"
# The registry key this agent's prompt renders from. The split-out from
# growth_compose (agent.md §8) gave follow-up its own card and template, while
# the module AGENT_KEY above still routes the model call and the metering.
PROMPT_AGENT_KEY = "growth_followup"
FOLLOW_UP_JOB = "growth_follow_up"
FEATURE = "outbound"
CHANNEL = "email"

MAX_WORDS = 90
# Generous ceiling: Gemini 3 thinking (HIGH) shares the budget.
FOLLOW_UP_MAX_TOKENS = 20000

# How many leads one follow-up pass writes for. Bounded for the same reason
# SEND_BATCH_SIZE is: the ceiling is the callback's wall clock, and a tenant with
# a large backlog is served across ticks rather than in one timeout.
BATCH = 40

_SYSTEM = (
    "You write short follow-up emails for a small business, as plain text. "
    "Every follow-up must carry something the earlier messages did not — a "
    "different angle, a concrete example, a specific question. You never "
    "write 'just checking in', 'following up', 'bumping this', 'circling "
    "back', 'did you get a chance to', or any variation, and you never "
    "manufacture urgency or invent a deadline. When you have nothing new to "
    "add, you reply with exactly: NOTHING NEW"
)

_PROMPT = """SELLER — what we do, in our own words:
{knowledge}

WHAT WE ALREADY SENT THEM (do not repeat any of this):
{history}

WHAT WE KNOW ABOUT THEM:
{research}

This is follow-up number {number} of {total}. They have not replied.

Write one email of at most {max_words} words in {language}, tone: {tone}. Lead
with something new — a different angle on their situation from WHAT WE KNOW, or
one concrete example. End with one small, easy question.

First output line: SUBJECT: <at most 60 characters, lower-case>
Then a blank line, then the body.

If you have nothing new to say that the earlier messages did not already say,
reply with exactly: NOTHING NEW
"""

NOTHING_NEW = "NOTHING NEW"


class FollowUpService:
    """Queues the follow-ups that are due today."""

    def __init__(self, container: Any) -> None:
        self._c = container

    async def _system_prompt(self, tenant_id: UUID, *, language: str) -> str:
        """The persona, rendered from this tenant's facts at invocation.

        Only the persona: the NOTHING NEW refusal this service parses out of
        the model's output is instructed by the user prompt and checked against
        the ``NOTHING_NEW`` constant, both untouched. Falls back to the module
        constant when the agent layer is absent or a required fact is missing.
        """
        prompts = getattr(self._c, "agent_prompts", None)
        if prompts is None:
            return _SYSTEM
        try:
            return await prompts.system_for(
                tenant_id, PROMPT_AGENT_KEY, runtime={"language": language}
            )
        except MissingFacts:
            logger.warning(
                "agent_prompt_missing_facts",
                agent_key=PROMPT_AGENT_KEY,
                tenant_id=str(tenant_id),
            )
            return _SYSTEM

    async def run(self, tenant_id: UUID) -> dict[str, Any]:
        """One ``growth_follow_up`` pass for one tenant."""
        settings = await self._c.growth_profile.get(tenant_id)
        block = settings.get("follow_up") or {}
        offsets = [int(day) for day in block.get("days") or []]
        maximum = int(block.get("max_follow_ups", 0))
        if not offsets or maximum <= 0:
            return {"status": "ok", "queued": 0, "reason": "follow-ups are off"}

        offsets = sorted(offsets)[:maximum]
        now = datetime.now(UTC)
        # the earliest offset decides who is even a candidate; which offset a
        # given lead is due for is then decided per lead from its own count
        cutoff = now - timedelta(days=offsets[0])

        due = await self._c.growth.leads.due_follow_ups(
            tenant_id, before=cutoff, max_follow_ups=maximum, limit=BATCH
        )
        if not due:
            return {"status": "ok", "queued": 0}

        knowledge = await self._knowledge(tenant_id, settings)
        queued = 0
        skipped = 0
        for lead in due:
            try:
                if await self._one(tenant_id, lead, offsets, now, settings, knowledge):
                    queued += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1
                logger.warning(
                    "growth_follow_up_lead_failed",
                    tenant_id=str(tenant_id),
                    lead_id=str(lead.get("id")),
                    exc_info=True,
                )

        logger.info(
            "growth_follow_up_queued",
            tenant_id=str(tenant_id),
            due=len(due),
            queued=queued,
            skipped=skipped,
        )
        return {"status": "ok", "due": len(due), "queued": queued, "skipped": skipped}

    # ---------------------------------------------------------------- private

    async def _one(
        self,
        tenant_id: UUID,
        lead: dict[str, Any],
        offsets: list[int],
        now: datetime,
        settings: dict[str, Any],
        knowledge: str,
    ) -> bool:
        """Queue this lead's next follow-up, if this one is actually due.

        The candidate query filters on the *earliest* offset, so a lead mailed
        four days ago comes back as a candidate for follow-up 2 as well as 1.
        This is where that is resolved: the lead's own ``follow_up_count`` picks
        the offset, and a lead whose next offset has not arrived is put back
        rather than chased early.
        """
        sent = int(lead.get("follow_up_count") or 0)
        if sent >= len(offsets):
            return False
        last = lead.get("last_outbound_at")
        if last is None:
            return False
        if isinstance(last, datetime) and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if now - last < timedelta(days=offsets[sent]):
            return False

        lead_id = UUID(str(lead["id"]))
        thread = await self._c.growth.messages.thread_for_lead(tenant_id, lead_id, limit=6)
        history = "\n\n---\n\n".join(
            str(row.get("content") or "")
            for row in reversed(thread)
            if str(row.get("direction")) == "out"
        )

        subject, body = await self._write(
            tenant_id,
            lead,
            settings=settings,
            knowledge=knowledge,
            history=history,
            number=sent + 1,
            total=len(offsets),
        )
        if not body:
            return False

        verdict = await self._c.growth_grounding.check(
            tenant_id,
            agent_key=AGENT_KEY,
            draft=body,
            evidence=f"{lead.get('research_summary') or ''}\n\n{knowledge}",
        )
        if not verdict.may_send:
            logger.info(
                "growth_follow_up_blocked",
                tenant_id=str(tenant_id),
                lead_id=str(lead_id),
                state=verdict.state,
            )
            return False

        await self._c.growth.messages.add(
            tenant_id,
            lead_id=lead_id,
            channel=CHANNEL,
            direction="out",
            content=body,
            language=str(lead.get("language") or "") or None,
            template_id=subject,
            status="queued",
        )
        # counted at queue time, not at send time. A follow-up that is written
        # and then blocked by the daily cap must still count, or the next pass
        # writes a second copy of the same one
        await self._c.growth.leads.bump_follow_up(tenant_id, lead_id)
        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="follow_up_drafted",
            input={"lead_id": str(lead_id), "number": sent + 1},
            output=verdict.as_meta(),
            reason=f"follow-up {sent + 1} of {len(offsets)} queued",
        )
        return True

    async def _write(
        self,
        tenant_id: UUID,
        lead: dict[str, Any],
        *,
        settings: dict[str, Any],
        knowledge: str,
        history: str,
        number: int,
        total: int,
    ) -> tuple[str, str]:
        from app.adapters.llm.base import Message
        from app.services.growth.coldmail import fallback_subject, split_subject, trim_words

        profile = settings.get("profile") or {}
        prompt = _PROMPT.format(
            knowledge=knowledge,
            history=history or "(nothing recorded)",
            research=str(lead.get("research_summary") or "(nothing specific)"),
            number=number,
            total=total,
            max_words=MAX_WORDS,
            language=str(lead.get("language") or "en"),
            tone=str(profile.get("tone") or "formal"),
        )
        llm = await self._c.agent_models.llm_for(tenant_id, AGENT_KEY, "primary")
        system = await self._system_prompt(
            tenant_id, language=str(lead.get("language") or "en")
        )
        try:
            completion = await llm.complete(
                [
                    Message(role="system", content=system),
                    Message(role="user", content=prompt),
                ],
                max_tokens=FOLLOW_UP_MAX_TOKENS,
            )
        except Exception:
            logger.warning(
                "growth_follow_up_llm_failed", tenant_id=str(tenant_id), exc_info=True
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
        if not text or text.upper().startswith(NOTHING_NEW):
            # a real outcome. Saying nothing is better than saying "just
            # checking in", and the lead keeps its remaining follow-ups for a
            # pass that has something to say
            return "", ""
        subject, body = split_subject(text)
        return subject or fallback_subject(lead), trim_words(body, MAX_WORDS)

    async def _knowledge(self, tenant_id: UUID, settings: dict[str, Any]) -> str:
        """The seller's own words, fetched once per pass rather than per lead."""
        profile = settings.get("profile") or {}
        query = str(profile.get("sells") or "").strip()
        if not query:
            return ""
        try:
            from app.domain.agent_config import RetrievalConfig

            result = await self._c.retrieval.search(
                tenant_id, query, RetrievalConfig(top_k=3)
            )
        except Exception:
            logger.info("growth_follow_up_retrieval_failed", exc_info=True)
            return query
        return result.context_block(max_chars=2000).strip() or query
