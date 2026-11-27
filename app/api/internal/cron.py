"""Scheduled job callback.

An external scheduler (QStash cron, or any cron hitting this URL) fires these on
a cadence. Like the ingest callback, the endpoint is public by necessity, so the
signature is verified before anything runs. Each task is idempotent, because a
scheduler will occasionally double-fire and a missed tick will occasionally be
retried.

Tasks:
  rollup            fold usage_events into usage_rollups, then age-sweep the
                    run-context store (rows older than 7 days)
  checkpoint_expiry drop expired identity replay-ledger rows, close and prune
                    expired procedure runs, then prune the action-call ledger
  email_retention   expire inbound/delivery receipts and resolved escalations
  recrawl           re-fetch URL-sourced documents whose refresh interval elapsed,
                    then sweep sources due for auto-retrain and any crawl left stalled
  growth_dispatch   the growth scheduler: per tenant, decide whether now is inside
                    the send window and under the cap, and enqueue a batch if so
  approval_expiry   flip approval requests nobody decided on to expired, hold the
                    batch, and tell the owner once
  growth_report_due enqueue one growth_report job per tenant whose weekly/monthly
                    report is due; the job builds the PDF and delivers it
  growth_store_sync enqueue one growth_store_sync job per tenant with a
                    connected store; the job pages each store incrementally
  growth_insights_due enqueue one growth_insights job per tenant with growth
                    settings; the job runs the four watchers and delivers the
                    digest only when something reaches urgency 2
  growth_crm_retry  enqueue one growth_crm_push sweep job per tenant with a
                    connected CRM; the sweep retries failed link rows whose
                    backoff has elapsed
  growth_indiamart_poll enqueue one growth_inbound_backfill job (one-day
                    window) per tenant with IndiaMART connected; the job
                    pulls the seller feed and ingests new enquiries
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Request
from sqlalchemy import text

from app.api.deps import ContainerDep
from app.core.db import admin_scope
from app.core.errors import AppError, AuthenticationError
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/internal/cron", tags=["internal"], include_in_schema=False)


@router.post("/{task}")
async def run_cron(
    task: str,
    request: Request,
    container: ContainerDep,
    upstash_signature: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    settings = container.settings
    raw = (await request.body()).decode()

    # Either credential admits the caller: the shared secret (for the
    # non-QStash backends, whose scheduled calls carry nothing else) or a
    # valid Upstash signature (QStash's scheduled pings — they sign, they do
    # not know the token). Rejecting on a missing token alone would lock
    # QStash's scheduler out whenever INTERNAL_TOKEN is set, and every cron
    # task would die as a 401 it keeps retrying. Same shape as the jobs
    # callback's gate.
    token_ok = False
    if settings.INTERNAL_TOKEN:
        import hmac

        got = request.headers.get("x-internal-token") or ""
        token_ok = hmac.compare_digest(got.encode(), settings.INTERNAL_TOKEN.encode())
        if not token_ok and settings.QUEUE_BACKEND != "qstash":
            logger.warning("internal_token_rejected", task=task)
            raise AuthenticationError("invalid internal token")

    if settings.QUEUE_BACKEND == "qstash":
        from app.adapters.queue.qstash import verify_signature

        if not token_ok and not verify_signature(
            upstash_signature or "",
            raw,
            settings.QSTASH_CURRENT_SIGNING_KEY,
            settings.QSTASH_NEXT_SIGNING_KEY,
        ):
            logger.warning("cron_signature_rejected", task=task)
            raise AuthenticationError("invalid cron signature")

    try:
        return await _run_cron_task(task, container)
    except AppError:
        # already carries a code and is logged with attribution by the app-error
        # handler; re-raise without a duplicate line
        raise
    except Exception:
        # any other task raising reaches the generic 500 handler, which has no
        # idea which task failed. name it here before it propagates.
        logger.error("cron_task_failed", task=task, exc_info=True)
        raise


async def _run_cron_task(task: str, container: ContainerDep) -> dict[str, Any]:
    settings = container.settings
    match task:
        case "rollup":
            from app.services.usage.rollup import run_rollup

            result = await run_rollup()
            # The run-context age sweep rides this tick because the rollup is
            # already the "nobody is waiting on this" cron: the context is a
            # scratchpad whose rows are worthless once the day has passed, so
            # the only cleanup it needs is an interval cutoff, never an
            # end-of-run delete (which in a fan-out architecture would erase
            # rows before the jobs that write them have run). Guarded: the
            # rollup is billing data, the sweep is housekeeping, and one must
            # not fail the other.
            try:
                swept: dict[str, int] = await container.growth.run_context.sweep_old()
                result["run_context"] = swept
            except Exception:
                logger.warning("run_context_sweep_failed", task=task, exc_info=True)
            return result
        case "checkpoint_expiry":
            # This slot was a declared no-op while no checkpointer existed. It now
            # expires the identity replay ledger, which is the one table in the
            # system that grows with every verified session and is worthless once
            # the tokens it records have expired.
            #
            # Missing this tick is safe: the ledger only ever holds rows that are
            # already past `expires_at`, and the replay check reads by primary key,
            # so a late sweep costs disk rather than correctness. Never the other
            # way round — the sweep deletes only expired rows, so it can never
            # erase a jti that could still be replayed.
            #
            # Procedure runs ride the same tick because they expire the same way:
            # a row whose TTL passed is dead state nobody will look at again. That
            # half is not purely disk, though. An expired run left `active` holds
            # the one-active-per-conversation index, so the next message on that
            # conversation cannot start a procedure until this closes it. The
            # runtime closes one as soon as it sees it; this catches the visitor
            # who never came back.
            #
            # One half failing must not stop the other: the ledger is a security
            # control and the runs are a liveness fix, and neither is worth losing
            # to the other's outage.
            #
            # The action-call ledger rides the same tick for the same reason as the
            # jti ledger: it grows with every tool call an agent makes and is the
            # highest-volume table the actions engine writes. Retention is disk
            # only — the engine reads a call by id while it is in flight and never
            # reads one back past the window — so a missed tick costs space.
            pruned: Any
            runs: Any
            calls: Any
            try:
                pruned = await container.contacts_repo.sweep_jti_ledger()
            except Exception as exc:
                logger.warning(
                    "identity_jti_sweep_failed", task="checkpoint_expiry",
                    error=str(exc), exc_info=True,
                )
                pruned = {"status": "failed", "error": str(exc)}
            try:
                runs = await container.procedures_repo.sweep_expired_runs(
                    settings.PROCEDURE_RUNS_RETENTION_DAYS
                )
            except Exception as exc:
                logger.warning(
                    "procedure_run_sweep_failed", task="checkpoint_expiry",
                    error=str(exc), exc_info=True,
                )
                runs = {"status": "failed", "error": str(exc)}
            try:
                calls = await container.actions_repo.prune_calls(
                    older_than_days=settings.ACTION_CALLS_RETENTION_DAYS
                )
            except Exception as exc:
                logger.warning(
                    "action_calls_prune_failed", task="checkpoint_expiry",
                    error=str(exc), exc_info=True,
                )
                calls = {"status": "failed", "error": str(exc)}
            return {
                "status": "ok",
                "identity_jti_pruned": pruned,
                "procedure_runs": runs,
                "action_calls_pruned": calls,
            }
        case "email_retention":
            return await _email_retention(container)
        case "recrawl":
            # the original per-document URL refresh, unchanged, plus the new
            # source-level pass. Both run; a failure in one is reported, not fatal
            # to the other, so adding sources cannot break the existing task.
            #
            # The import is function-local like `rollup` above, and here it is
            # required rather than stylistic: the retrain module is imported only
            # when this task fires, so a missing or broken sweep cannot take every
            # other cron task down with it at import time.
            documents: Any
            sources: Any
            try:
                documents = await _recrawl(container)
            except Exception as exc:
                logger.warning(
                    "recrawl_documents_failed", task="recrawl", error=str(exc), exc_info=True
                )
                documents = {"status": "failed", "error": str(exc)}
            try:
                from app.services.crawl.retrain import _recrawl_sources

                sources = await _recrawl_sources(container)
            except Exception as exc:
                logger.warning(
                    "recrawl_sources_failed", task="recrawl", error=str(exc), exc_info=True
                )
                sources = {"status": "failed", "error": str(exc)}
            return {"status": "ok", "documents": documents, "sources": sources}
        case "growth_dispatch":
            # The growth scheduler. Off entirely unless the platform switch is on,
            # reported rather than ignored so a schedule firing against a
            # deployment with the feature off is visible at the scheduler instead
            # of looking healthy and doing nothing.
            if not settings.GROWTH_ENABLED:
                return {"status": "skipped", "task": task, "reason": "growth_disabled"}
            return await container.growth_dispatch.tick()
        case "approval_expiry":
            # Deliberately NOT gated on GROWTH_ENABLED. Turning the platform off
            # must not leave approval requests pending forever: an owner who
            # never gets an answer is owed the expiry, and expiry only ever holds
            # a batch — it never sends anything.
            return await container.approvals.expire_sweep()
        case "growth_report_due":
            # Same gate as growth_dispatch: with the platform off there is no
            # report to build. A missed tick is safe — the next one re-asks the
            # ledger, and the unique key dedupes a double-fire.
            if not settings.GROWTH_ENABLED:
                return {"status": "skipped", "task": task, "reason": "growth_disabled"}
            # bound to an annotated local rather than returned directly: the
            # container is untyped, and a direct return hands back Any
            due_result: dict[str, Any] = await container.growth_reports.run_due()
            return due_result
        case "growth_store_sync":
            # Hourly, like the report tick: missed ticks are safe (the cursor
            # persists on the integration row) and a double-fire re-pages the
            # same cursor idempotently (upserts by external id).
            if not settings.GROWTH_ENABLED:
                return {"status": "skipped", "task": task, "reason": "growth_disabled"}
            sync_result: dict[str, Any] = await container.growth_stores.run_due()
            return sync_result
        case "growth_indiamart_poll":
            # Hourly, same shape as growth_store_sync: missed ticks cost one
            # day of enquiries and the next window re-pulls them; a double-fire
            # is deduped by the raw row's UNIQUE_QUERY_ID key.
            if not settings.GROWTH_ENABLED:
                return {"status": "skipped", "task": task, "reason": "growth_disabled"}
            poll_result: dict[str, Any] = await container.growth_indiamart.run_due()
            return poll_result
        case "growth_insights_due":
            # Daily. Same gate as the rest: with the platform off there is
            # nothing to watch. A missed tick costs one day of insights and
            # nothing else, and a double-fire is a no-op — every candidate is
            # deduped on (tenant_id, uid) before and during the insert.
            if not settings.GROWTH_ENABLED:
                return {"status": "skipped", "task": task, "reason": "growth_disabled"}
            insights_result: dict[str, Any] = await container.growth_insights.run_due()
            return insights_result
        case "growth_crm_retry":
            # Hourly retry sweep for CRM pushes that failed (checklist item c:
            # CRM down, nothing blocks the pipeline, retry via queue). Missed
            # ticks are safe — the backoff clock runs off the link row's own
            # attempts/updated_at, not off this schedule — and a double-fire
            # re-reads the same due rows, where the per-lead lease and the
            # synced_status check collapse the duplicate work.
            if not settings.GROWTH_ENABLED:
                return {"status": "skipped", "task": task, "reason": "growth_disabled"}
            crm_result: dict[str, Any] = await _crm_retry_fanout(container)
            return crm_result
        case _:
            # 200, not an error: an unknown task will never succeed and a non-2xx
            # would make the scheduler redeliver it forever
            logger.warning("unknown_cron_task", task=task)
            return {"status": "ignored", "task": task}


async def _crm_retry_fanout(container: ContainerDep) -> dict[str, Any]:
    """One ``growth_crm_push`` sweep job per tenant per connected CRM.

    The sweep itself runs in the job (where the per-tenant attribution boundary
    is), not here: this is only the fan-out, shaped like ``run_due`` in
    stores.py — candidates under ``admin_scope`` against ``tenants``, the
    per-tenant provider list inside that tenant's own scope.
    """
    from app.adapters.postgres.growth_repo import active_tenant_ids
    from app.adapters.queue.base import Job
    from app.services.growth.crm.service import PUSH_JOB

    queued = 0
    failures: list[str] = []
    total = 0
    # paginated, not one page: active_tenant_ids pages by id so a deployment
    # with more tenants than one page still reaches them all — an unpaged
    # fan-out would serve the first 200 forever and the rest never
    after: UUID | None = None
    while True:
        tenants = await active_tenant_ids(limit=200, after=after)
        if not tenants:
            break
        total += len(tenants)
        for tenant_id in tenants:
            try:
                providers = await container.growth_crm.connected_providers(tenant_id)
                for provider in providers:
                    try:
                        await container.queue.enqueue(
                            Job(
                                kind=PUSH_JOB,
                                tenant_id=tenant_id,
                                payload={"provider": provider},
                            )
                        )
                        queued += 1
                    except Exception:
                        logger.warning(
                            "growth_crm_retry_enqueue_failed",
                            task="growth_crm_retry",
                            tenant_id=str(tenant_id),
                            provider=provider,
                            exc_info=True,
                        )
            except Exception:
                failures.append(str(tenant_id))
                logger.warning(
                    "growth_crm_retry_tenant_failed",
                    task="growth_crm_retry",
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
        if len(tenants) < 200:
            break
        after = tenants[-1]
    return {"status": "ok", "tenants": total, "queued": queued, "failed": failures}


async def _email_retention(container: ContainerDep) -> dict[str, Any]:
    """Expire inbound/delivery receipts and resolved escalations.

    Two different shapes, deliberately not unified. `purge_receipts_older_than`
    already walks tenants itself under `admin_scope` and runs each DELETE inside
    that tenant's `tenant_scope`, so it is called once. `purge_older_than` is
    per-tenant by signature, so the tenant list is gathered here and each call
    opens its own scope — the RLS policy, not this loop, is what confines the
    statement.

    A failure in one half is reported rather than fatal to the other: a broken
    escalation sweep must not stop receipts expiring, because receipts are the
    half that carries message bodies.
    """
    settings = container.settings
    receipts: Any
    escalations: Any
    try:
        receipts = await container.email_repo.purge_receipts_older_than(
            settings.EMAIL_RECEIPT_RETENTION_DAYS
        )
    except Exception as exc:
        logger.warning(
            "email_receipt_purge_failed", task="email_retention", error=str(exc), exc_info=True
        )
        receipts = {"status": "failed", "error": str(exc)}

    try:
        async with admin_scope() as session:
            rows = (
                await session.execute(
                    text("SELECT id FROM tenants WHERE status = 'active' ORDER BY id LIMIT 200")
                )
            ).mappings().all()
        removed = 0
        failures: list[str] = []
        for row in rows:
            try:
                removed += await container.escalation_repo.purge_older_than(
                    row["id"], settings.ESCALATION_RETENTION_DAYS
                )
            except Exception:
                # one tenant's purge failing must not starve the other 199: the
                # tick reruns on the schedule, and the failed id is reported back
                failures.append(str(row["id"]))
                logger.warning(
                    "escalation_purge_tenant_failed", task="email_retention",
                    tenant_id=str(row["id"]), exc_info=True,
                )
        escalations = removed if not failures else {"purged": removed, "failed": failures}
    except Exception as exc:
        logger.warning(
            "escalation_purge_failed", task="email_retention", error=str(exc), exc_info=True
        )
        escalations = {"status": "failed", "error": str(exc)}

    return {"status": "ok", "email_receipts_pruned": receipts, "escalations_pruned": escalations}


async def _recrawl(container: ContainerDep) -> dict[str, Any]:
    """Re-fetch URL-sourced documents whose refresh interval has elapsed.

    A document ingested by URL keeps its provenance in `metadata` (`source_url`,
    `recrawl_hours`, `last_crawled_at`), written by the URL upload endpoint. This
    finds the due ones and runs them through the same guard → fetch → gate →
    store → enqueue path as a fresh upload, so a changed remote file is re-indexed
    and an unchanged one is deduplicated by content hash at no parse cost.

    Cross-tenant by nature, so the candidate query runs under `admin_scope` and
    every subsequent write is scoped to the document's own tenant. One document's
    failure is logged and skipped rather than failing the tick.
    """
    settings = container.settings
    batch = max(1, settings.URL_RECRAWL_BATCH)
    now = datetime.now(UTC)

    async with admin_scope() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, tenant_id, agent_id, filename,
                           metadata->>'source_url'      AS source_url,
                           metadata->>'recrawl_hours'   AS recrawl_hours,
                           metadata->>'last_crawled_at' AS last_crawled_at
                      FROM documents
                     WHERE status <> 'deleted'
                       AND metadata ? 'source_url'
                       AND COALESCE((metadata->>'recrawl_hours')::int, 0) > 0
                     ORDER BY COALESCE(metadata->>'last_crawled_at', '') ASC
                     LIMIT :limit
                    """
                ),
                {"limit": batch * 4},
            )
        ).mappings().all()

    due: list[dict[str, Any]] = []
    for row in rows:
        hours = int(row["recrawl_hours"] or 0)
        if hours <= 0 or not row["source_url"]:
            continue
        last = row["last_crawled_at"]
        if last:
            try:
                when = datetime.fromisoformat(last)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
            except ValueError:
                when = None
            if when is not None and now - when < timedelta(hours=hours):
                continue  # not due yet
        due.append(dict(row))
        if len(due) >= batch:
            break

    refreshed = 0
    skipped: list[str] = []
    for row in due:
        try:
            await _recrawl_one(container, row)
            refreshed += 1
        except AppError as exc:
            # a dead link or a URL that now resolves privately must not stop the
            # rest of the batch; the reason is logged for the tenant's Documents view
            logger.warning(
                "recrawl_failed",
                task="recrawl",
                document_id=str(row["id"]),
                tenant_id=str(row["tenant_id"]),
                code=exc.code,
                error=exc.message,
            )
            skipped.append(str(row["id"]))
        except Exception:
            logger.warning(
                "recrawl_error", task="recrawl", document_id=str(row["id"]), exc_info=True
            )
            skipped.append(str(row["id"]))

    logger.info("recrawl_done", candidates=len(due), refreshed=refreshed, skipped=len(skipped))
    return {
        "status": "ok",
        "candidates": len(due),
        "refreshed": refreshed,
        "skipped": len(skipped),
    }


