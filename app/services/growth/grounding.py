"""The per-agent groundedness guard for growth deliverables.

Every agent's final text — a cold mail, a reply draft, a research summary line —
passes through here before it can reach a stranger. The check is modelled on
``runner._verify``: the same yes/no judge on the utility tier, the same
three-state verdict, the same fail-open posture. It is a separate module rather
than a refactor of ``runner.py`` because the two callers want different things
from the same question — the chat path delivers an unverifiable answer, and an
outbound path must be able to refuse to send one.

Three states, and the third is the one that matters:

* **grounded** — the judge said yes.
* **ungrounded** — the judge said no.
* **unknown** — the judge never answered (outage, timeout, no provider). In
  *advisory* mode this passes, because an outage must not silently stop a
  tenant's outbound; in *block* mode it does not, because a tenant who asked for
  a hard gate did not ask for one that opens when the guard is asleep.

Per-agent configuration lives in ``growth_settings.grounding``, validated by
``profile.GroundingBlock``: ``{"agents": {"growth_compose": true, ...}, "mode":
"advisory" | "block"}``. The default is advisory for every agent, so a tenant
who has never opened the settings screen gets visibility rather than a wall.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.core.logging import get_logger

logger = get_logger(__name__)

# Verdicts. `unknown` is not a failure of the text; it is a failure of the check.
GROUNDED = "grounded"
UNGROUNDED = "ungrounded"
UNKNOWN = "unknown"

MODE_ADVISORY = "advisory"
MODE_BLOCK = "block"

# The judge answers in one word. Anything longer is the model ignoring the
# instruction, and the first word is still the answer.
# Generous ceiling: Gemini 3 thinking (HIGH) shares the budget.
JUDGE_MAX_TOKENS = 20000

# A judge that has not answered in this long is not going to. Bounded here
# rather than left to the provider's own timeout so one slow verdict cannot eat
# a whole send batch's callback budget.
JUDGE_TIMEOUT_SECONDS = 12.0

# Evidence longer than this is truncated. The judge is checking claims against
# sources, and a source block that fills the context window costs more than the
# generation it is checking.
MAX_EVIDENCE_CHARS = 8000

# Which cost line a judge's own tokens belong on. The judge runs for whichever
# agent asked, so a single hard-coded tag put a report's and a campaign's
# verification cost on the outbound bill — a breakdown that looks right and is
# not, which is exactly what the fixed feature set in §1.10 exists to prevent.
# Anything unmapped falls to `outbound`, the surface this module was built for.
AGENT_FEATURES: dict[str, str] = {
    "growth_research": "outbound",
    "growth_compose": "outbound",
    "growth_replies": "inbound",
    "growth_inbound": "inbound",
    "growth_report": "report",
    "growth_campaign": "campaign",
    "growth_insights": "insights",
    "growth_talkprep": "talkprep",
    "growth_reviews": "reviews",
}
DEFAULT_FEATURE = "outbound"

_PROMPT = (
    "Does the DRAFT stay within what the SOURCES support?\n"
    "A claim about the recipient's own business, their products, their size or "
    "their needs must appear in the sources. Ordinary courtesy, a question, and "
    "a description of what the sender offers are not claims about the sources; "
    "ignore them.\n"
    "Reply with exactly one word: yes or no.\n\n"
    "SOURCES:\n{evidence}\n\nDRAFT:\n{draft}"
)


@dataclass(slots=True, frozen=True)
class Verdict:
    """The judge's answer, and what the caller is allowed to do with it."""

    state: str
    mode: str
    checked: bool
    reason: str = ""

    @property
    def may_send(self) -> bool:
        """Whether this text may go to a stranger.

        Advisory always sends — the verdict is recorded on the audit row and
        shown in the panel, and that is the whole point of advisory. Block sends
        only on an affirmative ``grounded``: ``unknown`` is refused there, which
        is the one place the two modes differ in how they treat an outage.
        """
        if not self.checked or self.mode != MODE_BLOCK:
            return True
        return self.state == GROUNDED

    def as_meta(self) -> dict[str, Any]:
        """The audit-row shape. Small, and the same for every agent."""
        return {
            "grounding": self.state,
            "grounding_mode": self.mode,
            "grounding_checked": self.checked,
        }


