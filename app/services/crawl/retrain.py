"""Scheduled retrains and crash recovery, driven by the existing `recrawl` cron tick.

Two passes, deliberately in one function, because they answer the same question
from opposite ends: *which sources should a wave be running for right now?*

1. **Due retrains.** Sources whose `auto_retrain_hours` has elapsed. The selector
   is a cross-tenant scan and therefore runs under `admin_scope()` inside the
   repository — the same shape `cron.py:_recrawl` has used since it shipped — and
   every write after it is scoped to one tenant through `tenant_scope`. A
   cross-tenant write path here would defeat RLS, which is the isolation mechanism
   for the whole product, so the boundary is drawn at exactly one SELECT.

2. **Stalled crawls.** Sources sitting in `discovering|fetching|extracting` whose
   row has not moved for `CRAWL_STALL_TTL_SECONDS`. This is what makes a crawl
   self-healing after a serverless worker is killed mid-wave: the frontier is
   durable, so re-enqueuing a wave resumes exactly where the dead one stopped.

Neither pass does any crawling. It selects and enqueues, so the cron tick stays
fast and a tick that overruns cannot stall the whole schedule.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.adapters.postgres.source_repo import SourceRepository
from app.adapters.queue.base import Job
from app.core.logging import get_logger

logger = get_logger(__name__)

# how many sources one tick may retrain across the whole deployment
CRAWL_RETRAIN_SOURCES_PER_TICK = 10
# how many of those one tenant may take. 1 is the starvation fix: a tenant with
# 200 due sources contributes exactly one per tick, so a small tenant's single
# site is never queued behind them
CRAWL_RETRAIN_PER_TENANT = 1
# stalled crawls are cheap to revive (one enqueue each), so this can be larger
CRAWL_STALL_SOURCES_PER_TICK = 20


async def due_sources(
    batch: int = CRAWL_RETRAIN_SOURCES_PER_TICK,
    per_tenant: int = CRAWL_RETRAIN_PER_TENANT,
    *,
    repo: Any = None,
) -> list[dict[str, Any]]:
    """Sources whose auto-retrain interval has elapsed, already claimed.

    The claim matters: the repository stamps `last_checked_at = now()` and sets
    `crawl_state='discovering'` in the selector's own transaction, so a
    double-fired cron cannot hand the same source to two waves.
    """
    repository = repo or SourceRepository()
    rows: list[dict[str, Any]] = await repository.due_sources(
        batch=batch, per_tenant=per_tenant
    )
    return rows


async def stalled_sources(
    ttl_seconds: int, batch: int = CRAWL_STALL_SOURCES_PER_TICK, *, repo: Any = None
) -> list[dict[str, Any]]:
    """Crawls that stopped moving. Not claimed — the wave's own lease does that."""
    repository = repo or SourceRepository()
    rows: list[dict[str, Any]] = await repository.stalled_sources(
        ttl_seconds=ttl_seconds, batch=batch
    )
    return rows


async def enqueue_retrain(container: Any, row: dict[str, Any]) -> None:
    """Enqueue one `crawl_wave` for a selected source.

    The payload is the source id and nothing else. Every other input a wave needs
    — config, origin, frontier, boilerplate — is a row it reads for itself, which
    is what lets a redelivered or delayed job still do the right thing.
    """
    tenant_id = UUID(str(row["tenant_id"]))
    source_id = UUID(str(row["id"]))
    await container.queue.enqueue(
        Job(
            kind="crawl_wave",
            tenant_id=tenant_id,
            payload={"source_id": str(source_id)},
        )
    )


async def _recrawl_sources(container: Any) -> dict[str, Any]:
    """The source-level half of the `recrawl` cron tick.

    Returns `{"candidates": 0}` and touches nothing when a deployment has no
    sources, so adding this pass to the existing tick is invisible until the
    feature is actually used.
    """
    settings = container.settings
    if not settings.CRAWL_ENABLED:
        return {"candidates": 0, "skipped": "crawl_disabled"}

    repo = getattr(container, "source_repo", None) or SourceRepository()

    try:
        due = await due_sources(repo=repo)
    except Exception:
        logger.warning("retrain_select_failed", exc_info=True)
        due = []

    try:
        stalled = await stalled_sources(settings.CRAWL_STALL_TTL_SECONDS, repo=repo)
    except Exception:
        logger.warning("stall_sweep_select_failed", exc_info=True)
        stalled = []

    if not due and not stalled:
        return {"candidates": 0}

    enqueued = 0
    for row in due:
        try:
            await enqueue_retrain(container, row)
            enqueued += 1
        except Exception:
            # a queue failure is not fatal to the tick: the source keeps its
            # claim stamp and the stall sweeper picks it up on a later pass
            logger.warning(
                "retrain_enqueue_failed", source_id=str(row.get("id")), exc_info=True
            )

    revived = 0
    for row in stalled:
        try:
            await enqueue_retrain(container, row)
            revived += 1
        except Exception:
            logger.warning(
                "stall_resume_failed", source_id=str(row.get("id")), exc_info=True
            )

    logger.info(
        "retrain_sources_done",
        candidates=len(due),
        enqueued=enqueued,
        stalled=len(stalled),
        revived=revived,
    )
    return {
        "candidates": len(due),
        "enqueued": enqueued,
        "stalled": len(stalled),
        "revived": revived,
    }
