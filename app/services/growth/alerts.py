"""Owner alerts: what is worth interrupting a person for.

Two layers, kept apart on purpose.

``Facts`` is a plain snapshot of the tenant's log DB — counts and states, no
judgement. The rules are **pure predicates over that snapshot**: each one takes
``Facts`` and returns an ``Alert`` or None, with no database, no clock of its own
and no I/O. That is what makes "should this have fired?" a question you can
answer in a unit test instead of by staring at production.

``AlertService`` does the two impure halves: gather the facts, and hand whatever
fired to the approval fan-out — the same Telegram/WhatsApp/Slack destinations the
owner already configured, because nobody wants to connect Telegram twice.

Alerts are deduplicated by a cache key with a cooldown. An alert that fires every
five minutes is an alert the owner mutes, and a muted channel is worse than no
channel at all.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.logging import get_logger

logger = get_logger(__name__)

# How long one alert kind stays quiet after firing for a tenant. Long enough that
# a dispatcher running every five minutes cannot repeat itself, short enough that
# a problem lasting a working day is mentioned more than once.
COOLDOWN_SECONDS = 6 * 3600

# A day's outbound volume this many times the tenant's own cap is not enthusiasm,
# it is a bug or a compromised key.
USAGE_SPIKE_MULTIPLE = 3


@dataclass(slots=True, frozen=True)
class Facts:
    """Everything the rules are allowed to look at.

    Deliberately a flat snapshot rather than a handle to the repositories: a rule
    that could query would eventually query, and then it would be untestable and
    would run a statement per tenant per tick.
    """

    tenant_id: UUID
    now: datetime
    hot_leads: int = 0
    pending_approvals: int = 0
    oldest_pending_hours: float = 0.0
    sent_today: int = 0
    daily_cap: int = 0
    failed_messages_today: int = 0
    broken_integrations: tuple[str, ...] = ()
    queued_messages: int = 0


@dataclass(slots=True, frozen=True)
class Alert:
    """One thing worth telling the owner."""

    key: str
    summary: str
    items: tuple[str, ...] = field(default=())


def rule_hot_leads(facts: Facts) -> Alert | None:
    """Someone replied and said yes. This is the alert that pays for the rest."""
    if facts.hot_leads <= 0:
        return None
    plural = "lead" if facts.hot_leads == 1 else "leads"
    return Alert(
        key="hot_leads",
        summary=f"{facts.hot_leads} interested {plural} waiting for you.",
        items=("Open the inbox to reply while it is still warm.",),
    )


def rule_approvals_waiting(facts: Facts) -> Alert | None:
    """A decision has been sitting long enough to be at risk of expiring.

    Half the TTL is the threshold rather than the deadline itself: an alert that
    arrives as the batch expires is a notification about a failure, not a chance
    to prevent one.
    """
    if facts.pending_approvals <= 0 or facts.oldest_pending_hours < 12:
        return None
    return Alert(
        key="approvals_waiting",
        summary=(
            f"{facts.pending_approvals} approval request(s) still waiting — "
            f"the oldest is {facts.oldest_pending_hours:.0f} hours old and will "
            "expire held, not sent."
        ),
    )

def rule_integration_broken(facts: Facts) -> Alert | None:
    """A connection went down. Silent for the tenant, fatal for the pipeline."""
    if not facts.broken_integrations:
        return None
    return Alert(
        key="integration_broken",
        summary="One or more connections need attention before work can continue.",
        items=tuple(sorted(facts.broken_integrations)),
    )


def rule_usage_spike(facts: Facts) -> Alert | None:
    """Volume far past what the owner configured.

    The cap is enforced by the scheduler, so passing it means something wrote
    messages outside the scheduler — which is exactly the case worth waking
    somebody for.
    """
    if facts.daily_cap <= 0:
        return None
    if facts.sent_today < facts.daily_cap * USAGE_SPIKE_MULTIPLE:
        return None
    return Alert(
        key="usage_spike",
        summary=(
            f"{facts.sent_today} messages went out today against a daily cap of "
            f"{facts.daily_cap}. Sending has not been stopped — check this now."
        ),
    )


def rule_delivery_failures(facts: Facts) -> Alert | None:
    """A quarter of today's sends failing is a provider or reputation problem."""
    attempted = facts.sent_today + facts.failed_messages_today
    if facts.failed_messages_today < 5 or attempted <= 0:
        return None
    share = facts.failed_messages_today / attempted
    if share < 0.25:
        return None
    return Alert(
        key="delivery_failures",
        summary=(
            f"{facts.failed_messages_today} of {attempted} messages failed today "
            f"({share:.0%}). That is a deliverability problem, not bad luck."
        ),
    )


