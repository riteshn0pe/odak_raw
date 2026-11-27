"""The answer path: question in, grounded answer out.

Written as an explicit async pipeline rather than a graph library. The control
flow here is a short, mostly linear sequence with two early exits, and a state
machine framework would add a dependency and indirection without removing any of
the logic. LangGraph earns its place when a graph branches, loops, or needs
mid-run checkpointing; this does not yet. When tool calling and human-in-the-loop
clarification arrive, revisit — the stage boundaries below are already the nodes.

Stages:
    screen      reject prompt injection and abuse before spending anything
    mask        strip PII before any text reaches a provider
    intent      small-talk gate: pure greetings skip retrieval entirely
    retrieve    hybrid search, tenant-scoped
    assess      confidence gate; refuse rather than guess
    assemble    fit persona, summary, history, evidence into the budget
    generate    stream or complete
    verify      validate citations, check groundedness
    persist     transcript and usage
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.core.constants import Channel, MessageRole
from app.core.logging import get_logger
from app.domain.actions import ActionContext
from app.domain.agent_config import AgentConfig
from app.domain.entities import Citation, WebResult
from app.domain.guardrails import Verdict, needs_crisis_response, screen_input
from app.domain.pii import compile_extra_rules, mask_pii
from app.services.actions.service import ToolSet
from app.services.agent.context import (
    SUMMARY_PROMPT,
    assemble,
    should_summarise,
    summary_input,
)
from app.services.agent.evidence import EVIDENCE_CHAR_BUDGET, Evidence
from app.services.agent.intent import (
    DEFAULT_SMALL_TALK_REPLIES,
    GREETING_LABELS,
    INTENT_MAX_CHARS,
    INTENT_PROMPT,
    REJECT_LABELS,
    classify_intent_reply,
    is_small_talk,
    pick_small_talk_reply,
)
from app.services.agent.loop import (
    LoopBudget,
    LoopResult,
    flagged_payload,
    run_tool_loop,
)
from app.services.retrieval.search import RetrievalService, validate_citations

logger = get_logger(__name__)

CRISIS_FALLBACK = (
    "It sounds like you may be going through something difficult. I'm not able to "
    "help with this here, but people are available who can — please reach out to a "
    "local crisis line or emergency services."
)

BLOCKED_MESSAGE = "I can't help with that request."

# Appended to the system prompt when the corpus looks thin but an action could
# still answer. Without it the model apologises for the documents instead of
# reaching for the tool that has the answer.
WEAK_EVIDENCE_WITH_TOOLS = (
    "The documents may not cover this question. If an available action can "
    "answer it, use that action. If neither the documents nor an action can, "
    "say so plainly."
)

# A screened-out question never reaches the model, so its `reason` is the only
# thing left to escalate on. Only these two map to a trigger: an injection block
# is an attack, and paging a support lead for every probe is how a tenant learns
# to ignore the channel.
TRIGGER_BY_BLOCK_REASON = {
    "crisis": "crisis",
    "blocklist": "blocklist",
}


def _newest_user_message_id(history: list[dict[str, Any]]) -> UUID | None:
    """The id of the most recent visitor message in this turn's history.

    Used only on a resumed turn, where the question has already been written and
    is therefore the newest user row. Returns None when the projection carries no
    id, which keeps every caller's fallback path intact rather than guessing.
    """
    for entry in reversed(history):
        if str(entry.get("role", "")).endswith("user"):
            raw = entry.get("id")
            if isinstance(raw, UUID):
                return raw
            if raw:
                try:
                    return UUID(str(raw))
                except (TypeError, ValueError):
                    return None
            return None
    return None


def _considered_refs(citations: list[Citation]) -> tuple[Any, ...]:
    """The sources this answer was built from, as escalation receipts.

    Read through `getattr` because a citation from a web result and one from a
    document chunk do not carry the same attributes, and an escalation that
    raises on a missing page number is worse than one with a page number of zero.
    """
    from app.domain.escalation import SourceRef

    refs: list[Any] = []
    for citation in citations[:10]:
        refs.append(
            SourceRef(
                filename=str(getattr(citation, "filename", "") or ""),
                page_from=int(getattr(citation, "page_from", 0) or 0),
                score=float(getattr(citation, "score", 0.0) or 0.0),
                document_id=getattr(citation, "document_id", None),
            )
        )
    return tuple(refs)


@dataclass(slots=True)
class AnswerResult:
    answer: str
    citations: list[Citation] = field(default_factory=list)
    confidence: float = 0.0
    grounded: bool = True
    blocked: bool = False
    reason: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    latency_ms: int = 0
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    # one entry per dispatched action: name, ok, outcome, duration_ms,
    # status_code, source. Surfaced so an API caller can tell what was done.
    actions: list[dict[str, Any]] = field(default_factory=list)
    # "client_action" when the turn ended waiting for the caller's browser, so a
    # non-streaming caller can tell a complete answer from a paused one
    awaiting: str = ""
    # The groundedness verifier's own outcome, kept separate from `grounded`.
    # None means no verdict exists: the check was off, or the verifier itself
    # failed. `grounded` degrades to True in that case so an outage never blocks
    # an answer, which is exactly why a caller that must *require* a verdict —
    # the email channel's `email_require_grounded` gate — has to read this field
    # instead. A failed verifier is not a pass.
    verified: bool | None = None
    # set when this turn converged on an escalation, so a caller can tell the
    # visitor a person has been notified
    escalated: bool = False
    escalation_id: UUID | None = None
    # the live procedure run this turn belonged to, if any
    procedure_run_id: UUID | None = None


class AgentRunner:
    def __init__(
        self,
        retrieval: RetrievalService,
        llm: Any,
        conversations: Any = None,
        documents: Any = None,
        telemetry: Any = None,
        web_search: Any = None,
        llm_resolver: Any = None,
        actions: Any = None,
        procedures: Any = None,
        identity: Any = None,
        escalation: Any = None,
        utility_llm: Any = None,
        cache: Any = None,
        settings: Any = None,
        agent_prompts: Any = None,
    ) -> None:
        self._retrieval = retrieval
        self._llm = llm
        self._conversations = conversations
        self._documents = documents
        self._telemetry = telemetry
        self._web_search = web_search
        # resolves the per-agent provider from config.model; when absent the
        # runner uses the shared managed llm, its original behaviour
        self._llm_resolver = llm_resolver
        # cheaper provider for utility calls (groundedness, summary, small-talk
        # fallback, intent classifier). None means the shared managed llm, so
        # every utility call behaves exactly as it did before this parameter.
        self._utility_llm = utility_llm
        # CachePort | None, for the exact-match answer cache. None disables.
        self._cache = cache
        # Settings | None; read lazily so an unpersisted runner (tests) works.
        self._settings = settings
        # ActionService | None. None disables the feature entirely, following the
        # web_search=None graceful-off precedent above: every downstream branch
        # collapses to an empty toolset and the answer path is byte-for-byte what
        # it was before actions existed.
        self._actions = actions
        # ProcedureService | None. None disables procedures the same way
        # actions=None disables actions: `_procedure_runtime()` returns None, no
        # reserved tool is declared, and the answer path is byte-for-byte what it
        # was before procedures existed.
        self._procedures = procedures
        # built once per runner from `procedures`, because it holds no per-turn
        # state: every run's state is reloaded from Postgres each turn
        self._runtime: Any = None
        # IdentityService | None. Supplies the one line of verified identity the
        # system prompt may carry. None, or an anonymous turn, yields "" and the
        # prompt is byte-for-byte what it was before identity existed.
        self._identity = identity
        # EscalationService | None. Without it every trigger below evaluates and
        # then does nothing, which is the state this feature shipped in: the
        # service and its API existed, and no turn ever called them.
        self._escalation = escalation
        # AgentPromptService | None. Renders the tenant-facts block the system
        # prompt carries (who the business is, what it sells, when it answers).
        # None, or a render that fails, yields "" and the prompt is byte-for-byte
        # what it was before the fact base existed — the same fail-open posture
        # as identity above.
        self._agent_prompts = agent_prompts

    def _procedure_runtime(self) -> Any:
        """The procedures runtime for this runner, or None when unavailable.

        Built lazily and cached. The import is local so a tenant with procedures
        off never pays for the module, and a broken import degrades to "no
        procedures" rather than taking down every answer.
        """
        if self._procedures is None:
            return None
        if self._runtime is None:
            try:
                from app.services.procedures.runtime import ProcedureRuntime

                self._runtime = ProcedureRuntime(
                    self._procedures,
                    escalation=self._escalation,
                    telemetry=self._telemetry,
                )
            except Exception:
                logger.warning("procedure_runtime_unavailable", exc_info=True)
                self._procedures = None
                return None
        return self._runtime

    async def _begin_procedure_turn(
        self,
        runtime: Any,
        tenant_id: UUID,
        *,
        agent_id: UUID | None,
        conversation: Any,
        channel: Channel,
        config: AgentConfig,
        toolset: ToolSet,
        question: str,
        contact_id: UUID | None,
        identity_verified: bool,
        external_user_ref: str | None,
    ) -> Any:
        """Load or resume this conversation's procedure run. Never raises."""
        if runtime is None:
            return None
        try:
            return await runtime.begin_turn(
                tenant_id,
                agent_id=agent_id,
                conversation_id=conversation.id if conversation else None,
                channel=channel,
                config=config,
                toolset=toolset,
                question=question,
                contact_id=contact_id,
                identity_verified=identity_verified,
                external_user_ref=external_user_ref,
            )
        except Exception:
            logger.warning(
                "procedure_turn_not_started", tenant_id=str(tenant_id), exc_info=True
            )
            return None

    async def _escalate_if_needed(
        self,
        tenant_id: UUID,
        config: AgentConfig,
        *,
        question: str,
        channel: Channel,
        agent_id: UUID | None,
        conversation: Any,
        result: AnswerResult,
        external_user_ref: str | None = None,
        trigger: str | None = None,
        note: str | None = None,
        page_url: str | None = None,
    ) -> None:
        """The one place a turn decides a person should hear about this.

        Every trigger converges here rather than each calling the escalation
        service itself, because the interesting logic — the cooldown, the hourly
        cap, the dedupe — is per conversation and not per trigger. Two triggers
        firing on one turn would otherwise notify twice for the same failure.

        The trigger is chosen in severity order, not evaluation order: a visitor
        in distress who also happens to have asked for a human is a crisis, and
        labelling that "user_requested" would route it to the wrong queue.

        Never raises. The visitor already has an answer, and losing a notification
        must not cost them the reply.
        """
        if self._escalation is None or not config.escalation.enabled:
            return

        from app.domain.escalation import EscalationTrigger, wants_human

        live = set(config.escalation.triggers)
        chosen = trigger
        if chosen is None:
            if wants_human(question):
                chosen = str(EscalationTrigger.USER_REQUESTED)
            elif (
                result.verified is False
                and config.guardrails.groundedness_mode == "block"
            ):
                # advisory mode deliberately does not escalate: it exists so a
                # tenant can watch the signal before acting on it
                chosen = str(EscalationTrigger.UNGROUNDED)
            elif (
                result.reason == "below_confidence_threshold"
                or result.confidence < config.retrieval.min_confidence
            ):
                chosen = str(EscalationTrigger.LOW_CONFIDENCE)
        if chosen is None or chosen not in live:
            return

        try:
            decision = await self._escalation.maybe_escalate(
                tenant_id=tenant_id,
                config=config,
                trigger=chosen,
                question=question,
                channel=str(channel),
                agent_id=agent_id,
                conversation_id=conversation.id if conversation else None,
                message_id=result.message_id,
                answer=result.answer,
                confidence=result.confidence,
                considered=_considered_refs(result.citations),
                external_user_ref=external_user_ref,
                page_url=page_url,
                note=note,
            )
        except Exception:
            logger.warning(
                "escalation_failed", tenant_id=str(tenant_id), trigger=chosen, exc_info=True
            )
            return

        if getattr(decision, "escalated", False):
            result.escalated = True
            result.escalation_id = getattr(decision, "escalation_id", None)
            logger.info(
                "answer_escalated",
                tenant_id=str(tenant_id),
                trigger=chosen,
                escalation_id=str(result.escalation_id or ""),
            )

    async def _finish_procedure_turn(
        self,
        runtime: Any,
        procedure_turn: Any,
        *,
        config: AgentConfig,
        result: AnswerResult,
        external_user_ref: str | None,
        stopped_for: str = "",
    ) -> None:
        """Write the run's deltas back and surface its ids on the result."""
        if runtime is None or procedure_turn is None:
            return
        await runtime.finish_turn(
            procedure_turn,
            config=config,
            answer=result.answer,
            confidence=result.confidence,
            external_user_ref=external_user_ref,
            message_id=result.message_id,
            stopped_for=stopped_for,
        )
        result.procedure_run_id = getattr(procedure_turn, "run_id", None)
        escalation_id = getattr(procedure_turn, "escalation_id", None)
        if escalation_id is not None:
            result.escalated = True
            result.escalation_id = escalation_id

    async def _identity_preamble(
        self,
        tenant_id: UUID,
        contact_id: UUID | None,
        identity_verified: bool,
    ) -> str:
        """The verified visitor's identity line for this turn, or "".

        Gated on `identity_verified` as well as on `contact_id` because a contact
        row on its own is not proof of who is typing — only the session's verified
        flag is. A contacts read that fails costs the visitor nothing: the preamble
        is an aid to the model, never a precondition for answering.
        """
        if self._identity is None or not identity_verified or contact_id is None:
            return ""
        try:
            return str(await self._identity.preamble_for(tenant_id, contact_id))
        except Exception:
            logger.warning("identity_preamble_failed", tenant_id=str(tenant_id))
            return ""

    async def _business_context(self, tenant_id: UUID) -> str:
        """The tenant-facts block for this turn's system prompt, or "".

        The chat path's only bridge to the fact base. Fail-open like the identity
        preamble: a facts read that fails costs the visitor nothing, because the
        grounding rules after it already forbid guessing — the block adds *who*
        to answer as, not the permission to answer without evidence.
        """
        if self._agent_prompts is None:
            return ""
        try:
            return str(await self._agent_prompts.business_context(tenant_id))
        except Exception:
            logger.warning("business_context_failed", tenant_id=str(tenant_id))
            return ""

    async def _resolve_llm(self, tenant_id: UUID, config: AgentConfig) -> Any:
        """Pick the provider for this request from the agent's model config.

        The shared managed llm is the default and the safety net: no resolver, or
        a resolver that raises, leaves generation exactly as it was.
        """
        if self._llm_resolver is None:
            return self._llm
        try:
            return self._llm_resolver.resolve(config.model)
        except Exception:
            logger.warning("llm_resolve_failed", tenant_id=str(tenant_id), exc_info=True)
            return self._llm

    async def _resolve_toolset(
        self,
        tenant_id: UUID,
        agent_id: UUID | None,
        config: AgentConfig,
        channel: Channel,
    ) -> ToolSet:
        """The actions this turn may dispatch, or an empty set.

        Empty for every reason that should degrade rather than fail: no action
        service wired, the agent's switch off, or nothing linked and enabled. An
        empty set leaves the answer path byte-for-byte what it is today, which is
        what makes an actions-off tenant cost nothing.
        """
        if self._actions is None or agent_id is None:
            return ToolSet.empty(web_search=self._web_search)
        if not config.tools.actions_enabled:
            return ToolSet.empty(web_search=self._web_search)

        try:
            from app.core.config import get_settings

            return await ToolSet.resolve(
                repo=self._actions.repo,
                settings=get_settings(),
                tenant_id=tenant_id,
                agent_id=agent_id,
                config=config,
                channel=channel,
                web_search=self._web_search,
            )
        except Exception:
            logger.warning("toolset_resolve_failed", tenant_id=str(tenant_id), exc_info=True)
            return ToolSet.empty(web_search=self._web_search)

    async def _action_context(
        self,
        tenant_id: UUID,
        agent_id: UUID | None,
        conversation_id: UUID | None,
        channel: Channel,
        config: AgentConfig,
        toolset: ToolSet,
        restore: Any,
        external_user_ref: str | None,
        contact_id: UUID | None = None,
        identity_verified: bool = False,
        message_id: UUID | None = None,
    ) -> ActionContext:
        """Everything one dispatch is allowed to read about this turn.

        `restore` is carried because `_mask` replaced the visitor's email with
        `[EMAIL_1]` before the model saw it, and an un-restored `{{input.email}}`
        would make every lookup fail against the tenant's own API. Masking exists
        to keep PII from the model vendor, not from the customer's order system.

        `message_id` is the visitor message this dispatch belongs to, and it is
        passed only on a resumed turn. On a fresh turn the newest user row in
        history is the *previous* question — the current one is not written until
        `_persist` — so filling it from history there would hand the ledger, and
        any resume built from it, an anchor pointing at the wrong message.
        """
        counters = {"calls": 0, "writes": 0}
        if conversation_id is not None:
            try:
                counters = await self._actions.conversation_counters(
                    tenant_id, conversation_id
                )
            except Exception:
                logger.warning(
                    "action_counters_failed", conversation_id=str(conversation_id)
                )

        return ActionContext(
            tenant_id=tenant_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            channel=channel,
            restore=restore,
            external_user_ref=external_user_ref,
            message_id=message_id,
            # the executor resolves `{{contact.*}}` from these two and refuses
            # without both. `contact` itself is deliberately left empty here: it is
            # filled from the stored row inside `check_ceilings`, so nothing the
            # visitor asserted can reach a template hole.
            contact_id=contact_id,
            identity_verified=identity_verified,
            calls_this_conversation=int(counters.get("calls", 0)),
            writes_this_conversation=int(counters.get("writes", 0)),
            max_calls_per_conversation=config.actions.max_calls_per_conversation,
            max_writes_per_conversation=config.actions.max_writes_per_conversation,
        )

    async def _augment(
        self,
        tenant_id: UUID,
        query: str,
        config: AgentConfig,
        retrieval: Any,
        toolset: ToolSet | None = None,
    ) -> Evidence:
        """Build the numbered evidence block and its citation list, folding in web
        results when the agent's web-search tool is enabled.

        Local chunks and web results share one numbering so the model cites both
        with the same ``[n]`` scheme, and the citation list is index-aligned to the
        evidence block so ``validate_citations`` maps every ``[n]`` correctly. A
        tool result appended mid-turn continues the same numbering.

        Web search is best-effort: an outage or a missing provider leaves the
        document-only evidence untouched.
        """
        evidence = Evidence(budget=EVIDENCE_CHAR_BUDGET)
        if toolset:
            # hold back room so a result the model asked for never evicts a
            # document block the model has already been shown
            evidence.reserve_for_tools()

        sources: list[tuple[str, Citation]] = []

        for chunk in retrieval.chunks:
            heading = " > ".join(chunk.heading_path) if chunk.heading_path else chunk.filename
            body = chunk.context_text.strip()
            block = (
                f"source: {chunk.filename}"
                f"{f' | section: {heading}' if heading else ''}"
                f" | page: {chunk.page_from}\n{body}"
            )
            sources.append((
                block,
                Citation(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    filename=chunk.filename,
                    page_from=chunk.page_from,
                    page_to=chunk.page_to,
                    heading_path=chunk.heading_path,
                ),
            ))

        web_used = 0
        # an action-enabled agent that declares web_search searches only when the
        # model decides to, instead of on every question
        owns_web_search = toolset is not None and "web_search" in toolset.actions
        if (
            config.tools.web_search_enabled
            and self._web_search is not None
            and not owns_web_search
        ):
            try:
                results: list[WebResult] = await self._web_search.search(query, max_results=5)
            except Exception:
                # never let a web outage fail an answer that documents can serve
                logger.warning("web_search_error", exc_info=True)
                results = []
            for result in results:
                block = f"source: {result.title} | web: {result.url}\n{result.content.strip()}"
                sources.append((
                    block,
                    Citation(
                        chunk_id=f"web:{result.url}",
                        document_id=UUID(int=0),
                        filename=result.title,
                        page_from=0,
                        page_to=0,
                        heading_path=[],
                        url=result.url,
                    ),
                ))
            web_used = len(results)

        for block, citation in sources:
            if evidence.append_source(block, citation) is None:
                break

        # a web hit is external evidence the confidence gate never scored, so its
        # presence lifts an otherwise-empty local retrieval past the refuse gate
        confidence = retrieval.confidence
        if web_used and confidence < config.retrieval.min_confidence:
            confidence = config.retrieval.min_confidence
        evidence.confidence = confidence
        return evidence

    async def answer(
        self,
        tenant_id: UUID,
        config: AgentConfig,
        question: str,
        agent_id: UUID | None = None,
        conversation_id: UUID | None = None,
        channel: Channel = Channel.API,
        external_user_ref: str | None = None,
        document_ids: list[UUID] | None = None,
        contact_id: UUID | None = None,
        identity_verified: bool = False,
    ) -> AnswerResult:
        started = time.monotonic()

        guard, flagged = self._screen(question, config)
        if guard is not None:
            await self._escalate_if_needed(
                tenant_id,
                config,
                question=question,
                channel=channel,
                agent_id=agent_id,
                conversation=None,
                result=guard,
                external_user_ref=external_user_ref,
                trigger=TRIGGER_BY_BLOCK_REASON.get(guard.reason),
            )
            return guard

        masked, restore = self._mask(question, config)

        conversation = None
        history: list[dict[str, Any]] = []
        summary: str | None = None
        if self._conversations is not None and agent_id is not None:
            conversation = await self._conversations.get_or_create(
                tenant_id, agent_id, channel, external_user_ref, conversation_id
            )
            summary = conversation.summary
            history = await self._conversations.recent_messages(
                tenant_id, conversation.id, limit=config.context.history_turns * 2
            )

        # exact-match answer cache: a repeat of an identical question under an
        # identical configuration skips retrieval and generation entirely. The
        # reply is still persisted so the transcript shows what the visitor saw.
        cache_key = (
            None
            if document_ids
            else self._answer_cache_key(tenant_id, agent_id, config, masked)
        )
        if cache_key:
            cached = await self._answer_cache_get(cache_key)
            if cached is not None:
                self._trace("answer_cache", tenant_id=tenant_id, hit=True)
                cached.conversation_id = conversation.id if conversation else None
                await self._persist(
                    tenant_id, conversation, question, cached, config
                )
                return cached

        # Intent gate. A message that is pure greeting/politeness gets a short
        # reply and never reaches retrieval, the toolset, or a procedure turn —
        # the failure being prevented is the "hey" that came back as a
        # source-summarising clarification essay. Fail-open: any ambiguous
        # message returns False from is_small_talk and flows on. The reply comes
        # from the tenant's presaved bank when one exists (zero LLM cost,
        # rotating by turn count), else the persona-only completion.
        if config.guardrails.small_talk_fast_path and is_small_talk(masked):
            result = self._small_talk_result(config, conversation, started)
            await self._persist(tenant_id, conversation, question, result, config)
            return result

        # Second-stage intent classification, only when there is something to
        # classify: a flagged-but-unproven input, or a short message with no
        # question mark that the lexicon could not judge. One cheap utility-model
        # call decides reject / small-talk / off-topic before retrieval is spent.
        # Fail-open: any error or unparseable reply continues to the normal path.
        if config.guardrails.intent_classifier == "on":
            labelled = await self._classify_intent(
                tenant_id, config, masked, flagged
            )
            if labelled is not None:
                label, intent_tokens_in, intent_tokens_out = labelled
                if label in REJECT_LABELS:
                    result = AnswerResult(
                        answer=config.guardrails.reject_message.strip()
                        or BLOCKED_MESSAGE,
                        blocked=True,
                        reason=f"intent:{label}",
                        tokens_in=intent_tokens_in,
                        tokens_out=intent_tokens_out,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        conversation_id=conversation.id if conversation else None,
                    )
                    await self._persist(
                        tenant_id, conversation, question, result, config
                    )
                    await self._escalate_if_needed(
                        tenant_id,
                        config,
                        question=question,
                        channel=channel,
                        agent_id=agent_id,
                        conversation=conversation,
                        result=result,
                        external_user_ref=external_user_ref,
                    )
                    return result
                if label in GREETING_LABELS:
                    result = self._small_talk_result(
                        config,
                        conversation,
                        started,
                        tokens_in=intent_tokens_in,
                        tokens_out=intent_tokens_out,
                    )
                    await self._persist(
                        tenant_id, conversation, question, result, config
                    )
                    return result
                if label == "off_topic":
                    result = AnswerResult(
                        answer=config.guardrails.fallback_message,
                        confidence=0.0,
                        reason="off_topic",
                        tokens_in=intent_tokens_in,
                        tokens_out=intent_tokens_out,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        conversation_id=conversation.id if conversation else None,
                    )
                    await self._persist(
                        tenant_id, conversation, question, result, config
                    )
                    await self._escalate_if_needed(
                        tenant_id,
                        config,
                        question=question,
                        channel=channel,
                        agent_id=agent_id,
                        conversation=conversation,
                        result=result,
                        external_user_ref=external_user_ref,
                    )
                    return result
                # label == "question": the normal path, unchanged

        toolset = await self._resolve_toolset(
            tenant_id, agent_id, config, channel
        )

        # a live procedure changes three things about this turn: the reserved
        # tools are declared, the system prompt carries one step instead of a
        # catalogue, and the loop's budget is widened for bookkeeping. It never
        # changes retrieval — a procedure is instructions, not evidence.
        runtime = self._procedure_runtime()
        procedure_turn = await self._begin_procedure_turn(
            runtime,
            tenant_id,
            agent_id=agent_id,
            conversation=conversation,
            channel=channel,
            config=config,
            toolset=toolset,
            question=question,
            contact_id=contact_id,
            identity_verified=identity_verified,
            external_user_ref=external_user_ref,
        )
        procedure_block = ""
        if runtime is not None and procedure_turn is not None:
            toolset = runtime.augment_toolset(toolset, procedure_turn)
            procedure_block = runtime.prompt_block(procedure_turn)

        retrieval = await self._retrieval.search(
            tenant_id, masked, config.retrieval, document_ids
        )
        self._trace(
            "retrieval",
            tenant_id=tenant_id,
            chunks=len(retrieval.chunks),
            confidence=retrieval.confidence,
            strategy=retrieval.strategy,
        )

        # assemble evidence, folding in web results when the tool is enabled.
        # web hits are external evidence, so `confidence` here may be lifted above
        # the raw local retrieval score.
        evidence = await self._augment(tenant_id, masked, config, retrieval, toolset)
        confidence = evidence.confidence

        # refuse rather than answer from a weak match. a confident wrong answer
        # is worse than an admitted miss, and it is the failure a tenant notices.
        # A live action, though, is evidence the retriever cannot score, so a weak
        # corpus match must not pre-empt the model's chance to fetch the real
        # answer — without this exemption the whole feature is inert.
        if not toolset and (
            not evidence.has_text or confidence < config.retrieval.min_confidence
        ):
            logger.info(
                "answer_below_confidence",
                tenant_id=str(tenant_id),
                confidence=confidence,
                threshold=config.retrieval.min_confidence,
            )
            result = AnswerResult(
                answer=config.guardrails.fallback_message,
                confidence=confidence,
                reason="below_confidence_threshold",
                latency_ms=int((time.monotonic() - started) * 1000),
                conversation_id=conversation.id if conversation else None,
            )
            await self._persist(tenant_id, conversation, question, result, config)
            # a run that `begin_turn` closed (expired, or out of budget) still has
            # to be written even on the path that never reaches the model
            await self._finish_procedure_turn(
                runtime,
                procedure_turn,
                config=config,
                result=result,
                external_user_ref=external_user_ref,
            )
            await self._escalate_if_needed(
                tenant_id,
                config,
                question=question,
                channel=channel,
                agent_id=agent_id,
                conversation=conversation,
                result=result,
                external_user_ref=external_user_ref,
            )
            return result

        weak = not evidence.has_text or confidence < config.retrieval.min_confidence
        preamble = await self._identity_preamble(
            tenant_id, contact_id, identity_verified
        )
        biz_context = await self._business_context(tenant_id)

        def build_system(current: Evidence) -> str:
            return self._system_for(
                config,
                question=masked,
                history=history,
                evidence=current,
                summary=summary,
                toolset=toolset,
                weak=weak,
                identity_preamble=preamble,
                procedure_block=procedure_block,
                business_context=biz_context,
            )

        context = assemble(
            config,
            question=masked,
            history=history,
            evidence=evidence.prompt_text,
            summary=summary,
            identity_preamble=preamble,
            business_context=biz_context,
        )
        context.system = build_system(evidence)

        llm = await self._resolve_llm(tenant_id, config)

        if toolset:
            loop_result = await self._run_loop(
                llm=llm,
                context=context,
                toolset=toolset,
                evidence=evidence,
                config=config,
                tenant_id=tenant_id,
                agent_id=agent_id,
                conversation_id=conversation.id if conversation else None,
                channel=channel,
                restore=restore,
                external_user_ref=external_user_ref,
                build_system=build_system,
                contact_id=contact_id,
                identity_verified=identity_verified,
                runtime=runtime,
                procedure_turn=procedure_turn,
            )
            raw_text = loop_result.text
            tokens_in = loop_result.usage.input_tokens
            tokens_out = loop_result.usage.output_tokens
            model = loop_result.model or getattr(llm, "model_id", "")
        else:
            completion = await llm.complete(
                self._to_messages(context),
                max_tokens=config.model.max_output_tokens,
            )
            loop_result = LoopResult()
            raw_text = completion.text
            tokens_in = completion.usage.input_tokens
            tokens_out = completion.usage.output_tokens
            model = completion.model

        answer = restore(raw_text)
        kept, invalid = validate_citations(answer, evidence.citations)
        if invalid:
            logger.info("citations_invalid_dropped", indexes=invalid, tenant_id=str(tenant_id))

        grounded = True
        verified: bool | None = None
        # No citations means no evidence this answer could be checked against —
        # retrieval and the toolset both came up empty. Verifying an answer
        # against nothing is a vacuous model call that costs the same seconds as
        # a real check and cannot fail it, so it is skipped.
        if config.guardrails.groundedness_check and evidence.citations:
            verified, verify_in, verify_out = await self._verify(answer, evidence)
            tokens_in += verify_in
            tokens_out += verify_out
            # None means the verifier itself failed. That degrades to grounded,
            # because the answer was produced from retrieved evidence either way
            # and an outage must not turn every reply into the fallback message.
            # `verified` keeps the distinction for callers that must require a
            # verdict rather than assume one.
            grounded = verified is not False
            # blocking after a side effect produces a lie: "I've cancelled your
            # subscription" has no supporting source and never will
            blocking = (
                config.guardrails.groundedness_mode == "block" and not evidence.wrote
            )
            if not grounded and blocking:
                answer = config.guardrails.fallback_message
                kept = []
            elif not grounded and evidence.wrote:
                logger.warning(
                    "groundedness_advisory_after_write", tenant_id=str(tenant_id)
                )

        result = AnswerResult(
            answer=answer,
            citations=kept,
            confidence=confidence,
            grounded=grounded,
            verified=verified,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            conversation_id=conversation.id if conversation else None,
            actions=[record.as_dict() for record in loop_result.calls],
            awaiting="client_action" if loop_result.awaiting is not None else "",
        )
        self._trace(
            "answer",
            tenant_id=tenant_id,
            grounded=grounded,
            citations=len(kept),
            invalid_citations=len(invalid),
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            model=result.model,
            latency_ms=result.latency_ms,
            actions=len(result.actions),
            stopped_for=loop_result.stopped_for,
        )
        await self._persist(tenant_id, conversation, question, result, config)
        # a normal grounded tool-free answer is worth replaying for the next
        # visitor who asks the same thing; a tool loop or a procedure turn
        # produced state that replaying would lie about
        if not toolset and procedure_turn is None:
            await self._answer_cache_store(cache_key, result)
        # the run is written after the message, so `finish_turn` can stamp the
        # assistant message id onto an escalation raised by the procedure itself
        await self._finish_procedure_turn(
            runtime,
            procedure_turn,
            config=config,
            result=result,
            external_user_ref=external_user_ref,
            stopped_for=loop_result.stopped_for,
        )
        await self._escalate_if_needed(
            tenant_id,
            config,
            question=question,
            channel=channel,
            agent_id=agent_id,
            conversation=conversation,
            result=result,
            external_user_ref=external_user_ref,
        )
        return result

    def _system_for(
        self,
        config: AgentConfig,
        *,
        question: str,
        history: list[dict[str, Any]],
        evidence: Evidence,
        summary: str | None,
        toolset: ToolSet,
        weak: bool,
        identity_preamble: str = "",
        procedure_block: str = "",
        business_context: str = "",
    ) -> str:
        """The system prompt for this turn's current evidence.

        Rebuilt rather than patched because the loop can drop the retrieval blocks
        mid-turn, and re-running `assemble` is the only way to keep the prompt and
        the numbering in agreement. The withheld-capability notes come from the
        toolset so the model is never told about an action it cannot call.

        The procedure block goes last, after the toolset notes, because it is the
        most specific instruction in the prompt: when a run is live it names one
        step and the model's job is that step.
        """
        rebuilt = assemble(
            config,
            question=question,
            history=history,
            evidence=evidence.prompt_text,
            summary=summary,
            identity_preamble=identity_preamble,
            business_context=business_context,
        )
        extra: list[str] = []
        if toolset and weak:
            extra.append(WEAK_EVIDENCE_WITH_TOOLS)
        extra.extend(toolset.notes)
        if procedure_block:
            extra.append(procedure_block)
        if not extra:
            return rebuilt.system
        return "\n\n".join([rebuilt.system, *extra])

    async def _run_loop(
        self,
        *,
        llm: Any,
        context: Any,
        toolset: ToolSet,
        evidence: Evidence,
        config: AgentConfig,
        tenant_id: UUID,
        agent_id: UUID | None,
        conversation_id: UUID | None,
        channel: Channel,
        restore: Any,
        external_user_ref: str | None,
        build_system: Any,
        emit: Any = None,
        contact_id: UUID | None = None,
        identity_verified: bool = False,
        message_id: UUID | None = None,
        runtime: Any = None,
        procedure_turn: Any = None,
    ) -> LoopResult:
        """Run the tool loop for one turn. Shared by `answer` and `stream`.

        When a procedure is in play the dispatcher is wrapped and the budget is
        widened. Both are deliberate: the loop is given no knowledge of
        procedures, so the three reserved tools are answered by the wrapper before
        the real dispatcher ever sees them, and the widening is what keeps
        bookkeeping calls from eating the iterations the actual work needs.
        """
        from app.core.config import get_settings

        ctx = await self._action_context(
            tenant_id,
            agent_id,
            conversation_id,
            channel,
            config,
            toolset,
            restore,
            external_user_ref,
            contact_id=contact_id,
            identity_verified=identity_verified,
            message_id=message_id,
        )
        budget = LoopBudget.from_settings(get_settings())
        dispatcher: Any = self._actions
        if runtime is not None and procedure_turn is not None:
            dispatcher = runtime.dispatcher(self._actions, procedure_turn, toolset)
            budget = runtime.widen_budget(budget, procedure_turn)
        return await run_tool_loop(
            llm=llm,
            messages=self._to_messages(context),
            toolset=toolset,
            evidence=evidence,
            dispatcher=dispatcher,
            ctx=ctx,
            budget=budget,
            config=config,
            emit=emit,
            rebuild_system=build_system,
        )

    async def stream(
        self,
        tenant_id: UUID,
        config: AgentConfig,
        question: str,
        agent_id: UUID | None = None,
        conversation_id: UUID | None = None,
        channel: Channel = Channel.API,
        external_user_ref: str | None = None,
        document_ids: list[UUID] | None = None,
        record_question: bool = True,
        resumed: dict[str, Any] | None = None,
        contact_id: UUID | None = None,
        identity_verified: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream the answer as events.

        Citations are emitted before the first token so the UI can render sources
        while the answer is still arriving. Groundedness is checked after the
        stream completes and reported as a trailing event — blocking mode cannot
        retract text already sent, so a flagged answer is marked, not hidden.

        With a dispatchable toolset the turn runs through the tool loop, which is
        request/response rather than token-by-token: `LLMProvider.stream()` takes
        no `tools` and the adapters are out of scope for this change, so the
        streamed shape is preserved by emitting progress events during dispatch
        and the answer as one token event. Citations are re-emitted as a superset
        because tool results add numbers mid-turn.
        """
        started = time.monotonic()

        guard, flagged = self._screen(question, config)
        if guard is not None:
            await self._escalate_if_needed(
                tenant_id,
                config,
                question=question,
                channel=channel,
                agent_id=agent_id,
                conversation=None,
                result=guard,
                external_user_ref=external_user_ref,
                trigger=TRIGGER_BY_BLOCK_REASON.get(guard.reason),
            )
            yield {"type": "blocked", "answer": guard.answer, "reason": guard.reason}
            return

        masked, restore = self._mask(question, config)

        conversation = None
        history: list[dict[str, Any]] = []
        summary: str | None = None
        if self._conversations is not None and agent_id is not None:
            conversation = await self._conversations.get_or_create(
                tenant_id, agent_id, channel, external_user_ref, conversation_id
            )
            summary = conversation.summary
            history = await self._conversations.recent_messages(
                tenant_id, conversation.id, limit=config.context.history_turns * 2
            )
            yield {"type": "conversation", "conversation_id": str(conversation.id)}

        # exact-match answer cache, same rule as answer(): a hit streams as
        # citations + one token + done, which the widget renders identically to
        # a live single-chunk stream. A resumed turn replays a paused action
        # turn and never reads the cache.
        cache_key = None
        if document_ids is None and resumed is None:
            cache_key = self._answer_cache_key(tenant_id, agent_id, config, masked)
        if cache_key:
            cached = await self._answer_cache_get(cache_key)
            if cached is not None:
                self._trace("answer_cache", tenant_id=tenant_id, hit=True)
                cached.conversation_id = conversation.id if conversation else None
                await self._persist(
                    tenant_id, conversation, question, cached, config, record_question
                )
                if cached.citations:
                    yield {
                        "type": "citations",
                        "citations": [
                            _citation_payload(c) for c in cached.citations
                        ],
                        "confidence": cached.confidence,
                    }
                yield {"type": "token", "text": cached.answer}
                yield {
                    "type": "done",
                    "confidence": cached.confidence,
                    "citations": [
                        _citation_payload(c) for c in cached.citations
                    ],
                    "reason": "cache_hit",
                }
                return

        # Intent gate, same as answer(): pure greetings skip retrieval entirely.
        # Presaved bank first (zero LLM cost), else the persona-only completion.
        if config.guardrails.small_talk_fast_path and is_small_talk(masked):
            result = self._small_talk_result(config, conversation, started)
            await self._persist(
                tenant_id, conversation, question, result, config, record_question
            )
            yield {"type": "token", "text": result.answer}
            yield {
                "type": "done",
                "confidence": result.confidence,
                "citations": [],
                "reason": "small_talk",
            }
            return

        # Second-stage intent classification, same rule as answer(): flagged or
        # short-and-unjudgeable input, one cheap utility call, fail-open.
        if config.guardrails.intent_classifier == "on":
            labelled = await self._classify_intent(tenant_id, config, masked, flagged)
            if labelled is not None:
                label, intent_tokens_in, intent_tokens_out = labelled
                if label in REJECT_LABELS:
                    result = AnswerResult(
                        answer=config.guardrails.reject_message.strip()
                        or BLOCKED_MESSAGE,
                        blocked=True,
                        reason=f"intent:{label}",
                        tokens_in=intent_tokens_in,
                        tokens_out=intent_tokens_out,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        conversation_id=conversation.id if conversation else None,
                    )
                    await self._persist(
                        tenant_id, conversation, question, result, config, record_question
                    )
                    await self._escalate_if_needed(
                        tenant_id,
                        config,
                        question=question,
                        channel=channel,
                        agent_id=agent_id,
                        conversation=conversation,
                        result=result,
                        external_user_ref=external_user_ref,
                    )
                    yield {
                        "type": "blocked",
                        "answer": result.answer,
                        "reason": result.reason,
                    }
                    return
                if label in GREETING_LABELS:
                    result = self._small_talk_result(
                        config,
                        conversation,
                        started,
                        tokens_in=intent_tokens_in,
                        tokens_out=intent_tokens_out,
                    )
                    await self._persist(
                        tenant_id, conversation, question, result, config, record_question
                    )
                    yield {"type": "token", "text": result.answer}
                    yield {
                        "type": "done",
                        "confidence": result.confidence,
                        "citations": [],
                        "reason": "small_talk",
                    }
                    return
                if label == "off_topic":
                    result = AnswerResult(
                        answer=config.guardrails.fallback_message,
                        confidence=0.0,
                        reason="off_topic",
                        tokens_in=intent_tokens_in,
                        tokens_out=intent_tokens_out,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        conversation_id=conversation.id if conversation else None,
                    )
                    await self._persist(
                        tenant_id, conversation, question, result, config, record_question
                    )
                    await self._escalate_if_needed(
                        tenant_id,
                        config,
                        question=question,
                        channel=channel,
                        agent_id=agent_id,
                        conversation=conversation,
                        result=result,
                        external_user_ref=external_user_ref,
                    )
                    yield {"type": "token", "text": result.answer}
                    yield {
                        "type": "done",
                        "confidence": 0.0,
                        "citations": [],
                        "reason": "off_topic",
                    }
                    return
                # label == "question": the normal path, unchanged

        toolset = await self._resolve_toolset(
            tenant_id, agent_id, config, channel
        )

        runtime = self._procedure_runtime()
        procedure_turn = await self._begin_procedure_turn(
            runtime,
            tenant_id,
            agent_id=agent_id,
            conversation=conversation,
            channel=channel,
            config=config,
            toolset=toolset,
            question=question,
            contact_id=contact_id,
            identity_verified=identity_verified,
            external_user_ref=external_user_ref,
        )
        procedure_block = ""
        if runtime is not None and procedure_turn is not None:
            toolset = runtime.augment_toolset(toolset, procedure_turn)
            procedure_block = runtime.prompt_block(procedure_turn)

        # the anchor for this turn's dispatches. Only a resumed turn has one:
        # `record_question=False` means the visitor's question is already the
        # newest user row, so it is the right message to attribute a call to.
        anchor_message_id: UUID | None = None
        if not record_question:
            anchor_message_id = _newest_user_message_id(history)

        retrieval = await self._retrieval.search(
            tenant_id, masked, config.retrieval, document_ids
        )

        evidence = await self._augment(tenant_id, masked, config, retrieval, toolset)
        confidence = evidence.confidence

        # a resumed turn carries the browser's answer to a client action. It
        # re-enters as the next numbered source with the same untrusted framing as
        # any third-party text, so a forged result is visible as forgeable data
        # rather than as something the server vouches for.
        if resumed:
            resumed_action = toolset.get(str(resumed.get("action", "")))
            if resumed_action is not None:
                payload = resumed.get("payload")
                body = payload if isinstance(payload, dict) else {}
                evidence.append_tool_result(
                    action=resumed_action,
                    call_id=str(resumed.get("call_id", "")),
                    payload={"ok": "error" not in body, **body},
                    ok="error" not in body,
                    flagged=flagged_payload(body),
                )

        if not toolset and not evidence.tool_sourced and (
            not evidence.has_text or confidence < config.retrieval.min_confidence
        ):
            result = AnswerResult(
                answer=config.guardrails.fallback_message,
                confidence=confidence,
                reason="below_confidence_threshold",
                latency_ms=int((time.monotonic() - started) * 1000),
                conversation_id=conversation.id if conversation else None,
            )
            await self._persist(
                tenant_id, conversation, question, result, config, record_question
            )
            await self._finish_procedure_turn(
                runtime,
                procedure_turn,
                config=config,
                result=result,
                external_user_ref=external_user_ref,
            )
            await self._escalate_if_needed(
                tenant_id,
                config,
                question=question,
                channel=channel,
                agent_id=agent_id,
                conversation=conversation,
                result=result,
                external_user_ref=external_user_ref,
            )
            yield {"type": "token", "text": result.answer}
            yield {"type": "done", "confidence": confidence, "citations": []}
            return

        yield {
            "type": "citations",
            "citations": [_citation_payload(c) for c in evidence.citations],
            "confidence": confidence,
        }

        weak = not evidence.has_text or confidence < config.retrieval.min_confidence
        preamble = await self._identity_preamble(
            tenant_id, contact_id, identity_verified
        )
        biz_context = await self._business_context(tenant_id)

        def build_system(current: Evidence) -> str:
            return self._system_for(
                config,
                question=masked,
                history=history,
                evidence=current,
                summary=summary,
                toolset=toolset,
                weak=weak,
                identity_preamble=preamble,
                procedure_block=procedure_block,
                business_context=biz_context,
            )

        context = assemble(
            config,
            question=masked,
            history=history,
            evidence=evidence.prompt_text,
            summary=summary,
            identity_preamble=preamble,
            business_context=biz_context,
        )
        context.system = build_system(evidence)

        llm = await self._resolve_llm(tenant_id, config)
        loop_result = LoopResult()
        chunks: list[str] = []
        # running totals for this turn; the loop result carries the loop's own
        # usage, and the non-toolset stream branch adds its usage below
        tokens_in = 0
        tokens_out = 0

        if toolset:
            events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

            async def emit(event: dict[str, Any]) -> None:
                await events.put(event)

            task = asyncio.create_task(
                self._run_loop(
                    llm=llm,
                    context=context,
                    toolset=toolset,
                    evidence=evidence,
                    config=config,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    conversation_id=conversation.id if conversation else None,
                    channel=channel,
                    restore=restore,
                    external_user_ref=external_user_ref,
                    build_system=build_system,
                    emit=emit,
                    contact_id=contact_id,
                    identity_verified=identity_verified,
                    message_id=anchor_message_id,
                    runtime=runtime,
                    procedure_turn=procedure_turn,
                )
            )
            try:
                while True:
                    if task.done() and events.empty():
                        break
                    try:
                        event = await asyncio.wait_for(events.get(), timeout=5.0)
                    except TimeoutError:
                        # a dispatch can take ten seconds with nothing flowing;
                        # an intermediary drops a silent connection. A client
                        # ignores event types it does not know.
                        yield {"type": "keepalive"}
                        continue
                    if event is None:
                        break
                    yield event
            finally:
                if not task.done():
                    task.cancel()
            try:
                loop_result = await task
            except asyncio.CancelledError:
                raise
            tokens_in = loop_result.usage.input_tokens
            tokens_out = loop_result.usage.output_tokens
            answer_text = restore(loop_result.text)
            if answer_text:
                chunks.append(answer_text)
                yield {"type": "token", "text": answer_text}
            if loop_result.awaiting is not None:
                yield loop_result.awaiting.as_event()
        else:
            # usage_out collects the adapter's final usage frame; without it a
            # streamed turn recorded zero tokens and was invisible to usage
            # accounting and analytics
            from app.adapters.llm.base import Usage as _Usage

            stream_usage: _Usage | None = _Usage()
            try:
                token_iter = llm.stream(
                    self._to_messages(context),
                    max_tokens=config.model.max_output_tokens,
                    usage_out=stream_usage,
                )
            except TypeError:
                # a provider predating usage_out (a test double, an exotic
                # adapter): keep the old behaviour of zero recorded usage
                token_iter = llm.stream(
                    self._to_messages(context),
                    max_tokens=config.model.max_output_tokens,
                )
                stream_usage = None
            async for piece in token_iter:
                restored = restore(piece)
                chunks.append(restored)
                yield {"type": "token", "text": restored}
            if stream_usage is not None:
                tokens_in += stream_usage.input_tokens
                tokens_out += stream_usage.output_tokens

        answer = "".join(chunks)
        kept, _ = validate_citations(answer, evidence.citations)

        grounded = True
        verified: bool | None = None
        # No citations means no evidence this answer could be checked against —
        # skip the vacuous verify call (see answer()).
        if config.guardrails.groundedness_check and evidence.citations:
            verified, verify_in, verify_out = await self._verify(answer, evidence)
            tokens_in += verify_in
            tokens_out += verify_out
            grounded = verified is not False
            if not grounded and evidence.wrote:
                logger.warning(
                    "groundedness_advisory_after_write", tenant_id=str(tenant_id)
                )

        result = AnswerResult(
            answer=answer,
            citations=kept,
            confidence=confidence,
            grounded=grounded,
            verified=verified,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=loop_result.model or getattr(llm, "model_id", ""),
            latency_ms=int((time.monotonic() - started) * 1000),
            conversation_id=conversation.id if conversation else None,
            actions=[record.as_dict() for record in loop_result.calls],
            awaiting="client_action" if loop_result.awaiting is not None else "",
        )
        await self._persist(
            tenant_id, conversation, question, result, config, record_question
        )
        if not toolset and procedure_turn is None:
            await self._answer_cache_store(cache_key, result)
        await self._finish_procedure_turn(
            runtime,
            procedure_turn,
            config=config,
            result=result,
            external_user_ref=external_user_ref,
            stopped_for=loop_result.stopped_for,
        )
        await self._escalate_if_needed(
            tenant_id,
            config,
            question=question,
            channel=channel,
            agent_id=agent_id,
            conversation=conversation,
            result=result,
            external_user_ref=external_user_ref,
        )
        yield {
            "type": "done",
            # the augmented confidence, not the raw local score: a web hit can
            # lift it, and reporting the unlifted number contradicts the answer
            # the user just received
            "confidence": confidence,
            "grounded": grounded,
            # the superset, because tool results added numbers after the first
            # citations event went out. A streaming client rebuilds the node, so
            # a superset replaces cleanly.
            "citations": [_citation_payload(c) for c in kept],
            "latency_ms": result.latency_ms,
            "actions": result.actions,
            "awaiting": result.awaiting,
            # so a caller can say a person has been notified instead of leaving
            # the visitor to ask twice
            "escalated": result.escalated,
        }

    async def resume_client_action(
        self,
        tenant_id: UUID,
        config: AgentConfig,
        conversation_id: UUID,
        call_id: str,
        *,
        agent_id: UUID | None = None,
        channel: Channel = Channel.API,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Continue a turn that ended waiting on the visitor's browser.

        The service half of `POST /v1/chat/resume`; the route, its ticket
        verification and its rate limits are mounted elsewhere. Everything here
        assumes the ticket has already been verified — this method's own guard is
        the one-shot claim inside `submit_client_result`, which is enforced by the
        database (`rowcount == 0` → the row was already delivered, or never
        existed, and the resume is refused).

        The browser's answer is data, never authority. It re-enters the prompt
        through the same numbered evidence, the same field allowlist and the same
        untrusted framing as any third-party text, and a client action can never
        be `effect="write"`, so a forged result cannot be the record of a change.
        """
        if self._actions is None:
            yield {"type": "error", "error": "actions are not available"}
            return

        payload: dict[str, Any] = (
            {"error": error} if error else (result if result is not None else {})
        )
        row = await self._actions.submit_client_result(
            tenant_id, conversation_id, call_id, result=payload
        )
        if row is None:
            # already delivered, expired, or never dispatched. Refused rather
            # than replayed: one claim per call, enforced in Postgres.
            yield {"type": "error", "error": "this action can no longer be answered"}
            return

        resume_state = row.get("resume_state")
        state = resume_state if isinstance(resume_state, dict) else {}
        question = await self._resumed_question(tenant_id, conversation_id, state, config)
        if not question:
            # the paused turn is no longer recoverable from the transcript, and
            # re-running it on an empty question would put a turn to the model
            # that the visitor never asked
            yield {"type": "error", "error": "this turn can no longer be continued"}
            return

        raw_agent = row.get("agent_id")
        async for event in self.stream(
            tenant_id,
            config,
            question,
            agent_id=agent_id or (raw_agent if isinstance(raw_agent, UUID) else None),
            conversation_id=conversation_id,
            channel=channel,
            # the question was written to the transcript by the turn that
            # paused; writing it again would show it twice in the transcript
            record_question=False,
            resumed={
                "action": str(row.get("action_name") or ""),
                "call_id": call_id,
                "payload": payload,
            },
        ):
            yield event

    async def _resumed_question(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        resume_state: Mapping[str, Any],
        config: AgentConfig,
    ) -> str:
        """Recover the visitor's turn that a suspension paused.

        §4.A 4.1 rebuilds a resumed turn from "history + pending tool_call +
        tool result": the browser posts back a result, never the question, and
        the suspension row stores no question either — `resume_state` holds only
        the message, procedure and in-procedure anchors. So the question is read
        back out of the transcript, where the paused turn wrote it before it
        suspended.

        `resume_state["message_id"]` is the anchor and is matched exactly when
        the history rows carry ids. Without one the last user message is the
        same message by construction: a visitor who types instead of answering
        cancels every pending suspension, so a newer user message means the
        one-shot claim would already have refused this resume.
        """
        if self._conversations is None:
            return ""
        anchor = resume_state.get("message_id")
        anchor_id = str(anchor) if anchor else ""
        window = max(config.context.history_turns, 1) * 2
        try:
            history = await self._conversations.recent_messages(
                tenant_id, conversation_id, limit=window
            )
        except Exception:
            logger.warning(
                "resume_history_unavailable",
                tenant_id=str(tenant_id),
                conversation_id=str(conversation_id),
            )
            return ""
        recovered = ""
        for message in history:
            if str(message.get("role") or "") != MessageRole.USER:
                continue
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            if anchor_id and str(message.get("id") or "") == anchor_id:
                return content
            # chronological, so the last user row standing is the newest
            recovered = content
        return recovered

    def _trace(self, name: str, **attributes: Any) -> None:
        """Record a stage observation.

        Attributes are shapes and scores, never question or answer text — the
        telemetry adapter scrubs content, and not sending it is the stronger
        guarantee. Wrapped because a tracing failure must not fail an answer.
        """
        if self._telemetry is None:
            return
        try:
            with self._telemetry.start_span(name, **attributes):
                pass
        except Exception:
            logger.debug("trace_failed", span=name, exc_info=True)

    def _screen(self, question: str, config: AgentConfig) -> tuple[AnswerResult | None, bool]:
        """Cheap checks first, before anything is spent.

        Returns the refusal result when the turn is blocked, and whether the
        pattern screen flagged the input as suspicious without proving it
        (score 0.5-0.9). A flag is not a verdict: it routes the turn to the
        intent classifier when the agent has one, and is logged either way —
        the flag verdict was previously computed and discarded.

        Crisis routing runs ahead of the injection screen: a first-person
        statement of distress must never be answered with a generic refusal.
        """
        if needs_crisis_response(question):
            return (
                AnswerResult(
                    answer=config.guardrails.crisis_message or CRISIS_FALLBACK,
                    blocked=True,
                    reason="crisis",
                ),
                False,
            )

        # tenant-defined hard stop list, checked before the pattern screen. A
        # match is refused with the tenant's own fallback message, not the
        # generic injection-block copy.
        blocklist = config.guardrails.blocklist
        if blocklist:
            lowered = question.lower()
            for phrase in blocklist:
                needle = phrase.strip().lower()
                if needle and needle in lowered:
                    logger.info("input_blocked", reasons=["blocklist"])
                    return (
                        AnswerResult(
                            answer=config.guardrails.fallback_message,
                            blocked=True,
                            reason="blocklist",
                        ),
                        False,
                    )

        if not config.guardrails.injection_screening:
            return None, False

        screened = screen_input(question)
        if screened.verdict == Verdict.BLOCK:
            logger.info("input_blocked", reasons=screened.reasons)
            return (
                AnswerResult(
                    answer=BLOCKED_MESSAGE, blocked=True, reason=";".join(screened.reasons)
                ),
                False,
            )
        if screened.verdict == Verdict.FLAG:
            logger.info("input_flagged", reasons=screened.reasons)
        return None, screened.verdict == Verdict.FLAG

    def _mask(self, question: str, config: AgentConfig) -> tuple[str, Any]:
        """Mask PII outbound, restore it inbound.

        The restore step matters: a user who asks about their own order number
        should see it in the reply, even though the provider never did.
        """
        if not config.guardrails.pii_masking:
            return question, lambda text: text

        extra = compile_extra_rules(config.guardrails.custom_pii_patterns)
        masked = mask_pii(question, extra_rules=extra)
        if masked.found:
            logger.info("pii_masked", labels=masked.labels)
        return masked.text, masked.restore

    async def _classify_intent(
        self, tenant_id: UUID, config: AgentConfig, masked: str, flagged: bool
    ) -> tuple[str, int, int] | None:
        """One cheap utility-model call labelling the turn's intent.

        Called only when there is a real decision to make: the pattern screen
        flagged the input as suspicious without proving it, or the message is
        short, question-mark-free, and the lexicon could not judge it. Long
        messages are almost always genuine questions and skip the call.

        Returns (label, tokens_in, tokens_out), or None when classification
        did not run or failed — None means "take the normal RAG path", the
        fail-open default for every error mode.
        """
        from app.adapters.llm.base import Message

        short = len(masked) <= INTENT_MAX_CHARS
        if not (flagged or short):
            return None
        if short and "?" in masked:
            # a short question is a question; the call would add latency and
            # label it "question" anyway
            return None

        try:
            completion = await self._utility_llm_or_managed().complete(
                [
                    Message(role="system", content=INTENT_PROMPT),
                    Message(role="user", content=masked[:2000]),
                ],
                max_tokens=16,
            )
        except Exception:
            logger.warning(
                "intent_classify_failed", tenant_id=str(tenant_id), exc_info=True
            )
            return None
        label = classify_intent_reply(completion.text)
        self._trace(
            "intent",
            tenant_id=tenant_id,
            label=label,
            flagged=flagged,
            tokens_in=completion.usage.input_tokens,
        )
        return (
            label,
            completion.usage.input_tokens,
            completion.usage.output_tokens,
        )

    def _utility_llm_or_managed(self) -> Any:
        """The cheaper utility provider when wired, else the shared managed one."""
        return self._utility_llm or self._llm

    # --- exact-match answer cache -------------------------------------------

    def _answer_cache_ttl(self) -> int:
        if self._settings is not None:
            return int(getattr(self._settings, "ANSWER_CACHE_TTL_SECONDS", 0))
        try:
            from app.core.config import get_settings

            return int(get_settings().ANSWER_CACHE_TTL_SECONDS)
        except Exception:
            return 0

    def _answer_cache_key(
        self, tenant_id: UUID, agent_id: UUID | None, config: AgentConfig, masked: str
    ) -> str | None:
        """Cache key for this question under this agent's exact configuration.

        The config fingerprint means any settings change — retrieval thresholds,
        persona, model — lands on a fresh key and never serves answers produced
        under the old configuration. Deliberately exact-match on the (masked)
        question: no embedding similarity, so there is no near-match class of
        wrong cache hits and no cross-question leakage.
        """
        import hashlib
        import json as _json

        if self._cache is None or self._answer_cache_ttl() <= 0:
            return None
        fingerprint = hashlib.sha256(
            _json.dumps(config.model_dump(), sort_keys=True, default=str).encode()
        ).hexdigest()
        digest = hashlib.sha256(masked.encode()).hexdigest()
        return f"t:{tenant_id}:ans:{agent_id or 'default'}:{fingerprint[:16]}:{digest}"

    async def _answer_cache_get(self, key: str | None) -> AnswerResult | None:
        if key is None or self._cache is None:
            return None
        try:
            raw = await self._cache.get(key)
        except Exception:
            logger.warning("answer_cache_read_failed", exc_info=True)
            return None
        if not raw:
            return None
        try:
            import json as _json

            payload = _json.loads(raw)
            citations = [
                Citation(
                    chunk_id=str(c["chunk_id"]),
                    document_id=UUID(str(c["document_id"])),
                    filename=str(c["filename"]),
                    page_from=int(c["page_from"]),
                    page_to=int(c["page_to"]),
                    heading_path=list(c.get("heading_path") or []),
                    url=c.get("url"),
                )
                for c in payload.get("citations") or []
            ]
            return AnswerResult(
                answer=str(payload["answer"]),
                citations=citations,
                confidence=float(payload.get("confidence", 0.0)),
                grounded=bool(payload.get("grounded", True)),
                verified=None,
                model=str(payload.get("model") or ""),
                reason="cache_hit",
            )
        except Exception:
            logger.warning("answer_cache_parse_failed", exc_info=True)
            return None

    async def _answer_cache_store(self, key: str | None, result: AnswerResult) -> None:
        """Store a normal, grounded, tool-free answer.

        Anything that varied per turn — actions taken, a paused turn, an
        ungrounded answer — is never stored, because a cache hit replays the
        answer verbatim.
        """
        if key is None or self._cache is None:
            return
        if result.reason or result.blocked or result.actions or result.awaiting:
            return
        if not result.grounded or result.verified is False:
            return
        try:
            import json as _json

            payload = _json.dumps(
                {
                    "answer": result.answer,
                    "citations": [_citation_payload(c) for c in result.citations],
                    "confidence": result.confidence,
                    "grounded": result.grounded,
                    "model": result.model,
                }
            )
            await self._cache.set(key, payload, ttl_seconds=self._answer_cache_ttl())
        except Exception:
            logger.warning("answer_cache_write_failed", exc_info=True)

    def _small_talk_result(
        self,
        config: AgentConfig,
        conversation: Any,
        started: float,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        model: str = "",
    ) -> AnswerResult:
        """The instant greeting reply: tenant bank when set, else built-ins.

        Deliberately no LLM call. A greeting is the cheapest interaction a
        visitor has and the one most likely to be repeated, so waiting twenty
        seconds on a completion — or failing to a nonsense "not in your
        documents" line — is worse than any wording trade-off. The tenant's
        presaved bank still wins when configured; `DEFAULT_SMALL_TALK_REPLIES`
        is the zero-cost fallback, rotated by the same turn count so consecutive
        greetings in one thread read differently.
        """
        turn = conversation.turn_count if conversation else 0
        banked = (
            pick_small_talk_reply(config.guardrails.small_talk_responses, turn)
            or pick_small_talk_reply(DEFAULT_SMALL_TALK_REPLIES, turn)
        )
        if banked is None:  # unreachable: the built-in bank is non-empty
            banked = DEFAULT_SMALL_TALK_REPLIES[0]
        return AnswerResult(
            answer=banked,
            citations=[],
            confidence=1.0,
            reason="small_talk",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            conversation_id=conversation.id if conversation else None,
        )

    def _to_messages(self, context: Any) -> list[Any]:
        from app.adapters.llm.base import Message

        messages = [Message(role="system", content=context.system)]
        messages.extend(
            Message(role=entry["role"], content=entry["content"])
            for entry in context.as_messages()
        )
        return messages

    async def _verify(self, answer: str, evidence: Evidence) -> tuple[bool | None, int, int]:
        """Ask the model whether its own answer is supported by the sources.

        Returns (verdict, tokens_in, tokens_out). The verdict is True or False
        for a decision, and None when no verdict could be obtained — the
        three-state return is what lets a caller distinguish "the verifier
        said no" from "the verifier never ran": a channel that gates sending
        on groundedness must treat the second as a failure, while the chat
        path treats it as grounded and delivers the answer. Token counts come
        back because this call re-prefills the evidence block; before they
        were recorded, every verification was billed by the provider and
        invisible to usage accounting.

        Runs on the utility tier — an 8-token verdict needs no answer-tier
        model.

        The prompt names live actions explicitly. Without those two clauses the
        check returns a false `no` whenever the answer restates live data the
        model correctly treated as authoritative, or describes something it did.
        """
        from app.adapters.llm.base import Message

        verify_llm = self._utility_llm_or_managed()
        prompt = (
            "Does the ANSWER stay within what the SOURCES support?\n"
            "Sources marked 'live action' are authoritative for the facts they "
            "state, even when no document mentions them.\n"
            "Statements describing an action the assistant performed "
            "(a ticket raised, a booking made) are not claims about the sources; "
            "ignore them.\n"
            "Reply with exactly one word: yes or no.\n\n"
            f"SOURCES:\n{evidence.text}\n\nANSWER:\n{answer}"
        )
        try:
            verdict = await verify_llm.complete(
                [
                    Message(role="system", content="You check answers against sources."),
                    Message(role="user", content=prompt),
                ],
                max_tokens=8,
            )
        except Exception:
            # a verification outage must not fail the request. the answer was
            # already produced from retrieved evidence, so the chat path treats
            # None as grounded; a caller that requires a verdict sees the None
            logger.warning("groundedness_check_failed", exc_info=True)
            return None, 0, 0

        return (
            not verdict.text.strip().lower().startswith("no"),
            verdict.usage.input_tokens,
            verdict.usage.output_tokens,
        )

    async def _persist(
        self,
        tenant_id: UUID,
        conversation: Any,
        question: str,
        result: AnswerResult,
        config: AgentConfig,
        record_question: bool = True,
    ) -> None:
        if self._conversations is None or conversation is None:
            return

        # False on a resumed turn: the visitor's question was written when the
        # turn first ran and paused, and writing it twice would show the same
        # message twice in the transcript
        if record_question:
            await self._conversations.add_message(
                tenant_id, conversation.id, MessageRole.USER, question
            )
        result.message_id = await self._conversations.add_message(
            tenant_id,
            conversation.id,
            MessageRole.ASSISTANT,
            result.answer,
            citations=result.citations,
            confidence=result.confidence,
            latency_ms=result.latency_ms,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            model=result.model,
        )
        await self._conversations.record_query_usage(
            tenant_id, result.tokens_in, result.tokens_out, result.model, conversation.id
        )

        await self._maybe_summarise(tenant_id, conversation, config)

    async def _maybe_summarise(
        self, tenant_id: UUID, conversation: Any, config: AgentConfig
    ) -> None:
        if not config.context.rolling_summary:
            return
        from app.core.constants import SUMMARY_EVERY_N_TURNS

        if not should_summarise(conversation.turn_count + 1, SUMMARY_EVERY_N_TURNS):
            return

        from app.adapters.llm.base import Message

        history = await self._conversations.recent_messages(
            tenant_id, conversation.id, limit=SUMMARY_EVERY_N_TURNS * 2
        )
        try:
            # utility tier: a conversation summary needs no answer-tier quality,
            # and this call already ran on the shared managed provider
            summary = await self._utility_llm_or_managed().complete(
                [
                    Message(role="system", content=SUMMARY_PROMPT),
                    Message(role="user", content=summary_input(history, conversation.summary)),
                ],
                max_tokens=512,
            )
        except Exception:
            # losing a summary costs some long-range context, not correctness
            logger.warning("summary_failed", exc_info=True)
            return

        await self._conversations.set_summary(tenant_id, conversation.id, summary.text.strip())


def _citation_payload(citation: Citation) -> dict[str, Any]:
    return {
        "chunk_id": citation.chunk_id,
        "document_id": str(citation.document_id),
        "filename": citation.filename,
        "page_from": citation.page_from,
        "page_to": citation.page_to,
        "heading_path": citation.heading_path,
        "url": citation.url,
    }
