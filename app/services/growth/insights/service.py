"""The Part B insights agent: one daily pass, four watchers, one digest.

The whole stage exists to answer a question no CRM answers — "what should I do
today, and why" — so the shape here is a funnel, not a feed. Candidates come in
from `watchers.py`, three bars drop most of them, what survives is written and
ranked, and the owner hears about it only when something on the list is worth
interrupting them for.

The bars, in the order they are applied:

1. **Sourced.** No ``source_url``, no row. The database enforces it too, so a
   future caller that skips this service cannot write an unsourced insight.
2. **Actionable against this tenant's own rows.** ``clears_action_bar`` — an
   article summary is dropped here, before the insert, rather than stored and
   ranked low.
3. **New.** ``uid`` is checked against the table before insert *and* the insert
   is ``ON CONFLICT DO NOTHING``, so a re-run the same day writes nothing.

Delivery is silent by default. A day where nothing reaches urgency 2 pushes no
message at all; the panel still shows every row. This is the difference between
a tool an owner opens and a notification an owner mutes.

No HITL: an insight is information plus a suggestion. The money action is the
campaign the owner drafts from it, and that already has its own approval gate.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.services.growth.insights import watchers
from app.services.growth.insights.watchers import Candidate, clears_action_bar

logger = get_logger(__name__)

AGENT_KEY = watchers.AGENT_KEY
FEATURE = watchers.FEATURE
INSIGHTS_JOB = "growth_insights"
DUE_TASK = "growth_insights_due"

MAX_TENANTS_PER_TICK = 200
# The per-tenant budget for a whole pass. own_numbers is milliseconds of SQL;
# the external watchers are the slow legs and each has its own shorter leash in
# watchers.py. A tenant that blows this loses today's external upside and keeps
# tomorrow's run — nothing is left half-written, because the insert is last.
RUN_TIMEOUT_SECONDS = 110.0
DELIVER_TIMEOUT_SECONDS = 30.0

# How many rows one pass may write. Twenty is far above what a real day
# produces; it is here so a competitor who rebuilt their whole site cannot turn
# one run into a hundred rows the owner has to dismiss by hand.
MAX_WRITES_PER_RUN = 20
# What goes on WhatsApp/Telegram. The panel gets everything; chat gets the top
# few, because a chat message with fifteen bullet points is one nobody reads.
DIGEST_MIN_ITEMS = 3
DIGEST_MAX_ITEMS = 5
# Nothing is pushed on a day where the best item is only urgency 1. "Worth
# knowing" belongs in the panel; only "worth acting on this week" earns a ping.
PUSH_URGENCY_FLOOR = 2


class InsightsService:
    """Runs one tenant's daily insights pass and hands the panel its reads."""

    def __init__(self, container: Any) -> None:
        self._c = container

    # ------------------------------------------------------------ cron fan-out

    async def run_due(self) -> dict[str, Any]:
        """One ``growth_insights_due`` pass: enqueue a job per eligible tenant.

        Queued, never run inline. A tenant with no growth settings at all has
        nothing to watch and no profile to judge relevance against, so it is
        skipped and counted — a silent skip is indistinguishable from a broken
        cron when someone asks why they got no insights.
        """
        from app.adapters.postgres.growth_repo import active_tenant_ids

        tenants = await active_tenant_ids(limit=MAX_TENANTS_PER_TICK)
        enqueued = 0
        skipped = 0
        failures: list[str] = []
        for tenant_id in tenants:
            try:
                if not await self._c.growth.settings.has_settings(tenant_id):
                    skipped += 1
                    logger.info(
                        "growth_insights_skipped_no_settings",
                        task=DUE_TASK,
                        tenant_id=str(tenant_id),
                    )
                    continue
                await self._enqueue(tenant_id)
                enqueued += 1
            except Exception:
                failures.append(str(tenant_id))
                logger.warning(
                    "growth_insights_due_failed",
                    task=DUE_TASK,
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
        return {
            "status": "ok",
            "tenants": len(tenants),
            "skipped": skipped,
            "enqueued": enqueued,
            "failed": failures,
        }

    async def _enqueue(self, tenant_id: UUID) -> None:
        from app.adapters.queue.base import Job

        await self._c.queue.enqueue(
            Job(kind=INSIGHTS_JOB, tenant_id=tenant_id, payload={})
        )

    async def request(self, tenant_id: UUID, *, via: str = "panel") -> dict[str, Any]:
        """The panel's "look now" button. Queues, same as the cron does."""
        await self._enqueue(tenant_id)
        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="insights_requested",
            input={"via": via},
            reason=f"on-demand insights run requested via {via}",
        )
        return {"status": "queued"}

    # ----------------------------------------------------------------- the run

    async def run(self, tenant_id: UUID, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """One tenant's daily pass, end to end.

        The timeout wraps gathering only. Whatever the watchers produced before
        the budget ran out is still written and still delivered: a slow
        competitor site should cost a tenant that site's insight, not their own
        numbers.
        """
        _ = payload
        try:
            candidates = await asyncio.wait_for(
                self._gather(tenant_id), timeout=RUN_TIMEOUT_SECONDS
            )
        except TimeoutError:
            logger.warning("growth_insights_run_timeout", tenant_id=str(tenant_id))
            candidates = []
        except Exception:
            logger.warning(
                "growth_insights_run_failed", tenant_id=str(tenant_id), exc_info=True
            )
            return {"status": "failed", "written": 0, "delivered": 0}

        kept, dropped = await self._filter(tenant_id, candidates)
        written = await self._c.growth.insights.create_many(
            tenant_id, [c.as_row() for c in kept[:MAX_WRITES_PER_RUN]]
        )
        delivered = await self._deliver(tenant_id, written)
        await self._note(tenant_id, written, delivered)

        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action="insights_run",
            input={"candidates": len(candidates)},
            output={
                "written": len(written),
                "delivered": delivered,
                "dropped": dropped,
            },
            reason=(
                f"{len(candidates)} candidates, {len(written)} written, "
                f"{delivered} owner notifications"
            ),
        )
        return {
            "status": "ok",
            "candidates": len(candidates),
            "written": len(written),
            "dropped": dropped,
            "delivered": delivered,
        }

    # ------------------------------------------------------- run context

    async def _note(
        self, tenant_id: UUID, written: list[dict[str, Any]], delivered: int
    ) -> None:
        """Append the day's insight digest to the run context.

        One entry, not one per insight: the report agent wants "what the
        watchers flagged today", and the headlines plus the counts are that —
        the rows themselves are already in ``growth_insights``. Guarded, like
        the research agent's note, because the scratchpad must never fail the
        pass that produced the findings.
        """
        from app.services.growth.reports.windows import tenant_today

        try:
            settings = await self._c.growth_profile.get(tenant_id)
            await self._c.growth.run_context.append(
                tenant_id,
                local_date=tenant_today(settings),
                agent_key=AGENT_KEY,
                kind="finding",
                payload={
                    "written": len(written),
                    "delivered": delivered,
                    "headlines": [
                        str(row.get("summary") or "") for row in written[:5]
                    ],
                },
            )
        except Exception:
            logger.warning(
                "growth_insights_note_failed", tenant_id=str(tenant_id), exc_info=True
            )

    async def _gather(self, tenant_id: UUID) -> list[Candidate]:
        """Run the four watchers, own numbers first.

        Each watcher is isolated: a search provider outage or a competitor's
        expired certificate costs that watcher's candidates and nothing else.
        own_numbers runs first and unguarded by any external dependency, which is
        what makes the digest have content on a bad day.
        """
        from app.domain import segments as segment_defs

        settings = await self._c.growth_profile.get(tenant_id)
        profile = dict(settings.get("profile") or {})
        competitors = [str(c) for c in (profile.get("competitors") or [])]
        keywords = [str(k) for k in (profile.get("keywords") or [])]

        try:
            orders = await self._c.growth.orders.recent(tenant_id)
            customer_count = await self._c.growth.customers.count(tenant_id)
        except Exception:
            logger.warning(
                "growth_insights_store_read_failed",
                tenant_id=str(tenant_id),
                exc_info=True,
            )
            orders, customer_count = [], 0
        categories = segment_defs.categories(orders)

        out: list[Candidate] = []
        legs: list[tuple[str, Any]] = [
            ("own_numbers", watchers.own_numbers(self._c, tenant_id, self._panel_base())),
            (
                "competitor_site",
                watchers.competitor_sites(self._c, tenant_id, competitors, categories),
            ),
            (
                "news",
                watchers.news(
                    self._c,
                    tenant_id,
                    competitors=competitors,
                    keywords=keywords,
                    profile=profile,
                    categories=categories,
                    customer_count=customer_count,
                ),
            ),
            (
                "event",
                watchers.events(
                    self._c,
                    tenant_id,
                    categories=categories,
                    customer_count=customer_count,
                    profile=profile,
                ),
            ),
        ]
        for name, coro in legs:
            try:
                out.extend(await coro)
            except Exception:
                logger.warning(
                    "growth_insights_watcher_failed",
                    tenant_id=str(tenant_id),
                    watcher=name,
                    exc_info=True,
                )
        return out

    async def _filter(
        self, tenant_id: UUID, candidates: list[Candidate]
    ) -> tuple[list[Candidate], dict[str, int]]:
        """Apply the three bars and rank what is left.

        Counts what each bar removed rather than only what survived: "nothing
        today" and "eleven things today, all of them unsourced" are very
        different states, and only the second is a bug.
        """
        dropped = {"no_source": 0, "no_action": 0, "duplicate": 0, "same_run": 0}
        staged: list[Candidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate.source_url.strip():
                dropped["no_source"] += 1
                continue
            if not clears_action_bar(candidate):
                dropped["no_action"] += 1
                continue
            if candidate.uid in seen:
                # two watchers landing on the same article or the same page
                dropped["same_run"] += 1
                continue
            seen.add(candidate.uid)
            staged.append(candidate)

        if staged:
            known = await self._c.growth.insights.existing_uids(
                tenant_id, [c.uid for c in staged]
            )
            fresh = [c for c in staged if c.uid not in known]
            dropped["duplicate"] = len(staged) - len(fresh)
            staged = fresh

        staged.sort(key=lambda c: (-c.urgency, -c.confidence, c.type))
        if dropped["no_action"] or dropped["no_source"]:
            logger.info(
                "growth_insights_dropped",
                tenant_id=str(tenant_id),
                **{k: v for k, v in dropped.items() if v},
            )
        return staged, dropped

    # ---------------------------------------------------------------- delivery

    async def _deliver(self, tenant_id: UUID, written: list[dict[str, Any]]) -> int:
        """Push the top few, or push nothing.

        Three gates, all deliberate. Nothing written means nothing to say.
        Nothing at urgency 2 or above means today is a quiet day, and a quiet day
        that still pings is how an owner learns to ignore the channel. Grounding
        runs on the digest text against the rows it was built from, because the
        digest is the only text here a model contributed to.
        """
        if not written:
            return 0
        top = [row for row in written if int(row.get("urgency") or 1) >= PUSH_URGENCY_FLOOR]
        if not top:
            logger.info(
                "growth_insights_quiet_day",
                tenant_id=str(tenant_id),
                written=len(written),
            )
            return 0

        items = [self._line(row) for row in top[:DIGEST_MAX_ITEMS]]
        more = len(written) - len(items)
        summary = (
            f"{len(written)} new insight{'s' if len(written) != 1 else ''} for you today."
        )
        if more > 0:
            items.append(f"+{more} more in the panel: {self._panel_base()}/growth/insights")

        verdict = await self._c.growth_grounding.check(
            tenant_id,
            agent_key=AGENT_KEY,
            draft="\n".join(items),
            evidence="\n".join(
                f"{row.get('summary')} | {row.get('why')} | {row.get('source_url')}"
                for row in written
            ),
        )
        if not verdict.may_send:
            logger.warning(
                "growth_insights_digest_blocked",
                tenant_id=str(tenant_id),
                reason=verdict.reason,
            )
            await self._c.growth.audit.record(
                tenant_id,
                agent=AGENT_KEY,
                action="insights_digest_blocked",
                output=verdict.as_meta(),
                reason=verdict.reason or "grounding refused the digest",
            )
            return 0

        try:
            return int(
                await asyncio.wait_for(
                    self._c.approvals.alert(tenant_id, summary, items),
                    timeout=DELIVER_TIMEOUT_SECONDS,
                )
            )
        except Exception:
            # the rows are already written and visible in the panel; a notifier
            # outage must not lose a day's work
            logger.warning(
                "growth_insights_alert_failed", tenant_id=str(tenant_id), exc_info=True
            )
            return 0

    def _line(self, row: dict[str, Any]) -> str:
        """One digest line: what happened, then what to do about it."""
        mark = "!" * max(1, min(3, int(row.get("urgency") or 1)))
        summary = str(row.get("summary") or "").strip()
        action = str(row.get("suggested_action") or "").strip()
        return f"[{mark}] {summary} -> {action}"[:400]

    def _panel_base(self) -> str:
        settings = self._c.settings
        return str(settings.DASHBOARD_URL or settings.PUBLIC_BASE_URL or "").rstrip("/")

    # ------------------------------------------------------------- panel reads

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        open_only: bool = False,
    ) -> dict[str, Any]:
        rows = await self._c.growth.insights.list(
            tenant_id, limit=limit, offset=offset, open_only=open_only
        )
        return {
            "items": [_public(row) for row in rows],
            "total": await self._c.growth.insights.count(tenant_id, open_only=open_only),
            "open": await self._c.growth.insights.count(tenant_id, open_only=True),
        }

    async def act(
        self, tenant_id: UUID, insight_id: UUID, action: str
    ) -> dict[str, Any] | None:
        """Mark one insight acted on, snoozed or ignored.

        The campaign that "act on it" drafts is created by the campaign service,
        not here — this only records that the owner dealt with the row, so the
        panel's open list shrinks and the same insight is not re-surfaced.
        """
        row = await self._c.growth.insights.set_acted_on(tenant_id, insight_id, action)
        if row is None:
            return None
        await self._c.growth.audit.record(
            tenant_id,
            agent=AGENT_KEY,
            action=f"insight_{action}",
            input={"insight_id": str(insight_id)},
            reason=f"owner marked insight {action}",
        )
        return _public(row)