async def _recrawl_one(container: ContainerDep, row: dict[str, Any]) -> None:
    """Refresh one document from its source URL.

    Reuses the upload endpoint's streaming fetch (which validates every redirect
    hop) and the same gate, so a recrawl can never bypass a control that a manual
    URL upload enforces.
    """
    from app.api.v1.documents import fetch_url_stream, url_recrawl_quota

    tenant_id: UUID = row["tenant_id"]
    url: str = row["source_url"]
    settings = container.settings

    quota = await url_recrawl_quota(container, tenant_id)
    gated = await container.gate.run(
        tenant_id,
        row["filename"] or "download",
        fetch_url_stream(settings, url),
        quota,
        max_bytes=settings.MAX_UPLOAD_BYTES,
    )

    existing = await container.documents.find_by_hash(tenant_id, gated.sha256)
    stamp = datetime.now(UTC).isoformat()

    if existing is not None:
        # bytes unchanged (or already stored under another row): stamp the crawl
        # time so the interval restarts, and skip re-parsing identical content
        await container.documents.update_metadata(
            tenant_id,
            row["id"],
            metadata={
                "source_url": url,
                "recrawl_hours": int(row["recrawl_hours"] or 0),
                "last_crawled_at": stamp,
                "last_crawl_result": "unchanged",
            },
        )
        return

    from app.adapters.queue.base import Job
    from app.adapters.storage.base import document_path

    # content changed: overwrite this document's bytes in place and re-enqueue, so
    # the tenant keeps one row (and its tags/collection) across refreshes
    path = document_path(tenant_id, row["id"])
    await container.storage.put(path, gated.data, gated.mime)
    await container.documents.update_metadata(
        tenant_id,
        row["id"],
        metadata={
            "source_url": url,
            "recrawl_hours": int(row["recrawl_hours"] or 0),
            "last_crawled_at": stamp,
            "last_crawl_result": "updated",
        },
    )
    await container.queue.enqueue(
        Job(
            kind="ingest_document",
            tenant_id=tenant_id,
            payload={"document_id": str(row["id"]), "storage_path": path},
        )
    )


