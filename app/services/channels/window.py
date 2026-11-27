"""The 24-hour customer service window.

Meta only permits free-form messages within 24 hours of the customer's last
inbound message. Outside that window a free-form send is rejected, and the only
way to reopen the conversation is an approved template.

Enforcing it before the API call rather than reacting to the rejection matters
for two reasons: a rejected send still consumed an answer's tokens, and the
tenant needs to know *why* their reply did not arrive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.domain.channels import JSONValue

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WindowDecision:
    """What the sender is allowed to do right now."""

    open: bool
    last_inbound_at: datetime | None
    template_name: str | None = None
    template_language: str = "en_US"

    @property
    def can_send_free_form(self) -> bool:
        return self.open

    @property
    def can_reopen(self) -> bool:
        """A template is the only way back into a closed conversation."""
        return not self.open and self.template_name is not None


def evaluate_window(
    last_inbound_at: datetime | None,
    service_window_hours: int,
    template_config: dict[str, JSONValue] | None = None,
    now: datetime | None = None,
) -> WindowDecision:
    """Decide whether a free-form reply is permitted.

    A conversation with no recorded inbound message is treated as closed. That is
    the safe default: business-initiated contact always requires a template, and
    assuming otherwise would send something Meta rejects.
    """
    config = template_config or {}
    raw_name = config.get("reopen_template")
    raw_language = config.get("reopen_language")
    template_name = raw_name if isinstance(raw_name, str) and raw_name else None
    language = raw_language if isinstance(raw_language, str) and raw_language else "en_US"

    if last_inbound_at is None:
        return WindowDecision(False, None, template_name, language)

    reference = now or datetime.now(UTC)
    if last_inbound_at.tzinfo is None:
        last_inbound_at = last_inbound_at.replace(tzinfo=UTC)

    deadline = last_inbound_at + timedelta(hours=service_window_hours)
    return WindowDecision(reference < deadline, last_inbound_at, template_name, language)


class ServiceWindow:
    """Reads the last inbound timestamp for a conversation.

    Cached briefly. A window boundary is measured in hours, so a few seconds of
    staleness cannot flip the decision, and the alternative is a database read on
    every outbound message.
    """

    def __init__(self, repo: Any, cache: Any = None, ttl_seconds: int = 30) -> None:
        self._repo = repo
        self._cache = cache
        self._ttl = ttl_seconds

    async def evaluate(
        self,
        tenant_id: UUID,
        integration_id: UUID,
        sender_ref: str,
        service_window_hours: int,
        template_config: dict[str, JSONValue] | None = None,
    ) -> WindowDecision:
        last_inbound = await self._last_inbound_at(tenant_id, integration_id, sender_ref)
        decision = evaluate_window(last_inbound, service_window_hours, template_config)

        if not decision.open:
            logger.info(
                "whatsapp_window_closed",
                tenant_id=str(tenant_id),
                last_inbound_at=last_inbound.isoformat() if last_inbound else None,
                can_reopen=decision.can_reopen,
            )
        return decision

    async def _last_inbound_at(
        self, tenant_id: UUID, integration_id: UUID, sender_ref: str
    ) -> datetime | None:
        cache_key = f"t:{tenant_id}:wa-window:{integration_id}:{sender_ref}"

        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached:
                try:
                    return datetime.fromisoformat(cached)
                except ValueError:
                    await self._cache.delete(cache_key)

        stamp = await self._repo.last_inbound_at(tenant_id, integration_id, sender_ref)

        if stamp is not None and self._cache is not None:
            await self._cache.set(cache_key, stamp.isoformat(), ttl_seconds=self._ttl)
        return stamp

    async def note_inbound(
        self, tenant_id: UUID, integration_id: UUID, sender_ref: str, when: datetime
    ) -> None:
        """Refresh the cache when a message arrives.

        Without this the cached value would keep a just-reopened window looking
        closed for the remainder of the TTL.
        """
        if self._cache is None:
            return
        await self._cache.set(
            f"t:{tenant_id}:wa-window:{integration_id}:{sender_ref}",
            when.isoformat(),
            ttl_seconds=self._ttl,
        )