# Order is the order the owner reads them in: money first, then things that
# block money, then things that are merely wrong.
RULES: tuple[Callable[[Facts], Alert | None], ...] = (
    rule_hot_leads,
    rule_approvals_waiting,
    rule_integration_broken,
    rule_delivery_failures,
    rule_usage_spike,
)


def evaluate(facts: Facts) -> list[Alert]:
    """Every rule that fired, in reading order. Pure."""
    out: list[Alert] = []
    for rule in RULES:
        alert = rule(facts)
        if alert is not None:
            out.append(alert)
    return out


class AlertService:
    """Gathers facts, evaluates the rules, and delivers what fired."""

    def __init__(self, container: Any) -> None:
        self._container = container

    async def run_for(self, tenant_id: UUID) -> dict[str, Any]:
        """One tenant's alert pass. Called from the ``growth_dispatch`` tick."""
        facts = await self.gather(tenant_id)
        alerts = evaluate(facts)
        sent: list[str] = []
        for alert in alerts:
            if not await self._claim(tenant_id, alert.key):
                continue
            try:
                await self._container.approvals.alert(
                    tenant_id, summary=alert.summary, items=list(alert.items)
                )
                sent.append(alert.key)
            except Exception:
                logger.warning(
                    "growth_alert_delivery_failed",
                    tenant_id=str(tenant_id),
                    alert=alert.key,
                    exc_info=True,
                )
        return {"evaluated": len(alerts), "sent": sent}

    async def gather(self, tenant_id: UUID) -> Facts:
        """One query per fact group, all read-only, all tenant-scoped.

        Written as explicit SQL here rather than as repository methods because
        every one of these exists only to feed the rules; a repository method
        with exactly one caller is indirection, not abstraction.
        """
        from sqlalchemy import text

        from app.core.db import tenant_scope

        now = datetime.now(UTC)
        day_start = now - timedelta(hours=24)

        async with tenant_scope(tenant_id) as session:
            hot = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM growth_leads WHERE tenant_id = :tid "
                        "AND status = 'positive' AND updated_at >= :since"
                    ),
                    {"tid": tenant_id, "since": day_start},
                )
            ).scalar_one_or_none()

            approvals = (
                await session.execute(
                    text(
                        "SELECT count(*) AS n, min(created_at) AS oldest "
                        "FROM growth_approvals WHERE tenant_id = :tid "
                        "AND status = 'pending'"
                    ),
                    {"tid": tenant_id},
                )
            ).mappings().first()

            messages = (
                await session.execute(
                    text(
                        "SELECT "
                        "count(*) FILTER (WHERE status NOT IN ('queued','failed','bounced')) "
                        "  AS sent, "
                        "count(*) FILTER (WHERE status IN ('failed','bounced')) AS failed, "
                        "count(*) FILTER (WHERE status = 'queued') AS queued "
                        "FROM growth_messages WHERE tenant_id = :tid "
                        "AND direction = 'out' AND created_at >= :since"
                    ),
                    {"tid": tenant_id, "since": day_start},
                )
            ).mappings().first()

            broken = (
                await session.execute(
                    text(
                        "SELECT provider FROM growth_integrations WHERE tenant_id = :tid "
                        "AND status = 'error' ORDER BY provider"
                    ),
                    {"tid": tenant_id},
                )
            ).scalars().all()

        settings = await self._container.growth_profile.get(tenant_id)
        oldest = approvals["oldest"] if approvals else None
        oldest_hours = (
            (now - oldest).total_seconds() / 3600.0 if oldest is not None else 0.0
        )

        return Facts(
            tenant_id=tenant_id,
            now=now,
            hot_leads=int(hot or 0),
            pending_approvals=int(approvals["n"]) if approvals else 0,
            oldest_pending_hours=oldest_hours,
            sent_today=int(messages["sent"]) if messages else 0,
            failed_messages_today=int(messages["failed"]) if messages else 0,
            queued_messages=int(messages["queued"]) if messages else 0,
            daily_cap=int(settings.get("send_caps", {}).get("daily_cap", 0)),
            broken_integrations=tuple(str(p) for p in broken),
        )

    async def _claim(self, tenant_id: UUID, key: str) -> bool:
        """Cooldown gate. True means this alert may fire now.

        Implemented on the cache rather than a table: a missed cooldown after a
        cache flush costs one duplicate notification, and a table would cost a
        write per rule per tenant per tick forever.
        """
        cache_key = f"t:{tenant_id}:growth:alert:{key}"
        try:
            existing = await self._container.cache.get(cache_key)
            if existing:
                return False
            await self._container.cache.set(
                cache_key, "1", ttl_seconds=COOLDOWN_SECONDS
            )
            return True
        except Exception:
            # a cache outage should not silence a real alert. Duplicates are the
            # safe direction here, unlike everywhere else in this file
            logger.debug("growth_alert_cooldown_unavailable", exc_info=True)
            return True