class GroundingService:
    """Runs the judge for one agent's deliverable."""

    def __init__(self, container: Any) -> None:
        self._container = container

    async def check(
        self,
        tenant_id: UUID,
        *,
        agent_key: str,
        draft: str,
        evidence: str,
    ) -> Verdict:
        """Judge one draft against the evidence it was written from.

        Returns ``checked=False`` — never an exception — when the agent's toggle
        is off, when there is nothing to check against, or when no provider could
        be built. An unchecked verdict always sends: an agent nobody asked to
        gate must behave exactly as it did before this module existed.
        """
        settings = await self._container.growth_profile.get(tenant_id)
        block = settings.get("grounding") or {}
        mode = str(block.get("mode") or MODE_ADVISORY)
        enabled = bool((block.get("agents") or {}).get(agent_key, True))

        if not enabled:
            return Verdict(GROUNDED, mode, checked=False, reason="disabled for this agent")
        draft = (draft or "").strip()
        evidence = (evidence or "").strip()
        if not draft or not evidence:
            # nothing to check against is not the same as a failed check. A mail
            # written from an empty research summary is a different bug, caught
            # by the agent that wrote it, not by the judge
            return Verdict(GROUNDED, mode, checked=False, reason="no evidence to check")

        state, reason = await self._judge(tenant_id, agent_key, draft, evidence)
        verdict = Verdict(state, mode, checked=True, reason=reason)
        if state != GROUNDED:
            logger.info(
                "growth_grounding_verdict",
                tenant_id=str(tenant_id),
                agent_key=agent_key,
                state=state,
                mode=mode,
                may_send=verdict.may_send,
            )
        return verdict

    async def _judge(
        self, tenant_id: UUID, agent_key: str, draft: str, evidence: str
    ) -> tuple[str, str]:
        """One utility-tier yes/no call. Never raises."""
        from app.adapters.llm.base import Message

        try:
            llm = await self._container.agent_models.llm_for(
                tenant_id, agent_key, "utility"
            )
        except Exception:
            logger.warning("growth_grounding_no_provider", agent_key=agent_key, exc_info=True)
            return UNKNOWN, "no verifier available"
        if llm is None:
            return UNKNOWN, "no verifier available"

        prompt = _PROMPT.format(evidence=evidence[:MAX_EVIDENCE_CHARS], draft=draft)
        try:
            completion = await asyncio.wait_for(
                llm.complete(
                    [
                        Message(
                            role="system",
                            content="You check drafts against sources.",
                        ),
                        Message(role="user", content=prompt),
                    ],
                    max_tokens=JUDGE_MAX_TOKENS,
                ),
                timeout=JUDGE_TIMEOUT_SECONDS,
            )
        except Exception:
            # the same fail-open-to-unknown as runner._verify. The distinction
            # between "said no" and "never answered" is the whole reason this
            # returns three states instead of a bool
            logger.warning(
                "growth_grounding_check_failed", agent_key=agent_key, exc_info=True
            )
            return UNKNOWN, "the verifier did not answer"

        await self._meter(tenant_id, agent_key, completion)
        answer = (completion.text or "").strip().lower()
        if answer.startswith("no"):
            return UNGROUNDED, "the verifier found an unsupported claim"
        if answer.startswith("yes"):
            return GROUNDED, ""
        # neither word: the judge did not answer the question that was asked, so
        # this is an unknown rather than a pass
        return UNKNOWN, f"unreadable verdict {answer[:32]!r}"

    async def _meter(self, tenant_id: UUID, agent_key: str, completion: Any) -> None:
        """The judge's own tokens are the tenant's cost too.

        Before this was recorded, every verification was billed by the provider
        and invisible in the breakdown — the same gap ``runner._verify`` closed
        by returning its token counts.

        The feature comes from the agent that asked, not from a constant: this
        one helper serves every growth surface, so a fixed tag would file a
        report's verification cost under outbound.
        """
        from app.services.growth.metering import meter_llm

        try:
            await meter_llm(
                tenant_id,
                completion.usage,
                feature=AGENT_FEATURES.get(agent_key, DEFAULT_FEATURE),
                agent_key=f"{agent_key}:grounding",
                model=getattr(completion, "model", ""),
            )
        except Exception:
            logger.debug("growth_grounding_meter_failed", exc_info=True)
