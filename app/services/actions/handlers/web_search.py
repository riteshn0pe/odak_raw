"""`web_search` — the prefetch capability, re-expressed as a tool the model calls.

Same provider, same breaker, same degrade-to-empty contract as the prefetch path
in `_augment()`: `build_web_search()` returns None when no backend is configured,
and a None provider is not an error. This handler checks it and reports
NOT_CONFIGURED, which is what makes the Actions card read "Not configured"
instead of failing a turn — and it is the reason that contract must not be
rewritten.

Whether the tool is offered at all is `ActionsConfig.web_search_mode`, applied
where the toolset is resolved. By the time a call reaches here the decision is
already made; the only question left is whether a provider exists.
"""

from __future__ import annotations

from typing import Any

from app.adapters.tools.web_search import build_web_search
from app.core.logging import get_logger
from app.domain.actions import (
    ActionContext,
    ActionDefinition,
    ActionOutcome,
)
from app.services.actions.handlers.base import (
    Args,
    Elapsed,
    Handler,
    InvalidArgumentError,
    invalid,
    not_configured,
    settings_for,
    succeeded,
)

logger = get_logger(__name__)

MAX_RESULTS = 5
# The adapter already trims each result to 2000 characters for the prefetch path,
# where web evidence competes with document chunks for the prompt budget. A tool
# result is spent inside an agent loop that may run several iterations, so it is
# trimmed harder here.
MAX_SNIPPET_CHARS = 500


def build(service: Any) -> Handler:
    # One provider per service, built on first use. A TavilyWebSearch owns an
    # httpx client, so building one per call would leak a connection pool per
    # tool call.
    cache: dict[str, Any] = {}

    def provider_for() -> Any:
        if "provider" in cache:
            return cache["provider"]
        provider = getattr(service, "web_search", None)
        if provider is None:
            settings = settings_for(service)
            provider = build_web_search(settings) if settings is not None else None
        cache["provider"] = provider
        return provider

    async def handle(
        *,
        action: ActionDefinition,
        arguments: dict[str, Any],
        ctx: ActionContext,
    ) -> ActionOutcome:
        clock = Elapsed()
        args = Args(arguments, ctx)
        try:
            query = args.text(
                "query", required=True, min_length=3, max_length=200, restore=False
            )
        except InvalidArgumentError as exc:
            return invalid(str(exc), duration_ms=clock.ms)
        if query is None:  # pragma: no cover - required=True already raised
            return invalid("query is required", duration_ms=clock.ms)

        provider = provider_for()
        if provider is None:
            return not_configured(action, "web search is not connected")

        try:
            results = await provider.search(query, max_results=MAX_RESULTS)
        except Exception as exc:
            # The adapter swallows provider failures already; anything reaching
            # here is a programming or configuration fault on our side, and it
            # still must not kill the turn.
            logger.warning("action_web_search_failed", error=str(exc))
            results = []

        items: list[dict[str, Any]] = []
        for result in list(results)[:MAX_RESULTS]:
            url = str(getattr(result, "url", "") or "")
            if not url:
                continue
            items.append(
                {
                    "title": str(getattr(result, "title", "") or url),
                    "url": url,
                    "snippet": str(getattr(result, "content", "") or "")[
                        :MAX_SNIPPET_CHARS
                    ],
                }
            )
        logger.info(
            "action_web_search",
            tenant_id=str(ctx.tenant_id),
            results=len(items),
        )
        if not items:
            return succeeded(
                {
                    "query": query,
                    "results": [],
                    "message": (
                        "The web search returned nothing usable. Tell the visitor "
                        "you could not find current information on that."
                    ),
                },
                duration_ms=clock.ms,
            )
        return succeeded(
            {
                "query": query,
                "results": items,
                "message": (
                    "These came from the public web, not the tenant's documents. "
                    "Say so in your answer."
                ),
            },
            duration_ms=clock.ms,
        )

    return handle