def _draft(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The composer pre-fill behind the panel's "act on it" button.

    The watcher already chose the audience against this tenant's rows, so the
    panel deep-links straight into the campaign composer with that choice made —
    no second LLM call, and no re-parsing of English that was written for a
    human to read. The owner still sees the audience count and the offer before
    anything is drafted, which is the difference between a suggestion and a
    campaign that appeared by itself.

    ``None`` for an insight that names no segment. That is not a broken insight;
    it just has no audience the composer could select.
    """
    segment = payload.get("segment")
    if not isinstance(segment, dict) or not segment.get("key"):
        return None
    offer = payload.get("offer_pct")
    return {
        "segment_key": str(segment.get("key")),
        "params": {str(k): str(v) for k, v in dict(segment.get("params") or {}).items()},
        "offer_pct": int(offer) if isinstance(offer, int | float) else None,
        "event_name": str(payload.get("event_name") or ""),
    }


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """One insight as the panel sees it.

    ``uid`` stays server-side: it is a dedup key, not information, and putting a
    hash on screen invites someone to build against it.
    """
    when = row.get("date")
    created = row.get("created_at")
    payload = dict(row.get("action_payload") or {})
    draft = _draft(payload)
    return {
        "id": str(row.get("id")),
        "date": (
            when.isoformat()
            if (when is not None and hasattr(when, "isoformat"))
            else str(when or "")
        ),
        "type": str(row.get("type") or ""),
        "summary": str(row.get("summary") or ""),
        "why": str(row.get("why") or ""),
        "suggested_action": str(row.get("suggested_action") or ""),
        "source_url": str(row.get("source_url") or ""),
        "confidence": int(row.get("confidence") or 1),
        "urgency": int(row.get("urgency") or 1),
        "acted_on": row.get("acted_on") or None,
        # can_draft and draft give the panel everything it needs to decide whether
        # to offer "act on it" and, when the owner clicks it, which query params
        # to pass to the campaign composer — no second API call needed.
        "can_draft": draft is not None,
        "draft": draft,
        "evidence": dict(payload.get("evidence") or {}),
        "created_at": (
            created.isoformat()
            if (created is not None and hasattr(created, "isoformat"))
            else ""
        ),
    }
