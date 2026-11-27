"""Internal job callback. Not part of the public API.

The queue POSTs here when a job is due. The endpoint is publicly reachable by
necessity — an external scheduler must be able to call it — so the signature
check is the only thing standing between the internet and tenant ingestion. It
runs before the body is parsed.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Request

from app.api.deps import ContainerDep
from app.core.errors import AuthenticationError
from app.core.logging import get_logger, tenant_id_var

logger = get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


@router.post("/jobs")
async def run_job(
    request: Request,
    container: ContainerDep,
    upstash_signature: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    settings = container.settings
    raw = (await request.body()).decode()

    # Two credentials, either one admits the caller: the shared secret
    # (x-internal-token, the door for the non-QStash backends whose calls
    # carry nothing else to verify) or a valid Upstash signature (QStash's
    # own calls — they sign, they do not know the token). Checking the token
    # first and rejecting unconditionally would lock QStash out whenever
    # INTERNAL_TOKEN is set, and every queued job — a telegram reply included
    # — would die as a 401 the queue keeps retrying. Empty token (the
    # default) skips that credential and behaviour is exactly what it was.
    token_ok = bool(settings.INTERNAL_TOKEN) and _internal_ok(
        request, settings.INTERNAL_TOKEN
    )
    if settings.INTERNAL_TOKEN and not token_ok and settings.QUEUE_BACKEND != "qstash":
        logger.warning("internal_token_rejected", path="/internal/jobs")
        raise AuthenticationError("invalid internal token")

    if settings.QUEUE_BACKEND == "qstash":
        from app.adapters.queue.qstash import verify_signature

        if not token_ok and not verify_signature(
            upstash_signature or "",
            raw,
            settings.QSTASH_CURRENT_SIGNING_KEY,
            settings.QSTASH_NEXT_SIGNING_KEY,
        ):
            logger.warning("job_signature_rejected")
            raise AuthenticationError("invalid job signature")

    import json

    from app.adapters.queue.base import Job
    from app.core.errors import ValidationError

    # parse errors on the public callback must be a 422, not a 500: QStash
    # redelivers on any non-2xx, and a body it will never re-send us intact is
    # not worth retrying
    try:
        body = json.loads(raw)
        job = Job.from_body(body if isinstance(body, dict) else {})
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        logger.warning("job_body_rejected", body=raw[:64])
        raise ValidationError("malformed job payload") from None

    tenant_id_var.set(str(job.tenant_id))

    try:
        return await _dispatch_job(container, job)
    except Exception as exc:
        if _final_qstash_attempt(request, container):
            # QStash has exhausted its retries: this callback is the last time we
            # will ever see the job, and a non-2xx now drops it for good. Record
            # the whole payload, loudly, so a human can replay it by hand.
            logger.error(
                "job_dead_lettered",
                kind=job.kind,
                tenant_id=str(job.tenant_id),
                payload=job.payload,
                error=str(exc),
                exc_info=True,
            )
        raise


def _internal_ok(request: Request, expected: str) -> bool:
    """Constant-time comparison of the x-internal-token header."""
    import hmac

    got = request.headers.get("x-internal-token") or ""
    return hmac.compare_digest(got.encode(), expected.encode())


def _final_qstash_attempt(request: Request, container: ContainerDep) -> bool:
    """True when this callback carries QStash's final retry count.

    `Upstash-Retried` is 0 on first delivery and equals the configured ceiling on
    the last attempt. Absent (local queue, or a direct call) means there is no
    retry ladder to be at the end of, so it is never the final attempt.

    The ceiling is read from the queue itself — the same value it sent in
    `Upstash-Retries` on enqueue — rather than the module default, so a queue
    built with a custom `max_retries` still detects its own last attempt. A queue
    with no such attribute (the local one) never sends the header anyway, so the
    `raw is None` guard above returns first.
    """
    raw = request.headers.get("upstash-retried")
    if raw is None:
        return False
    from app.adapters.queue.qstash import DEFAULT_MAX_RETRIES

    max_retries = getattr(container.queue, "max_retries", DEFAULT_MAX_RETRIES)
    try:
        return int(raw) >= max_retries
    except ValueError:
        return False


async def _dispatch_job(container: ContainerDep, job: Any) -> dict[str, Any]:
    if job.kind == "ingest_document":
        return await _ingest(container, job)
    if job.kind == "ingest_documents":
        return await _ingest_batch(container, job)
    if job.kind == "whatsapp_reply":
        whatsapp: dict[str, Any] = await container.whatsapp.process_reply(
            job.tenant_id, job.payload
        )
        return whatsapp
    if job.kind == "crawl_wave":
        return await _crawl_wave(container, job)
    if job.kind == "delete_source":
        return await _delete_source(container, job)
    if job.kind == "ingest_qa":
        return await _ingest_qa(container, job)
    if job.kind == "email_reply":
        # enqueued by the email webhook (EMAIL_JOB_KIND); without a branch here
        # every inbound customer email was "accepted, then ignored" on qstash
        email: dict[str, Any] = await container.email_service.process_reply(
            job.tenant_id, job.payload
        )
        return email
    if job.kind == "escalation_notify":
        # enqueued by EscalationService._enqueue_notify; same silent-drop shape
        from uuid import UUID

        return {
            "status": "notified",
            "count": len(
                await container.escalation.notify(
                    job.tenant_id, UUID(str(job.payload["escalation_id"]))
                )
            ),
        }

    if job.kind in GROWTH_JOB_KINDS:
        return await _growth(container, job)

    logger.warning("unknown_job_kind", kind=job.kind)
    # 200 rather than an error: an unknown kind will never succeed, and a
    # non-2xx would make the queue redeliver it forever
    return {"status": "ignored", "kind": job.kind}


# Growth platform job kinds, in one set so the branch above stays a lookup and
# the LocalQueue handler in app/api/main.py has one thing to mirror.
GROWTH_JOB_KINDS: frozenset[str] = frozenset(
    {
        "approval_notify",
        "approval_expire",
        "growth_send_batch",
        "growth_follow_up",
        "growth_delete",
        "growth_research",
        "growth_batch_release",
        "growth_compose",
        "growth_inbound_lead",
        "growth_inbound_backfill",
        "growth_report",
        "growth_store_sync",
        "growth_store_webhook",
        "growth_campaign_release",
        "growth_campaign_draft",
        "growth_campaign_results",
        "growth_insights",
        "growth_crm_push",
        # the profile agent (P4), enqueued by the crawl engine's ready verdict
        # and by POST /v1/growth/profile/discover
        "growth_profile",
    }
)

# Activation gating (agent.md §9): which job kinds belong to an agent whose
# activation state must be consulted before the work runs. A gated kind whose
# agent is not active returns {"status": "skipped", "reason": …} exactly like
# every other skip path — blocked is a state, not an error.
#
# Deliberately NOT gated:
# * profile/crm/store jobs, approvals, compliance — platform work that must
#   run whatever the fact base looks like (the profile agent is what *fills*
#   the facts; gating it on them would deadlock a fresh tenant);
# * batch and campaign RELEASE (and results, and the send spine itself) —
#   already-approved work still sends. The approval the owner gave is the
#   authority there, and a released batch must not silently strand because a
#   fact went missing between approval and release.
GATED_AGENT_KEYS: dict[str, str] = {
    "growth_research": "growth_research",
    "growth_compose": "growth_compose",
    "growth_follow_up": "growth_followup",
    # both inbound kinds run the speed-to-lead agent: the webhook's lead job
    # and the IndiaMART backfill, which feeds each pulled enquiry straight
    # into growth_inbound.ingest
    "growth_inbound_lead": "growth_inbound",
    "growth_inbound_backfill": "growth_inbound",
    "growth_report": "growth_report",
    "growth_campaign_draft": "growth_campaign",
    "growth_insights": "growth_insights",
}


async def _growth(container: ContainerDep, job: Any) -> dict[str, Any]:
    """Every growth job kind, behind one attribution boundary.

    `agent_key` and `job_kind` are bound into the structlog context before any
    work starts, so every line a growth job emits — including ones from deep
    inside a service that knows nothing about jobs — carries who was running and
    for which tenant. `tenant_id` is already bound by `run_job`.

    The binding is cleared on the way out. These contextvars live for the life of
    the request, and a callback that left `agent_key` set would mislabel whatever
    the next handler on this worker logged.
    """
    from uuid import UUID

    from app.core.logging import agent_key_var, job_kind_var

    agent_token = agent_key_var.set(job.kind.removeprefix("growth_"))
    kind_token = job_kind_var.set(job.kind)
    try:
        # The activation gate, before any branch runs. Fail-open on the gate
        # itself: a facts read that cannot be served runs the job rather than
        # pausing a tenant's pipeline for want of a badge — the same posture
        # the graph endpoint takes.
        gate_key = GATED_AGENT_KEYS.get(job.kind)
        if gate_key is not None:
            try:
                allowed, reason = await container.agent_prompts.gate(
                    job.tenant_id, gate_key
                )
            except Exception:
                logger.warning(
                    "agent_gate_check_failed",
                    kind=job.kind,
                    agent_key=gate_key,
                    tenant_id=str(job.tenant_id),
                    exc_info=True,
                )
                allowed = True
            if not allowed:
                return {"status": "skipped", "reason": reason}

        if job.kind == "approval_notify":
            approval_id = UUID(str(job.payload["approval_id"]))
            receipts = await container.approvals.notify(job.tenant_id, approval_id)
            return {
                "status": "notified",
                "delivered": sum(1 for r in receipts if r.get("delivered")),
                "attempted": len(receipts),
            }
        # each branch binds its result to an annotated local rather than
        # returning the call directly: the container is typed `Any`, so a direct
        # return hands back `Any` from a function that promises a dict
        result: dict[str, Any]
        if job.kind == "approval_expire":
            result = await container.approvals.expire_tenant(job.tenant_id)
        elif job.kind == "growth_send_batch":
            result = await container.growth_dispatch.send_batch(job.tenant_id, job.payload)
        elif job.kind == "growth_follow_up":
            result = await container.growth_followup.run(job.tenant_id)
        elif job.kind == "growth_research":
            result = await container.growth_research.run(job.tenant_id, job.payload)
        elif job.kind == "growth_batch_release":
            result = await container.growth_coldmail.release_batch(
                job.tenant_id, job.payload
            )
        elif job.kind == "growth_compose":
            result = await container.growth_coldmail.compose(job.tenant_id, job.payload)
        elif job.kind == "growth_inbound_lead":
            if str(job.payload.get("source") or "") == "justdial":
                # same kind, different envelope: the payload came from the
                # email webhook, not the form ingress, and the worker must
                # fetch the mail before it can parse it
                result = await container.growth_justdial.handle(
                    job.tenant_id, job.payload
                )
            else:
                result = await container.growth_inbound.handle(
                    job.tenant_id, job.payload
                )
        elif job.kind == "growth_inbound_backfill":
            # pull-source catch-up: the poll cron (one day) and the tenant's
            # backfill button (wider) enqueue the same kind with a days width
            if str(job.payload.get("source") or "") == "indiamart":
                result = await container.growth_indiamart.backfill(
                    job.tenant_id, job.payload
                )
            else:
                result = {"status": "ignored", "kind": job.kind}
        elif job.kind == "growth_report":
            result = await container.growth_reports.run(job.tenant_id, job.payload)
        elif job.kind == "growth_store_sync":
            provider = str(job.payload.get("provider") or "")
            external_ref = str(job.payload.get("external_ref") or "")
            if provider and external_ref:
                result = await container.growth_stores.sync_store(
                    job.tenant_id, provider, external_ref
                )
            else:
                # the manual "sync now" button fans out to every connected
                # store; the cron fans out per store above
                result = await container.growth_stores.sync_tenant(job.tenant_id)
        elif job.kind == "growth_store_webhook":
            result = await container.growth_stores.handle_webhook(
                job.tenant_id,
                str(job.payload.get("provider") or ""),
                str(job.payload.get("topic") or ""),
                dict(job.payload.get("payload") or {}),
            )
        elif job.kind == "growth_campaign_release":
            result = await container.growth_campaigns.release(job.tenant_id, job.payload)
        elif job.kind == "growth_campaign_draft":
            result = await container.growth_campaigns.draft_from_job(
                job.tenant_id, job.payload
            )
        elif job.kind == "growth_campaign_results":
            # enqueued by the dispatcher when a send batch drains: the only
            # thing that moves a campaign off 'sending' without an owner
            # happening to open the panel
            result = await container.growth_campaigns.recompute(
                job.tenant_id, job.payload
            )
        elif job.kind == "growth_insights":
            result = await container.growth_insights.run(job.tenant_id, job.payload)
        elif job.kind == "growth_crm_push":
            # payload carries {provider, lead_id} for one changed lead (the
            # trigger path), {provider, lead_id, force} for the Retry button,
            # or {provider} for the cron's retry sweep
            result = await container.growth_crm.run(job.tenant_id, job.payload)
        elif job.kind == "growth_profile":
            # payload carries {source_id} from the crawl's ready verdict, or
            # {force: true} from the on-demand endpoint
            result = await container.growth_profile_agent.run(job.tenant_id, job.payload)
        else:  # growth_delete
            result = await container.growth_compliance.purge_tenant(
                job.tenant_id, actor=str(job.payload.get("actor") or "")
            )
        return result
    finally:
        agent_key_var.reset(agent_token)
        job_kind_var.reset(kind_token)


# how many child documents one delete_source callback reaps before handing the
# rest to a follow-up job. A 10,000-page source is 10,000 vector deletes, which no
# single callback can finish inside its timeout
DELETE_BATCH = 50


async def _crawl_wave(container: ContainerDep, job: Any) -> dict[str, Any]:
    """Run one wave of a crawl and let the engine decide whether to continue.

    Thin on purpose: the frontier, the bounds and the re-enqueue all live in the
    engine, so a redelivered callback resumes from the database rather than from
    anything this handler remembers.
    """
    from uuid import UUID

    source_id = UUID(str(job.payload["source_id"]))
    result = await container.crawl_engine.run_wave(job.tenant_id, source_id)
    return {"status": "ok", "source_id": str(source_id), "wave": result}


async def _delete_source(container: ContainerDep, job: Any) -> dict[str, Any]:
    """Reap one batch of a deleted source's children, then re-enqueue or finish.

    The source row is already invisible to the tenant — the API marked it on the
    way out — so this is cleanup, and it is batched because the alternative is a
    request that times out on a large site and leaves vectors behind.

    The row itself is deleted last. `documents.source_id` is `ON DELETE SET NULL`,
    so removing the parent first would orphan every child document instead of
    deleting it, and the pages would stay searchable forever.
    """
    from uuid import UUID

    from sqlalchemy import text

    from app.core.db import tenant_scope

    source_id = UUID(str(job.payload["source_id"]))

    async with tenant_scope(job.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id FROM documents WHERE source_id = :id "
                    "AND status <> 'deleted' ORDER BY created_at LIMIT :batch"
                ),
                {"id": source_id, "batch": DELETE_BATCH},
            )
        ).mappings().all()
    document_ids = [row["id"] for row in rows]

    for document_id in document_ids:
        # vectors first: a chunk whose document row is gone is unattributable and
        # would keep answering questions
        await container.indexer.remove(job.tenant_id, document_id)
        await container.documents.soft_delete(job.tenant_id, document_id)

    if len(document_ids) == DELETE_BATCH:
        from app.adapters.queue.base import Job

        await container.queue.enqueue(
            Job(
                kind="delete_source",
                tenant_id=job.tenant_id,
                payload={"source_id": str(source_id)},
            )
        )
        return {
            "status": "partial",
            "source_id": str(source_id),
            "deleted": len(document_ids),
        }

    async with tenant_scope(job.tenant_id) as session:
        # source_links and qa_pairs are ON DELETE CASCADE, so this one statement
        # takes the frontier and the authored pairs with it
        await session.execute(text("DELETE FROM sources WHERE id = :id"), {"id": source_id})
    return {"status": "deleted", "source_id": str(source_id), "deleted": len(document_ids)}


async def _ingest_qa(container: ContainerDep, job: Any) -> dict[str, Any]:
    """Chunk, embed and index authored Q&A pairs.

    A Q&A pair is already a chunk, so there is no parse and no OCR — the shortcut
    is the point. Everything after chunking is the ordinary path: the same chunk
    rows, the same indexer, the same usage meter, so retrieval and citations need
    no Q&A special case.

    Per pair rather than per set, and each pair's failure is recorded on its own
    document, because one malformed answer must not cost the tenant the other
    ninety-nine.
    """
    from uuid import UUID

    from sqlalchemy import text

    from app.core.constants import DocumentStatus, JobState, JobStep
    from app.core.db import tenant_scope
    from app.domain.chunking import build_qa_chunks

    source_id = UUID(str(job.payload["source_id"]))
    requested = [UUID(str(item)) for item in job.payload.get("pair_ids") or []]

    clause = "source_id = :source_id AND document_id IS NOT NULL"
    params: dict[str, Any] = {"source_id": source_id}
    if requested:
        clause += " AND id = ANY(:ids)"
        params["ids"] = requested

    async with tenant_scope(job.tenant_id) as session:
        pairs = (
            await session.execute(
                text(
                    # `clause` is assembled from literal fragments above and every
                    # value is bound, so the S608 suppression is safe. The directive
                    # sits on the first part of the concatenation, where ruff wants it.
                    "SELECT id, document_id, title, questions, answer FROM qa_pairs "  # noqa: S608
                    f"WHERE {clause} ORDER BY ordinal"
                ),
                params,
            )
        ).mappings().all()

    indexed = 0
    failed = 0
    for pair in pairs:
        document_id = pair["document_id"]
        try:
            chunks = build_qa_chunks(
                job.tenant_id,
                document_id,
                pair["title"],
                list(pair["questions"] or []),
                pair["answer"],
            )
            # deterministic chunk ids, so a redelivered callback replaces the same
            # rows and re-upserts the same vectors instead of doubling them
            await container.documents.store_chunks(job.tenant_id, document_id, chunks)
            await container.indexer.index(
                job.tenant_id, document_id, chunks, pair["title"]
            )
            await container.documents.mark_step(
                job.tenant_id, document_id, JobStep.DONE, JobState.SUCCEEDED
            )
            await container.documents.set_status(
                job.tenant_id, document_id, DocumentStatus.READY, None
            )
            await container.documents.record_usage(
                job.tenant_id, document_id, chunk_count=len(chunks)
            )
            indexed += 1
        except Exception as exc:
            failed += 1
            logger.error(
                "ingest_qa_failed",
                pair_id=str(pair["id"]),
                document_id=str(document_id),
                error=str(exc),
                exc_info=True,
            )
            await container.documents.set_status(
                job.tenant_id,
                document_id,
                DocumentStatus.FAILED,
                "this answer could not be indexed; edit and save it to try again",
            )

    async with tenant_scope(job.tenant_id) as session:
        # counters are recomputed from the pairs, never incremented, so a
        # redelivered callback lands on the same numbers
        await session.execute(
            text(
                """
                UPDATE sources s SET
                    link_total   = c.total,
                    link_trained = c.ready,
                    link_pending = c.total - c.ready,
                    size_bytes   = c.bytes,
                    chunk_count  = c.chunks,
                    crawl_state  = CASE WHEN c.ready > 0 THEN 'ready' ELSE crawl_state END,
                    last_trained_at = CASE WHEN c.ready > 0 THEN now() ELSE last_trained_at END,
                    updated_at   = now()
                  FROM (
                    SELECT count(*) AS total,
                           count(*) FILTER (WHERE d.status = 'ready') AS ready,
                           COALESCE(sum(octet_length(p.answer)), 0)   AS bytes,
                           COALESCE(sum(d.chunk_count), 0)            AS chunks
                      FROM qa_pairs p
                      LEFT JOIN documents d
                        ON d.id = p.document_id AND d.status <> 'deleted'
                     WHERE p.source_id = :id
                  ) c
                 WHERE s.id = :id
                """
            ),
            {"id": source_id},
        )

    return {
        "status": "ready" if failed == 0 else "partial",
        "source_id": str(source_id),
        "indexed": indexed,
        "failed": failed,
    }


async def _ingest(container: ContainerDep, job: Any) -> dict[str, Any]:
    """Run the ingestion pipeline for one document.

    Idempotent by construction — the step machine records each completed step, so
    a redelivered callback resumes rather than repeating paid work.
    """
    from uuid import UUID

    document_id = UUID(str(job.payload["document_id"]))
    storage_path = str(job.payload["storage_path"])

    result = await _ingest_document(container, job.tenant_id, document_id, storage_path)
    if result.get("skipped"):
        return {"status": "skipped", "reason": "document not found"}
    return {
        "status": "ready",
        "document_id": str(document_id),
        "chunks": result["chunks"],
        "vectors": result["vectors"],
    }


async def _ingest_batch(container: ContainerDep, job: Any) -> dict[str, Any]:
    """Run the ingestion pipeline for a batch of crawled pages.

    One message per handful of pages instead of one per page: the qstash free
    tier counts messages (500/day), and a whole-site crawl at one message per
    page is a quota event, not a crawl.

    Two bounds keep a batch honest:

    * the clock — a callback must answer inside the queue's request timeout
      (30s on the free tier), so the handler stops starting documents at an
      internal deadline and re-enqueues the rest as a fresh batch. Nothing is
      lost and nothing overruns the message.
    * per-document isolation — one document's failure (a 429 from the embedding
      tier, a corrupt blob) fails that document and nothing else. Failures are
      re-enqueued, bounded by INGEST_BATCH_MAX_ATTEMPTS; past that the document
      is already marked FAILED with its reason and the dashboard's Retry path
      is the recovery.
    """
    import asyncio
    import time
    from uuid import UUID

    from app.adapters.queue.base import Job as QueueJob

    docs = list(job.payload.get("docs") or [])
    if not docs:
        return {"status": "skipped", "reason": "empty batch"}
    attempt = int(job.payload.get("attempt") or 0)

    concurrency = max(1, container.settings.INGEST_DOC_CONCURRENCY)
    limit = asyncio.Semaphore(concurrency)
    deadline = time.monotonic() + INGEST_BATCH_DEADLINE_SECONDS

    ready: list[str] = []
    failed: list[dict[str, Any]] = []

    async def one(entry: dict[str, Any]) -> None:
        document_id = str(entry.get("document_id") or "")
        async with limit:
            try:
                await _ingest_document(
                    container,
                    job.tenant_id,
                    UUID(document_id),
                    str(entry.get("storage_path") or ""),
                )
                ready.append(document_id)
            except Exception as exc:
                # the pipeline has already recorded the failure against the
                # document, so the tenant can see why
                logger.error(
                    "ingest_batch_doc_failed",
                    tenant_id=str(job.tenant_id),
                    document_id=document_id,
                    error=str(exc),
                    exc_info=True,
                )
                failed.append(entry)

    while docs and time.monotonic() < deadline:
        chunk, docs = docs[:concurrency], docs[concurrency:]
        await asyncio.gather(*(one(entry) for entry in chunk))

    leftover = docs + failed
    if leftover and attempt < INGEST_BATCH_MAX_ATTEMPTS:
        await container.queue.enqueue(
            QueueJob(
                kind="ingest_documents",
                tenant_id=job.tenant_id,
                payload={"docs": leftover, "attempt": attempt + 1},
                # a minute of spacing turns a burst of embedding-tier 429s into
                # a retry that lands after the window, not inside it
                delay_seconds=60,
            )
        )
        logger.info(
            "ingest_batch_requeued",
            tenant_id=str(job.tenant_id),
            ready=len(ready),
            requeued=len(leftover),
            attempt=attempt + 1,
        )
    elif leftover:
        # past the attempt ceiling: the documents are marked failed with their
        # reasons; a raise here would retry the whole batch including them
        logger.error(
            "ingest_batch_attempts_exhausted",
            tenant_id=str(job.tenant_id),
            ready=len(ready),
            failed=len(leftover),
        )

    return {"status": "ok", "ready": len(ready), "requeued": len(leftover), "attempt": attempt}


# how long one batch message may spend ingesting before the rest is re-enqueued.
# sits under the qstash free tier's 30s callback timeout with room for the
# response and the finishing writes
INGEST_BATCH_DEADLINE_SECONDS = 20
# a failed document is re-enqueued this many times before it stays failed
INGEST_BATCH_MAX_ATTEMPTS = 2


async def _ingest_document(
    container: ContainerDep, tenant_id: Any, document_id: Any, storage_path: str
) -> dict[str, Any]:
    """The shared per-document ingestion body of the single and batch jobs."""
    from app.services.ingestion.gate import GateResult
    from app.services.ingestion.pipeline import PipelineContext

    record = await container.documents.get(tenant_id, document_id)
    if record is None:
        logger.warning("ingest_document_missing", document_id=str(document_id))
        return {"chunks": 0, "vectors": 0, "skipped": True}

    data = await container.storage.get(storage_path)

    gate = GateResult(
        data=data,
        filename=record["filename"],
        mime=record["mime"],
        size_bytes=record["size_bytes"],
        sha256=record["sha256"],
        page_count=record.get("page_count", 0),
    )

    # contextual retrieval is per agent. resolve the document's agent config so
    # ingestion honours the tenant's setting rather than a global default
    contextual = False
    agent_id = record.get("agent_id")
    if agent_id is not None:
        from app.domain.agent_config import AgentConfig

        config_row = await container.documents.get_agent_config(tenant_id, agent_id)
        if config_row is not None:
            contextual = AgentConfig.model_validate(config_row).retrieval.contextual_retrieval

    context = PipelineContext(
        tenant_id=tenant_id,
        document_id=document_id,
        gate=gate,
        contextual=contextual,
    )

    try:
        await container.pipeline.run(context)
    except Exception:
        # the pipeline has already recorded the failure against the document, so
        # the tenant can see why. re-raise so the caller decides what a retry
        # means: the single-document job lets the queue retry, the batch job
        # re-enqueues only the failures
        logger.error(
            "ingest_failed", document_id=str(document_id), exc_info=True
        )
        raise

    return {"chunks": context.chunk_count, "vectors": context.vector_count}
