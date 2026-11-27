"""Toolset resolution and dispatch for the actions engine.

Two things live here, and they are the two halves of one decision: which tools a
turn is allowed to see, and what happens when the model calls one.

`ToolSet.resolve` is where **graceful-off** lives. Every reason an action might
not run — the platform kill switch, the agent toggle, an empty
link set, a definition that no longer validates — collapses
into "the model is not offered that tool". None of them raise, and none of them
change the answer path: an agent with no resolvable actions produces exactly the
answer it produced before actions existed. That is deliberate. A tool the model
can see but cannot use is worse than no tool, because the model narrates the
failure to the visitor.

`ActionService.dispatch` is the `ActionDispatcher` port. It is the only thing the
agent loop imports, so the loop never knows whether a call went out over HTTP,
ran a built-in handler, was handed to the browser, or is parked waiting for a
human to say yes. Every path through it ends in exactly one `action_calls` row.

The service deliberately holds no per-turn state. Everything a decision needs is
on the `ActionContext` the loop builds, so two concurrent turns in the same
conversation cannot see each other's counters through this object.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.core.constants import (
    RESERVED_TOOL_NAMES,
    Channel,
)
from app.core.logging import get_logger
from app.domain.actions import (
    ActionContext,
    ActionDefinition,
    ActionEffect,
    ActionKind,
    ActionOutcome,
    ActionOutcomeCode,
)

logger = get_logger(__name__)


class ToolSet:
    """The tools one turn may use, plus why some are missing.

    A plain object rather than a list of specs because the loop needs three
    things that must not drift apart: the specs to send the provider, the
    definition behind each name to dispatch it, and the prompt clauses that
    explain a withheld capability. Returning only specs pushes the other two into
    the loop, which is how "the model called a tool we removed" happens.
    """

    __slots__ = ("action_ids", "actions", "notes", "specs", "web_search")

    def __init__(
        self,
        *,
        specs: tuple[dict[str, Any], ...] = (),
        actions: Mapping[str, ActionDefinition] | None = None,
        action_ids: Mapping[str, UUID] | None = None,
        notes: tuple[str, ...] = (),
        web_search: Any = None,
    ) -> None:
        self.specs = specs
        self.actions: dict[str, ActionDefinition] = dict(actions or {})
        self.action_ids: dict[str, UUID] = dict(action_ids or {})
        # prompt clauses, appended verbatim to the system prompt by the loop.
        # Telling the model a capability is spent is what stops it from promising
        # the visitor an action it cannot perform.
        self.notes = notes
        # The web search provider, or None when unconfigured. Passed through
        # untouched: build_web_search() returning None is a supported state, not
        # an error, and this class must not convert it into one.
        self.web_search = web_search

    def __bool__(self) -> bool:
        return bool(self.specs)

    def __len__(self) -> int:
        return len(self.specs)

    def get(self, name: str) -> ActionDefinition | None:
        return self.actions.get(name)

    def action_id(self, name: str) -> UUID | None:
        return self.action_ids.get(name)

    @classmethod
    def empty(cls, *, notes: tuple[str, ...] = (), web_search: Any = None) -> ToolSet:
        return cls(notes=notes, web_search=web_search)

    @classmethod
    async def resolve(
        cls,
        *,
        repo: Any,
        settings: Any,
        tenant_id: UUID,
        agent_id: UUID,
        config: Any,
        channel: Channel | str = Channel.API,
        web_search: Any = None,
    ) -> ToolSet:
        """Build the turn's toolset. Never raises, never returns a broken tool.

        The order of the early exits is the order of authority: platform, then
        agent. A tenant cannot enable past the kill switch, and each one is a
        cheap boolean checked before the query that would otherwise hit Postgres
        for nothing.
        """
        tools = getattr(config, "tools", None)
        actions_config = getattr(config, "actions", None)

        if not getattr(settings, "ACTIONS_ENABLED", False):
            return cls.empty(web_search=web_search)
        if tools is None or not getattr(tools, "actions_enabled", False):
            return cls.empty(web_search=web_search)

        channel_value = channel.value if isinstance(channel, Channel) else str(channel)
        try:
            rows = await repo.resolve_for_turn(
                tenant_id, agent_id, channel=channel_value
            )
        except Exception:
            # A Postgres blip must degrade to "no actions this turn", not to a 500
            # on a chat request. The answer path does not depend on this query.
            logger.warning(
                "actions_resolve_failed",
                tenant_id=str(tenant_id),
                agent_id=str(agent_id),
            )
            return cls.empty(web_search=web_search)

        confirm_all = bool(getattr(actions_config, "confirm_all_writes", False))
        identity_all = bool(getattr(actions_config, "require_identity_for_all", False))

        specs: list[dict[str, Any]] = []
        definitions: dict[str, ActionDefinition] = {}
        ids: dict[str, UUID] = {}

        for row in rows:
            name = str(row.get("name", ""))
            if name in RESERVED_TOOL_NAMES:
                # A tenant action named `search_documents` would shadow the built-in
                # of the same name in the provider's tool list, and the provider
                # picks one arbitrarily. The definition validator refuses these on
                # write; this is the guard for rows written before it existed.
                logger.warning(
                    "action_name_reserved", tenant_id=str(tenant_id), name=name
                )
                continue
            try:
                definition = ActionDefinition.model_validate(row["definition"])
            except Exception:
                # One tenant's stale row must not remove every other action from
                # the turn, so this is skipped rather than propagated.
                logger.warning(
                    "action_definition_invalid",
                    tenant_id=str(tenant_id),
                    action_id=str(row.get("id")),
                    name=name,
                )
                continue

            # Agent-level policy is applied by tightening the definition rather
            # than by adding two more parameters to the executor: the gates it
            # already enforces per definition are exactly these two, and a copy
            # keeps the enforcement in one place. Both directions are strictly
            # more restrictive, so no validator invariant can be violated.
            updates: dict[str, Any] = {}
            if confirm_all and definition.effect is ActionEffect.WRITE:
                updates["requires_confirmation"] = True
            if identity_all:
                updates["requires_verified_identity"] = True
            if updates:
                definition = definition.model_copy(update=updates)

            specs.append(definition.tool_spec())
            definitions[definition.name] = definition
            row_id = row.get("id")
            if isinstance(row_id, UUID):
                ids[definition.name] = row_id

        return cls(
            specs=tuple(specs),
            actions=definitions,
            action_ids=ids,
            web_search=web_search,
        )


class ActionService:
    """Dispatch, the ledger, and the durable pauses.

    Implements the `ActionDispatcher` port, so the agent loop imports this and
    never the executor. That indirection is what makes the four kinds
    interchangeable at the call site: `server` goes out over HTTP through the
    security envelope, `system` runs a built-in handler, `client` parks the turn
    for the browser, and `widget` renders without any side effect at all.
    """

    def __init__(
        self,
        *,
        repo: Any,
        executor: Any,
        notifier: Any = None,
        settings: Any,
    ) -> None:
        self._repo = repo
        self._executor = executor
        # Optional by construction. The notifier is only reachable from library
        # handlers (escalation, lead capture); nothing on the dispatch path needs
        # it, so a deployment without one loses a notification, not an action.
        self._notifier = notifier
        self._settings = settings
        self._handlers: dict[str, Any] | None = None

    # --------------------------------------------------------- library handlers

    def _handler_for(self, name: str) -> Any:
        """Look up a built-in handler, importing the library on first use.

        Imported lazily and defensively because `app.services.actions.library` is
        owned elsewhere and may not exist yet in a given checkout. A missing
        library makes system actions report NOT_CONFIGURED; it does not stop the
        module from importing, which would take the whole app down.

        Two shapes are accepted: `build_handlers(service)` for handlers that need
        the notifier or the repo, and a plain `HANDLERS` mapping for ones that do
        not. Each handler is awaited as
        `handler(action=..., arguments=..., ctx=...)` and returns an
        `ActionOutcome`.
        """
        if self._handlers is None:
            handlers: dict[str, Any] = {}
            try:
                library = importlib.import_module("app.services.actions.library")
            except ImportError:
                logger.info("action_library_unavailable")
            else:
                factory = getattr(library, "build_handlers", None)
                if callable(factory):
                    try:
                        handlers = dict(factory(self))
                    except Exception:
                        logger.warning("action_library_factory_failed")
                        handlers = {}
                if not handlers:
                    handlers = dict(getattr(library, "HANDLERS", {}) or {})
            self._handlers = handlers
        return self._handlers.get(name)

    @property
    def notifier(self) -> Any:
        """Read by library handlers. May be None."""
        return self._notifier

    @property
    def repo(self) -> Any:
        """Read by library handlers that persist their own rows (lead capture)."""
        return self._repo

    # ---------------------------------------------------------------- dispatch

    async def dispatch(
        self,
        *,
        action: ActionDefinition,
        arguments: dict[str, Any],
        ctx: ActionContext,
    ) -> ActionOutcome:
        """Run one tool call to a single outcome and one ledger row.

        The signature is the `ActionDispatcher` port's and carries no action id,
        so the row is looked up by name. That lookup is not waste: it is one hit
        on the `(tenant_id, name)` unique index, and it buys three things the
        dispatch cannot do without — the id the per-action breaker and the
        per-action rate limit are keyed on, the id the ledger row needs, and a
        re-read of `enabled` so an action disabled mid-conversation stops working
        on the next call rather than at the end of the turn.
        """
        row = None
        try:
            row = await self._repo.get_by_name(ctx.tenant_id, action.name)
        except Exception:
            logger.warning(
                "action_lookup_failed",
                tenant_id=str(ctx.tenant_id),
                name=action.name,
            )

        action_id: UUID | None = None
        if row is not None:
            candidate = row.get("id")
            if isinstance(candidate, UUID):
                action_id = candidate
            if not row.get("enabled", False):
                return await self._finish(
                    action,
                    ctx,
                    action_id,
                    ActionOutcome.failure(
                        ActionOutcomeCode.DISABLED,
                        f"{action.display_name} is currently unavailable",
                    ),
                )

        if action.kind is ActionKind.SERVER:
            outcome = await self._executor.execute(
                action=action, arguments=arguments, ctx=ctx, action_id=action_id
            )
        elif action.kind is ActionKind.SYSTEM:
            outcome = await self._dispatch_system(action, arguments, ctx, action_id)
        elif action.kind is ActionKind.CLIENT:
            outcome = await self._dispatch_client(action, arguments, ctx, action_id)
        else:
            outcome = await self._dispatch_widget(action, arguments, ctx, action_id)

        return await self._finish(action, ctx, action_id, outcome)

    async def _dispatch_system(
        self,
        action: ActionDefinition,
        arguments: dict[str, Any],
        ctx: ActionContext,
        action_id: UUID | None,
    ) -> ActionOutcome:
        """Built-in handlers still pass every ceiling.

        `escalate_to_human` is exempt from the rate limiter inside
        `check_ceilings`, not here, so the exemption is stated once and cannot
        drift between the two paths.
        """
        blocked: ActionOutcome | None = await self._executor.check_ceilings(
            action=action, ctx=ctx, action_id=action_id
        )
        if blocked is not None:
            return blocked

        handler = self._handler_for(action.system_handler or "")
        if handler is None:
            return ActionOutcome.failure(
                ActionOutcomeCode.NOT_CONFIGURED,
                f"{action.display_name} is not available on this deployment",
            )
        try:
            outcome = await handler(action=action, arguments=arguments, ctx=ctx)
        except Exception as exc:
            # A handler raising is a bug in the handler, not a reason to fail the
            # visitor's whole turn. The model gets a refusal it can talk about.
            logger.warning(
                "action_handler_failed",
                tenant_id=str(ctx.tenant_id),
                handler=action.system_handler,
                error=type(exc).__name__,
            )
            return ActionOutcome.failure(
                ActionOutcomeCode.HTTP_ERROR,
                f"{action.display_name} could not be completed",
            )
        if not isinstance(outcome, ActionOutcome):
            logger.warning(
                "action_handler_bad_return",
                handler=action.system_handler,
                returned=type(outcome).__name__,
            )
            return ActionOutcome.failure(
                ActionOutcomeCode.NOT_CONFIGURED,
                f"{action.display_name} is misconfigured",
            )
        return outcome

    async def _dispatch_client(
        self,
        action: ActionDefinition,
        arguments: dict[str, Any],
        ctx: ActionContext,
        action_id: UUID | None,
    ) -> ActionOutcome:
        """Hand the call to the browser and park the turn.

        The suspension row, not the SSE connection, is the state: the visitor's
        tab can reconnect to a different process after a scale-to-zero and the
        turn still resumes. A conversation is required — there is nowhere to park
        an API call that has no conversation to resume into.
        """
        blocked: ActionOutcome | None = await self._executor.check_ceilings(
            action=action, ctx=ctx, action_id=action_id
        )
        if blocked is not None:
            return blocked
        if ctx.conversation_id is None:
            return ActionOutcome.failure(
                ActionOutcomeCode.CHANNEL_UNAVAILABLE,
                "this action needs an interactive session",
            )
        suspension = await self._suspend(
            action,
            arguments,
            ctx,
            action_id,
            kind="client_action",
            ttl_seconds=int(
                getattr(self._settings, "ACTIONS_CLIENT_SUSPENSION_TTL_SECONDS", 300)
            ),
        )
        if suspension is None:
            return ActionOutcome.failure(
                ActionOutcomeCode.NOT_CONFIGURED,
                f"{action.display_name} could not be started",
            )
        return ActionOutcome(
            ok=False,
            outcome=ActionOutcomeCode.AWAITING_CLIENT,
            suspension_id=suspension,
            render=action.render.model_dump() if action.render else None,
            error="waiting for the visitor's browser",
        )

    async def _dispatch_widget(
        self,
        action: ActionDefinition,
        arguments: dict[str, Any],
        ctx: ActionContext,
        action_id: UUID | None,
    ) -> ActionOutcome:
        """Render a card, a form or a set of replies. No side effect, no network.

        Arguments are merged over the stored render spec so the model can fill a
        label or the options of a reply set, but the `type` and any URL stay the
        tenant's — a model-supplied URL in a rendered button is an open redirect
        the visitor will click.
        """
        blocked: ActionOutcome | None = await self._executor.check_ceilings(
            action=action, ctx=ctx, action_id=action_id
        )
        if blocked is not None:
            return blocked
        if action.render is None:
            return ActionOutcome.failure(
                ActionOutcomeCode.NOT_CONFIGURED,
                f"{action.display_name} has nothing to render",
            )
        render = action.render.model_dump()
        for key in ("label", "options"):
            if arguments.get(key):
                render[key] = arguments[key]
        return ActionOutcome(
            ok=True,
            outcome=ActionOutcomeCode.RENDERED,
            data={"rendered": render["type"]},
            render=render,
        )

    # ------------------------------------------------------------- suspensions

    async def _suspend(
        self,
        action: ActionDefinition,
        arguments: dict[str, Any],
        ctx: ActionContext,
        action_id: UUID | None,
        *,
        kind: str,
        ttl_seconds: int,
    ) -> UUID | None:
        if ctx.conversation_id is None:
            return None
        try:
            row = await self._repo.create_suspension(
                ctx.tenant_id,
                conversation_id=ctx.conversation_id,
                action_name=action.name,
                kind=kind,
                call_id=ctx.call_id or str(action_id or action.name),
                arguments=dict(arguments),
                resume_state={
                    "message_id": str(ctx.message_id) if ctx.message_id else None,
                    "procedure_run_id": (
                        str(ctx.procedure_run_id) if ctx.procedure_run_id else None
                    ),
                    "in_procedure": ctx.in_procedure,
                },
                ttl_seconds=ttl_seconds,
                agent_id=ctx.agent_id,
                action_id=action_id,
            )
        except Exception:
            logger.warning(
                "action_suspension_failed",
                tenant_id=str(ctx.tenant_id),
                name=action.name,
                kind=kind,
            )
            return None
        if row is None:
            return None
        candidate = row.get("id")
        return candidate if isinstance(candidate, UUID) else None

    async def confirm(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        call_id: str,
        *,
        approved: bool,
    ) -> dict[str, Any] | None:
        """Resolve a pending confirmation. Returns the row, or None if it is gone.

        The single-winner UPDATE lives in the repo, so a double click, a retried
        request and the expiry sweeper racing each other all end with exactly one
        of them holding the row — which is the whole reason a write can be
        confirmed at all.
        """
        row: dict[str, Any] | None = await self._repo.resolve_suspension(
            tenant_id,
            conversation_id,
            call_id,
            status="resolved" if approved else "declined",
            result={"approved": approved},
        )
        return row

    async def submit_client_result(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        call_id: str,
        *,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Accept the browser's answer to a client action.

        The result is stored verbatim and never trusted: it is visitor-controlled
        input that the loop feeds back to the model as a tool result, so it is
        subject to the same treatment as anything a visitor types.
        """
        row: dict[str, Any] | None = await self._repo.resolve_suspension(
            tenant_id,
            conversation_id,
            call_id,
            status="resolved",
            result=result,
        )
        return row

    # -------------------------------------------------- ledger and metering

    async def _finish(
        self,
        action: ActionDefinition,
        ctx: ActionContext,
        action_id: UUID | None,
        outcome: ActionOutcome,
    ) -> ActionOutcome:
        """One ledger row and, for a real call, one billed usage event.

        A suspension is not metered when it is created — the call has not happened
        yet, and charging for a confirmation the visitor declines would bill for
        work that was never done. The resumed call is metered when it runs.

        `quota_exempt` is honoured here rather than in the executor because it is
        a billing decision: escalation always works, so charging for it would make
        the exemption meaningless.
        """
        try:
            await self._repo.record_call(
                ctx.tenant_id,
                action_name=action.name,
                status=outcome.ledger_status,
                action_id=action_id,
                agent_id=ctx.agent_id,
                conversation_id=ctx.conversation_id,
                message_id=ctx.message_id,
                procedure_run_id=ctx.procedure_run_id,
                http_status=outcome.http_status,
                duration_ms=outcome.duration_ms,
                request_bytes=outcome.request_bytes or None,
                response_bytes=outcome.bytes_read or None,
                error_code=None if outcome.ok else str(outcome.outcome),
                error_message=outcome.error,
            )
        except Exception:
            # record_call already swallows; this is belt and braces so a ledger
            # failure can never turn a completed side effect into an error the
            # model retries against a live provider.
            logger.warning("action_ledger_unexpected", name=action.name)

        if outcome.ok and not outcome.quota_exempt and not outcome.suspended:
            try:
                await self._repo.record_call_usage(
                    ctx.tenant_id,
                    action_name=action.name,
                    conversation_id=ctx.conversation_id,
                )
            except Exception:
                logger.warning("action_usage_meter_failed", name=action.name)

        logger.info(
            "action_dispatched",
            tenant_id=str(ctx.tenant_id),
            name=action.name,
            kind=str(action.kind),
            effect=str(action.effect),
            outcome=str(outcome.outcome),
            status=outcome.ledger_status,
            duration_ms=outcome.duration_ms,
        )
        return outcome

    async def conversation_counters(
        self, tenant_id: UUID, conversation_id: UUID
    ) -> dict[str, int]:
        """Calls and writes so far in this conversation, for the per-conversation
        ceilings. Failing open here is bounded by the per-conversation ceilings
        re-checked on the next call."""
        try:
            counters: dict[str, int] = await self._repo.calls_in_conversation(
                tenant_id, conversation_id
            )
        except Exception:
            logger.warning(
                "action_counters_read_failed", conversation_id=str(conversation_id)
            )
            return {"calls": 0, "writes": 0}
        return counters

    async def suspend_for_confirmation(
        self,
        action: ActionDefinition,
        arguments: dict[str, Any],
        ctx: ActionContext,
        action_id: UUID | None = None,
    ) -> UUID | None:
        """Park a write until the visitor approves it.

        Called by the loop when a dispatch comes back AWAITING_CONFIRMATION, which
        is the executor's answer for a gated write with no confirmation on the
        context. It is a separate method rather than part of dispatch because the
        loop owns the message it renders alongside it, and one action must not
        produce two ledger rows for one call.
        """
        return await self._suspend(
            action,
            arguments,
            ctx,
            action_id,
            kind="confirmation",
            ttl_seconds=int(
                getattr(self._settings, "ACTIONS_CONFIRMATION_TTL_SECONDS", 900)
            ),
        )
