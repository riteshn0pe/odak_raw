"""The growth scheduler: who may send, when, and how much.

The ``growth_dispatch`` cron tick asks one question per tenant — *is now allowed,
and is there anything to send?* — and either enqueues a bounded batch or does
nothing. There is no sleeping, no polling loop and no long-running worker: a tick
that is too early simply returns, and the next tick five minutes later re-asks.
That is the whole design, and it is what lets the platform keep scaling to zero.

The decision itself is not made here. ``app/domain/schedule.py`` is pure and
answers it from the tenant's working hours, warmup day, daily cap and what has
already gone out; this module gathers those numbers and acts on the answer.
Separating them is what makes the send-timing rules unit-testable, and send
timing is exactly the kind of rule you otherwise learn is wrong from a
deliverability report three weeks later.

**Fairness** is a Redis lock per ``(tenant, job family)``. One tenant with ten
thousand queued messages cannot monopolise the callback budget, because their
second batch cannot start until their first finishes or the lease expires. A job
that cannot take the lock re-enqueues itself with a bounded, jittered delay and
gives up after a few tries rather than spinning — a parked batch that alerts is
better than a queue full of jobs fighting over one lock.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.core.logging import get_logger
from app.domain.schedule import SendCaps, WorkingHours, next_send_time, spread_gaps
from app.services.growth import locks
from app.services.growth.locks import lock_key

logger = get_logger(__name__)

DISPATCH_TASK = "growth_dispatch"
SEND_BATCH_JOB = "growth_send_batch"
FOLLOW_UP_JOB = "growth_follow_up"
RESEARCH_JOB = "growth_research"
RESULTS_JOB = "growth_campaign_results"

# When fewer than this many leads are waiting, the tick tops the queue up. The
# floor is a floor, not the rule: the real threshold is the tenant's own daily
# cap, so a high-volume tenant keeps a deeper queue. This just stops a tenant
# with a cap of zero or one from never researching at all.
RESEARCH_QUEUE_FLOOR = 10

# How many leads one automatic top-up asks for. Smaller than a hand-pressed
# "Find leads", because an automatic run spends money without anyone watching.
RESEARCH_TOPUP = 15

# One tick's fan-out ceiling. Bounds the work of a single cron callback the way
# BATCH bounds run_rollup: a backlog is drained across ticks, never in one
# unbounded scan.
MAX_TENANTS_PER_TICK = 100

# How many messages one growth_send_batch callback attempts before handing the
# rest to a follow-up job. Well under the daily cap on purpose — the bound here
# is the callback's wall clock, not the tenant's allowance.
SEND_BATCH_SIZE = 25

# Lock lease shape lives in locks.py — the CRM push is the second caller and a
# second copy of the fail-open policy is how the two would drift apart.
MAX_LOCK_RETRIES = 3
RETRY_BASE_SECONDS = 20
RETRY_JITTER_SECONDS = 25


class DispatchService:
    """Decides which tenants may send now, and drains one tenant's queue."""

    def __init__(self, container: Any) -> None:
        self._container = container

    # ------------------------------------------------------------- the tick

    async def tick(self) -> dict[str, Any]:
        """One ``growth_dispatch`` pass across every onboarded tenant.

        Candidate selection under ``admin_scope``, then everything per tenant
        inside that tenant's own ``tenant_scope``, with one try/except each — the
        shape every cron task here uses, because one tenant's broken settings
        must not starve the other ninety-nine.
        """
        from app.adapters.postgres.growth_repo import active_tenant_ids

        tenants = await active_tenant_ids(limit=MAX_TENANTS_PER_TICK)
        enqueued = 0
        waiting = 0
        alerted = 0
        follow_ups = 0
        skipped = 0
        failures: list[str] = []

        for tenant_id in tenants:
            try:
                # the settings row is what "onboarded onto growth" means, and it
                # can only be seen from inside that tenant's own scope
                if not await self._container.growth.settings.has_settings(tenant_id):
                    skipped += 1
                    continue
                result = await self.tick_tenant(tenant_id)
                enqueued += 1 if result.get("enqueued") else 0
                waiting += 1 if result.get("reason") else 0
                alerted += int(result.get("alerts", 0))
                follow_ups += 1 if result.get("follow_up") else 0
            except Exception:
                failures.append(str(tenant_id))
                logger.warning(
                    "growth_dispatch_tenant_failed",
                    task=DISPATCH_TASK,
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )

        return {
            "status": "ok",
            "tenants": len(tenants),
            "skipped": skipped,
            "enqueued": enqueued,
            "waiting": waiting,
            "alerts": alerted,
            "follow_ups": follow_ups,
            "failed": failures,
        }

    async def tick_tenant(self, tenant_id: UUID) -> dict[str, Any]:
        """Evaluate one tenant: alerts always, a send batch only if allowed."""
        alerts = await self._container.growth_alerts.run_for(tenant_id)
        alert_count = len(alerts.get("sent", []))

        now = datetime.now(UTC)
        caps = await self.caps_for(tenant_id, now)
        followed_up = await self._maybe_follow_up(tenant_id, now, caps)
        await self._maybe_research(tenant_id, now, caps)

        if not await self._container.growth.messages.has_queued(tenant_id, now):
            return {
                "enqueued": False,
                "reason": "nothing queued",
                "alerts": alert_count,
                "follow_up": followed_up,
            }

        allowed = next_send_time(now, caps)
        if allowed is None:
            # cap of zero, or no working days at all. An owner mistake, and one
            # the settings screen can show — so it is reported, not retried
            return {
                "enqueued": False,
                "reason": "sending is disabled by this tenant's settings",
                "alerts": alert_count,
                "follow_up": followed_up,
            }
        if allowed > now:
            return {
                "enqueued": False,
                "reason": f"next window at {allowed.isoformat()}",
                "alerts": alert_count,
                "follow_up": followed_up,
            }

        from app.adapters.queue.base import Job

        await self._container.queue.enqueue(
            Job(
                kind=SEND_BATCH_JOB,
                tenant_id=tenant_id,
                payload={"batch_id": str(uuid4()), "remaining": caps.remaining},
            )
        )
        return {
            "enqueued": True,
            "remaining": caps.remaining,
            "alerts": alert_count,
            "follow_up": followed_up,
        }

    async def _maybe_follow_up(
        self, tenant_id: UUID, now: datetime, caps: SendCaps
    ) -> bool:
        """Enqueue this tenant's follow-up pass, at most once per local day.

        The tick runs every five minutes, so enqueueing unconditionally would be
        288 jobs per tenant per day to answer a question whose answer changes
        once. The claim is a cache lease keyed on the tenant's *own* calendar
        date, for the same reason the daily cap is: a follow-up window that rolls
        over at the server's midnight is not a daily window.

        A cache outage lets the job through rather than skipping it. A duplicate
        follow-up pass is idempotent — it reads what is due and enqueues sends
        the cap and the scheduler still gate — whereas a skipped one means a lead
        goes cold, so duplicates are the safe direction here.
        """
        block = (await self._container.growth_profile.get(tenant_id)).get("follow_up", {})
        if not block.get("days") or int(block.get("max_follow_ups", 0)) <= 0:
            return False

        local_date = now.astimezone(caps.hours.zone()).date().isoformat()
        key = f"t:{tenant_id}:growth:followup:{local_date}"
        try:
            if await self._container.cache.get(key):
                return False
            # a day and a bit, so the lease outlives the date it names even if a
            # tenant's zone shifts under it
            await self._container.cache.set(key, "1", ttl_seconds=26 * 3600)
        except Exception:
            logger.debug("growth_follow_up_claim_unavailable", exc_info=True)

        from app.adapters.queue.base import Job

        try:
            await self._container.queue.enqueue(
                Job(kind=FOLLOW_UP_JOB, tenant_id=tenant_id, payload={"day": local_date})
            )
        except Exception:
            logger.warning(
                "growth_follow_up_enqueue_failed",
                task=DISPATCH_TASK,
                tenant_id=str(tenant_id),
                exc_info=True,
            )
            return False
        return True

    async def _maybe_research(
        self, tenant_id: UUID, now: datetime, caps: SendCaps
    ) -> bool:
        """Top the lead queue up when it runs low, at most once a local day.

        "Low" is measured against the tenant's own daily cap rather than a fixed
        number: a tenant sending 200 a day needs a deeper queue than one sending
        ten, and a constant here would either starve the first or drown the
        second in leads they will never mail.

        Claimed on the tenant's local date, exactly like the follow-up pass and
        for the same reason — the tick runs 288 times a day to answer a question
        whose answer changes once. A cache outage lets it through: a duplicate
        research run costs a search and is deduped by ``research_runs``
        freshness, whereas a skipped one means an idle sales day.
        """
        settings = await self._container.growth_profile.get(tenant_id)
        profile = settings.get("profile") or {}
        # "" (owner has not chosen) counts as applicable: only an explicit
        # choice opts out. Never assume B2B or D2C.
        if str(profile.get("tenant_type") or "") not in ("", "A", "both"):
            return False

        counts = await self._container.growth.leads.count_by_status(tenant_id)
        waiting = sum(counts.get(status, 0) for status in ("new", "researched", "queued"))
        if waiting >= max(RESEARCH_QUEUE_FLOOR, caps.effective_cap):
            return False

        local_date = now.astimezone(caps.hours.zone()).date().isoformat()
        key = f"t:{tenant_id}:growth:research:{local_date}"
        try:
            if await self._container.cache.get(key):
                return False
            await self._container.cache.set(key, "1", ttl_seconds=26 * 3600)
        except Exception:
            logger.debug("growth_research_claim_unavailable", exc_info=True)

        from app.adapters.queue.base import Job

        try:
            await self._container.queue.enqueue(
                Job(
                    kind=RESEARCH_JOB,
                    tenant_id=tenant_id,
                    payload={"limit": RESEARCH_TOPUP},
                )
            )
        except Exception:
            logger.warning(
                "growth_research_enqueue_failed",
                task=DISPATCH_TASK,
                tenant_id=str(tenant_id),
                exc_info=True,
            )
            return False
        logger.info(
            "growth_research_topup", tenant_id=str(tenant_id), waiting=waiting
        )
        return True

    async def caps_for(self, tenant_id: UUID, now: datetime) -> SendCaps:
        """The tenant's live sending budget.

        ``sent_today`` is counted against the tenant's own local midnight, not
        the server's: a daily cap that resets at 05:30 local time is not a daily
        cap.
        """
        settings = await self._container.growth_profile.get(tenant_id)
        block = settings.get("send_caps", {})
        hours = WorkingHours.from_settings(block)

        local_midnight = (
            now.astimezone(hours.zone())
            .replace(hour=0, minute=0, second=0, microsecond=0)
        )
        sent = await self._container.growth.messages.count_sent_since(
            tenant_id, local_midnight
        )
        return SendCaps(
            daily_cap=int(block.get("daily_cap", 50)),
            warmup_day=int(block.get("warmup_day", 0)),
            sent_today=sent,
            hours=hours,
        )

    # -------------------------------------------------------------- the job

    async def send_batch(self, tenant_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """Drain one tenant's due messages, bounded by the cap and the lock.

        Returns ``{"status": "partial"}`` and re-enqueues itself when it filled a
        whole batch — the ``DELETE_BATCH`` idiom from ``_delete_source``, for the
        same reason: a tenant with a thousand due messages cannot be served
        inside one callback's timeout.
        """
        attempt = int(payload.get("lock_attempt", 0))
        token = uuid4().hex
        key = lock_key(tenant_id, "send")

        if not await self._acquire(key, token):
            return await self._defer(tenant_id, payload, attempt, reason="lock held")

        try:
            now = datetime.now(UTC)
            caps = await self.caps_for(tenant_id, now)
            allowed = next_send_time(now, caps)
            if allowed is None or allowed > now:
                # the window closed between the tick and this callback, or the
                # cap filled. Nothing is sent and nothing is lost: the rows stay
                # queued and the next tick re-evaluates
                return {
                    "status": "skipped",
                    "reason": "outside the send window or over cap",
                }

            limit = min(SEND_BATCH_SIZE, caps.remaining)
            if limit <= 0:
                return {"status": "skipped", "reason": "daily cap reached"}

            due = await self._container.growth.messages.due_queued(tenant_id, now, limit)
            if not due:
                return {"status": "ok", "sent": 0}

            gaps = spread_gaps(len(due), random.Random())  # noqa: S311 - send jitter, not a secret
            sent, failed = await self._send_all(tenant_id, due, gaps)

            if len(due) == limit:
                await self._requeue(tenant_id, payload)
                return {"status": "partial", "sent": sent, "failed": failed}
            # the queue drained: this is the moment a campaign's numbers can
            # change and nothing else was ever going to recompute them
            await self._enqueue_results(tenant_id, due)
            return {"status": "ok", "sent": sent, "failed": failed}
        finally:
            await self._release(key, token)

    async def _enqueue_results(
        self, tenant_id: UUID, due: list[dict[str, Any]]
    ) -> None:
        """Ask for a results recompute on every campaign in the batch just sent.

        Campaign results used to be computed only when an owner opened the
        panel, so a campaign nobody looked at stayed ``sending`` forever and the
        planner's event scoring read empty history. This is the recompute pass,
        hung off the send spine rather than a new cron schedule: it fires
        exactly when there is something new to count, for exactly the campaigns
        that got a message, and never for a tenant with nothing to send.

        Only on the *draining* batch, not on a partial one — a campaign whose
        queue is still filling has nothing final to say, and its next batch will
        ask again. Bounded by construction: one batch holds at most
        ``SEND_BATCH_SIZE`` rows, so at most that many distinct campaigns.
        """
        from app.adapters.queue.base import Job

        campaign_ids = {
            str(row["campaign_id"]) for row in due if row.get("campaign_id") is not None
        }
        for campaign_id in sorted(campaign_ids):
            try:
                await self._container.queue.enqueue(
                    Job(
                        kind=RESULTS_JOB,
                        tenant_id=tenant_id,
                        payload={"campaign_id": campaign_id},
                    )
                )
            except Exception:
                # bookkeeping about a send that already happened: the next
                # batch (or the panel) recomputes, so this must not fail a send
                logger.warning(
                    "growth_campaign_results_enqueue_failed",
                    tenant_id=str(tenant_id),
                    campaign_id=campaign_id,
                    exc_info=True,
                )

    async def _send_all(
        self, tenant_id: UUID, due: list[dict[str, Any]], gaps: list[int]
    ) -> tuple[int, int]:
        """Hand each due message to its channel sender.

        The ``senders is None`` branch is the graceful-off leg, not a stub: a
        deployment carrying the scheduler without the sender registry defers its
        due rows rather than losing them, and the log line says so. In practice
        the registry is always present now — it is a container property — so
        this is the same kind of guard as the None reranker.
        """
        senders = getattr(self._container, "growth_senders", None)
        if senders is None:
            for row, gap in zip(due, gaps, strict=False):
                await self._container.growth.messages.set_status(
                    tenant_id, UUID(str(row["id"])), "queued"
                )
                _ = gap
            logger.info(
                "growth_send_batch_no_sender",
                tenant_id=str(tenant_id),
                due=len(due),
            )
            return 0, 0

        sent = 0
        failed = 0
        for row, gap in zip(due, gaps, strict=False):
            message_id = UUID(str(row["id"]))
            try:
                sender = senders.for_channel(str(row["channel"]))
                receipt = await sender.send(tenant_id, row)
                # SendReceipt is a dataclass, not a mapping: read the fields.
                # The cost mirror is what the campaign panel sums, and the
                # sender put the same number on the usage_events row.
                await self._container.growth.messages.set_status(
                    tenant_id,
                    message_id,
                    "sent",
                    provider_id=receipt.provider_id,
                    cost_micros=int(receipt.cost_micros),
                )
                sent += 1
            except Exception:
                # one refused recipient must not stop the batch. The row is
                # marked failed so it is not retried forever, and the log line
                # carries the attribution the invariants require
                failed += 1
                logger.warning(
                    "growth_send_failed",
                    tenant_id=str(tenant_id),
                    message_id=str(message_id),
                    channel=row.get("channel"),
                    exc_info=True,
                )
                await self._container.growth.messages.set_status(
                    tenant_id, message_id, "failed"
                )
            _ = gap
        if sent:
            # a message that just left the building is a lead change the CRM
            # push wants to mirror — `queued` rows are excluded from the notes
            # query, so the note can only exist once the row is `sent`. One
            # trigger per lead that had a success, after the loop, so a
            # sender error cannot skip it. on_lead_changed never raises, so a
            # CRM that is down or unconfigured costs the send nothing.
            crm = getattr(self._container, "growth_crm", None)
            if crm is not None:
                for lead_raw in {str(r.get("lead_id")) for r in due if r.get("lead_id")}:
                    await crm.on_lead_changed(tenant_id, UUID(lead_raw))
        return sent, failed

    # --------------------------------------------------------- lock plumbing

    async def _acquire(self, key: str, token: str) -> bool:
        return await locks.acquire(self._container, key, token)

    async def _release(self, key: str, token: str) -> None:
        await locks.release(self._container, key, token)

    async def _defer(
        self, tenant_id: UUID, payload: dict[str, Any], attempt: int, *, reason: str
    ) -> dict[str, Any]:
        """Re-enqueue behind a jittered delay, or park and alert.

        Jittered so two contending callbacks do not wake together and contend
        again; bounded so a permanently stuck lock produces one alert rather than
        an infinite queue of retries.
        """
        if attempt >= MAX_LOCK_RETRIES:
            logger.error(
                "growth_send_batch_parked",
                tenant_id=str(tenant_id),
                attempts=attempt,
                reason=reason,
            )
            await self._container.approvals.alert(
                tenant_id,
                summary=(
                    "Sending is stuck: a batch could not start after "
                    f"{attempt} attempts. Nothing was sent and nothing was lost — "
                    "the messages are still queued."
                ),
                items=(),
            )
            return {"status": "parked", "reason": reason, "attempts": attempt}

        from app.adapters.queue.base import Job

        delay = RETRY_BASE_SECONDS * (attempt + 1) + random.randint(  # noqa: S311 - backoff jitter
            0, RETRY_JITTER_SECONDS
        )
        await self._container.queue.enqueue(
            Job(
                kind=SEND_BATCH_JOB,
                tenant_id=tenant_id,
                payload={**payload, "lock_attempt": attempt + 1},
                delay_seconds=delay,
            )
        )
        return {"status": "deferred", "reason": reason, "retry_in_seconds": delay}

    async def _requeue(self, tenant_id: UUID, payload: dict[str, Any]) -> None:
        from app.adapters.queue.base import Job

        await self._container.queue.enqueue(
            Job(
                kind=SEND_BATCH_JOB,
                tenant_id=tenant_id,
                # the lock attempt counter resets: this is a continuation of work
                # that succeeded, not a retry of work that could not start
                payload={k: v for k, v in payload.items() if k != "lock_attempt"},
            )
        )

    # ------------------------------------------------------------ follow-ups

    async def follow_up(self, tenant_id: UUID) -> dict[str, Any]:
        """The follow-up pass, now that the agent that writes them exists.

        Stage 1 owned the *timing* and reported what would be due; the content
        is ``FollowUpService``'s, so this delegates rather than keeping a second
        copy of the due-lead query. The scheduling half — one claim per tenant
        per local day, in ``_maybe_follow_up`` above — is unchanged.
        """
        result: dict[str, Any] = await self._container.growth_followup.run(tenant_id)
        return result