# ── local cron ──────────────────────────────────────────────────────────────
#
# The hosted deployment drives these tasks with QStash schedules (setup.md's
# operator table). A laptop or a demo has no such schedules, and the swarm then
# silently never runs — every growth cron needs a ping, and the failure is
# invisible: no error, just a dashboard whose agents say they are waiting.
# LOCAL_CRON starts one in-process ticker that calls the same `_run_cron_task`
# bodies on the same cadences, so dev and prod agree on what a task does and the
# only divergence is who pulls the trigger.
#
# Do not enable it alongside real QStash schedules: every task is idempotent
# (claim-stamped or due-computed), so double-firing is waste rather than
# corruption, but it is still waste.

LOCAL_CRON_INTERVALS: dict[str, int] = {
    # the send-window/cap spine: five minutes is setup.md's cadence
    "growth_dispatch": 300,
    "rollup": 900,
    "recrawl": 3600,
    "checkpoint_expiry": 3600,
    "approval_expiry": 3600,
    "growth_report_due": 3600,
    "growth_store_sync": 3600,
    "growth_crm_retry": 3600,
    "growth_indiamart_poll": 3600,
    "growth_insights_due": 86_400,
    "email_retention": 86_400,
}


async def _local_cron_loop(container: Any, stop: asyncio.Event) -> None:
    """Fire each cron task when its interval elapses, forever, fail-open.

    One loop rather than one task per cadence: the due check is trivial and a
    single sleep keeps the log readable, while per-task try/except keeps one
    failing task from taking the others down with it.
    """
    now = time.monotonic()
    # stagger the first fire across the first minute, so boot does not stampede
    # eleven tasks into the same second (they would mostly no-op, but the log
    # and the DB would both say otherwise). Capped at a minute so no task waits
    # a meaningful share of its cadence just for the stagger.
    next_due = {
        task: now + (i * 37) % min(interval, 60)
        for i, (task, interval) in enumerate(sorted(LOCAL_CRON_INTERVALS.items()))
    }
    while not stop.is_set():
        now = time.monotonic()
        for task, due_at in next_due.items():
            if due_at > now:
                continue
            interval = LOCAL_CRON_INTERVALS[task]
            next_due[task] = now + interval
            try:
                result = await _run_cron_task(task, container)
                logger.info("local_cron_fired", task=task, result=str(result)[:200])
            except Exception:
                # a cron task that raises is logged and skipped, exactly as the
                # HTTP route treats it: the next tick is another chance
                logger.error("local_cron_task_failed", task=task, exc_info=True)
        # wake often enough that the shortest cadence (five minutes) is never
        # more than a few seconds late, and often enough to notice `stop`
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            pass


def start_local_cron(container: Any) -> asyncio.Event:
    """Start the ticker from the app lifespan. Returns the stop event.

    The task holds the only strong reference besides the event loop's, and the
    loop exits on the event rather than cancellation, so shutdown is a set plus
    at most one in-flight task finishing — no cancelled-task warnings.
    """
    stop = asyncio.Event()
    task = asyncio.create_task(_local_cron_loop(container, stop))
    # keep a strong reference so a garbage-collected task does not silently stop
    # the swarm mid-shift
    _LOCAL_CRON_TASKS.add(task)
    task.add_done_callback(_LOCAL_CRON_TASKS.discard)
    return stop


_LOCAL_CRON_TASKS: set[asyncio.Task[None]] = set()
