"""The owner's Telegram front door to the platform.

The orchestrator agent answers the owner's free-text questions from live
platform data and can invoke the other agents' work — the same reads and the
same buttons the dashboard offers, phrased as a conversation. It is owner-only
by construction: the only caller is the Telegram bot's free-text hook, which
sits behind the sender gate, and no ``conversations`` row is ever written
because that table is the customer-facing channels' memory.

The loop here is deliberately simpler than ``app/services/agent/loop.py``'s:
one system prompt, one user question, at most six tool rounds, one answer.
What it does share is the wire plumbing — ``_WireMessage``, ``_wire_assistant``
and ``_wire_results`` — because those encode API constraints (Anthropic's
single tool_result turn, the verbatim replay of the requesting assistant turn)
that would be silently re-broken by a fresh implementation.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from app.adapters.llm.base import Message
from app.core.logging import get_logger
from app.services.agent.loop import (
    WRAP_UP_PROMPT,
    _parse_tool_calls,
    _wire_assistant,
    _wire_results,
    provider_family,
)
from app.services.agents import orchestrator_tools
from app.services.agents.orchestrator_tools import run_tool

logger = get_logger(__name__)

AGENT_KEY = "orchestrator"

# Six rounds is enough to read two screens and press one button, and few
# enough that a model looping on a failing tool costs the owner one reply
# rather than a runaway turn.
MAX_TOOL_ROUNDS = 6
# Generous ceiling: owner-facing answer with tool history; thinking shares budget.
MAX_TOKENS = 20000

# A tool result is prompt prefill on the next round; the leads board is the
# widest one and it fits well under this.
TOOL_RESULT_MAX_CHARS = 8000

# The honest line the owner gets when the turn itself failed. It names nothing
# specific because the owner cannot act on the specifics — the log can.
FAILED_REPLY = "I could not answer that — the platform logged why."


def chunk_reply(text: str, limit: int = 3500) -> list[str]:
    """Split a reply into Telegram-sized chunks, on line breaks where possible.

    The bot service sends each chunk through the same adapter path it uses for
    its own screens; the split lives here so the length rule travels with the
    agent that produces the replies.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


class OrchestratorAgent:
    """Answers the owner's questions from live data, with tools."""

    def __init__(self, container: Any) -> None:
        self._c = container

    async def answer(self, tenant_id: UUID, chat_id: str, text: str) -> str:
        """One owner question in, one grounded answer out.

        Fail-open at every layer: a failing tool is a result the model explains,
        a failing turn is the honest one-liner, and the audit row is written
        only when there is an answer worth recording.
        """
        question = (text or "").strip()
        if not question:
            return ""
        try:
            reply = await self._turn(tenant_id, question)
        except Exception:
            logger.warning(
                "orchestrator_answer_failed",
                tenant_id=str(tenant_id),
                chat_id=str(chat_id),
                exc_info=True,
            )
            return FAILED_REPLY
        if not reply:
            return FAILED_REPLY
        await self._audit(tenant_id, question, reply)
        return reply

    # ------------------------------------------------------------------ turn

    async def _turn(self, tenant_id: UUID, question: str) -> str:
        system = await self._system_prompt(tenant_id)
        llm = await self._c.agent_models.llm_for(tenant_id, AGENT_KEY, "primary")
        family = provider_family(llm)

        messages: list[Any] = [
            Message(role="system", content=system),
            Message(role="user", content=question),
        ]
        for _ in range(MAX_TOOL_ROUNDS):
            completion = await llm.complete(
                messages,
                tools=list(orchestrator_tools.TOOL_SPECS),
                max_tokens=MAX_TOKENS,
            )
            calls = _parse_tool_calls(completion.tool_calls)
            if not calls:
                return (completion.text or "").strip()
            messages.append(_wire_assistant(family, completion.text, calls))
            results = []
            for call in calls:
                outcome = await run_tool(self._c, tenant_id, call.name, call.arguments)
                body = json.dumps(outcome, default=str)
                if len(body) > TOOL_RESULT_MAX_CHARS:
                    body = body[:TOOL_RESULT_MAX_CHARS] + "…}"
                results.append((call, body, "error" in outcome))
            messages.extend(_wire_results(family, results))

        # Cap reached without a final answer: ask once more without tools, so
        # the owner still gets the best answer the gathered data supports.
        messages.append(Message(role="user", content=WRAP_UP_PROMPT))
        completion = await llm.complete(messages, max_tokens=MAX_TOKENS)
        return (completion.text or "").strip()

    # ------------------------------------------------------- prompt and audit

    async def _system_prompt(self, tenant_id: UUID) -> str:
        """The rendered registry prompt, or the template itself when the facts
        are not filled yet — coldmail's ``_system_prompt`` pattern. A tenant
        mid-onboarding still gets an orchestrator; it just answers without the
        business-specific flavour.

        ``system_for`` is a coroutine: it is awaited *inside* the try, so a
        render that raises :class:`MissingFacts` still falls back to the
        registry template rather than handing the model a coroutine object.
        """
        prompts = getattr(self._c, "agent_prompts", None)
        if prompts is not None:
            try:
                return await prompts.system_for(tenant_id, AGENT_KEY)
            except Exception:
                logger.warning(
                    "agent_prompt_missing_facts", agent_key=AGENT_KEY, tenant_id=str(tenant_id)
                )
        from app.services.agents.registry import BY_KEY

        return BY_KEY[AGENT_KEY].prompt

    async def _audit(self, tenant_id: UUID, question: str, reply: str) -> None:
        """The one observability row this channel writes. Fail-open: a lost
        audit row must not cost the owner their answer."""
        try:
            await self._c.growth.audit.record(
                tenant_id,
                agent=AGENT_KEY,
                action="telegram_answer",
                input={"question": question[:1000]},
                output={"reply": reply[:1000]},
                reason="owner asked in telegram",
            )
        except Exception:
            logger.warning(
                "orchestrator_audit_failed", tenant_id=str(tenant_id), exc_info=True
            )
