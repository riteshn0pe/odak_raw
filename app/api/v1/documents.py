"""Document endpoints.

Upload accepts the file, makes it durable, and returns. Ingestion runs
afterwards through the queue, because a large scan takes minutes of OCR and no
HTTP request should be held open for that.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from fastapi import APIRouter, File, UploadFile
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, TenantDep
from app.api.schemas import DocumentOut, UploadResponse
from app.core.constants import DocumentStatus
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.urlguard import assert_fetchable, assert_peer_public
from app.services.ingestion.gate import Quota

logger = get_logger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

# plan quotas bound the total corpus; this bounds the rate of arrival, so a
# runaway script cannot exhaust a month of OCR budget in one minute
UPLOADS_PER_HOUR = 120

# platform-wide corpus ceilings, applied to uploads and recrawls alike
MAX_DOCUMENTS = 25
MAX_STORAGE_BYTES = 268_435_456


async def _quota(container: ContainerDep, tenant_id: UUID) -> Quota:
    """Build the quota from the platform ceilings and current consumption.

    Read per upload rather than cached: a stale quota either blocks a tenant who
    just deleted something or lets one past the limit.
    """
    used = await container.documents.usage_totals(tenant_id)

    return Quota(
        max_documents=MAX_DOCUMENTS,
        max_storage_bytes=MAX_STORAGE_BYTES,
        used_documents=used["documents"],
        used_storage_bytes=used["storage_bytes"],
    )


async def url_recrawl_quota(container: Any, tenant_id: UUID) -> Quota:
    """Quota for a recrawl, which has no request and therefore no TenantContext.

    Same numbers as an upload. A refresh replaces bytes rather than adding a
    document, so the document count is left at its current value and only the
    storage ceiling really binds.
    """
    return await _quota(container, tenant_id)


@router.post("", response_model=UploadResponse, status_code=202)
async def upload(
    tenant: TenantDep, container: ContainerDep, file: UploadFile = File(...)
) -> UploadResponse:
    # uploads are the expensive path — parsing and OCR are paid per page — so the
    # ceiling is tighter than chat and measured per hour rather than per minute
    await container.rate_limiter.check(
        "upload", str(tenant.tenant_id), UPLOADS_PER_HOUR, 3600
    )

    quota = await _quota(container, tenant.tenant_id)

    async def stream() -> AsyncIterator[bytes]:
        while chunk := await file.read(container.settings.UPLOAD_READ_CHUNK_BYTES):
            yield chunk

    gated = await container.gate.run(
        tenant.tenant_id, file.filename or "upload", stream(), quota
    )
    return await _persist(container, tenant, gated)


class UrlIngestRequest(BaseModel):
    # http/https only, and every hop is checked against app/core/urlguard.py so a
    # tenant cannot point us at the private subnet or a metadata endpoint
    url: str = Field(min_length=1, max_length=2048)
    filename: str | None = Field(default=None, max_length=255)
    # re-fetch cadence for the recrawl cron task; None falls back to
    # URL_RECRAWL_DEFAULT_HOURS, and 0 means never
    recrawl_hours: int | None = Field(default=None, ge=0, le=24 * 90)


@router.post("/url", response_model=UploadResponse, status_code=202)
async def upload_by_url(
    body: UrlIngestRequest, tenant: TenantDep, container: ContainerDep
) -> UploadResponse:
    """Ingest a document the tenant points us at, rather than one they upload.

    Same expensive path as a file upload — fetch, gate, store, enqueue — so it
    shares the per-hour ceiling and flows through the identical pipeline.
    """
    await container.rate_limiter.check(
        "upload", str(tenant.tenant_id), UPLOADS_PER_HOUR, 3600
    )

    settings = container.settings
    # rejects non-public targets before a single packet leaves the process
    assert_fetchable(
        body.url,
        allow_private=settings.URL_FETCH_ALLOW_PRIVATE,
        allowed_ports=settings.url_fetch_ports,
    )

    quota = await _quota(container, tenant.tenant_id)

    parsed = urlparse(body.url)
    # last path segment is the natural filename; the gate only trusts it as a
    # hint anyway and resolves the real type from the fetched bytes
    filename = body.filename or (parsed.path.rsplit("/", 1)[-1] or "download")

    gated = await container.gate.run(
        tenant.tenant_id,
        filename,
        fetch_url_stream(settings, body.url),
        quota,
        max_bytes=settings.MAX_UPLOAD_BYTES,
    )
    recrawl = (
        body.recrawl_hours
        if body.recrawl_hours is not None
        else settings.URL_RECRAWL_DEFAULT_HOURS
    )
    return await _persist(
        container, tenant, gated, source_url=body.url, recrawl_hours=recrawl
    )


async def fetch_url_stream(settings: Any, url: str) -> AsyncIterator[bytes]:
    """Stream a remote document, validating every redirect hop.

    Redirects are followed manually rather than by httpx: an allowed URL that
    302s to ``169.254.169.254`` must be rejected, and the client would follow it
    without ever consulting the guard.

    Each hop is checked twice: ``assert_fetchable`` before the request, and
    ``assert_peer_public`` on the open response before any body is read, which
    catches a resolver that answered publicly for the guard and privately for the
    client.
    """
    timeout = httpx.Timeout(settings.HTTP_TIMEOUT_SECONDS, connect=10.0)
    target = url
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(settings.URL_FETCH_MAX_REDIRECTS + 1):
            assert_fetchable(
                target,
                allow_private=settings.URL_FETCH_ALLOW_PRIVATE,
                allowed_ports=settings.url_fetch_ports,
            )
            try:
                async with client.stream("GET", target) as response:
                    assert_peer_public(
                        response, allow_private=settings.URL_FETCH_ALLOW_PRIVATE
                    )
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValidationError("url redirected without a target")
                        # resolve relative redirects against the current hop
                        target = str(response.url.join(location))
                        continue
                    if response.status_code // 100 != 2:
                        raise ValidationError(
                            f"could not fetch url (status {response.status_code})"
                        )
                    # cap is re-enforced downstream by the gate, but bailing here
                    # avoids buffering an oversized body the gate would reject
                    async for chunk in response.aiter_bytes(
                        settings.UPLOAD_READ_CHUNK_BYTES
                    ):
                        yield chunk
                    return
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                raise ValidationError(f"could not fetch url: {exc}") from exc
    raise ValidationError("url redirected too many times")


async def _persist(
    container: ContainerDep,
    tenant: TenantDep,
    gated,
    *,
    source_url: str | None = None,
    recrawl_hours: int | None = None,
) -> UploadResponse:
    """Store gated bytes and enqueue ingestion. Shared by file and URL upload.

    Everything before this differs (a multipart part versus a fetched URL);
    everything after is identical, so it lives in one place. A URL-sourced
    document records where it came from so the recrawl task can refresh it.
    """
    # a repeat upload is not an error. return the existing document so the client
    # can proceed, and skip paying to parse the same bytes twice
    existing = await container.documents.find_by_hash(tenant.tenant_id, gated.sha256)
    if existing is not None:
        record = await container.documents.get(tenant.tenant_id, existing["id"])
        return UploadResponse(document=DocumentOut(**_document_fields(record)), duplicate=True)

    from app.adapters.storage.base import document_path

    document = await container.documents.create(
        tenant_id=tenant.tenant_id,
        filename=gated.filename,
        mime=gated.mime,
        size_bytes=gated.size_bytes,
        sha256=gated.sha256,
        storage_path="",
        agent_id=tenant.agent_id,
        page_count=gated.page_count,
    )

    # the crawl provenance rides in the existing metadata bag rather than new
    # columns: it is read only by the recrawl task, never filtered on
    if source_url:
        await container.documents.update_metadata(
            tenant.tenant_id,
            document.id,
            metadata={
                "source_url": source_url,
                "recrawl_hours": int(recrawl_hours or 0),
                "last_crawled_at": datetime.now(UTC).isoformat(),
            },
        )

    path = document_path(tenant.tenant_id, document.id)
    try:
        await container.storage.put(path, gated.data, gated.mime)
    except Exception as exc:
        # the row already exists at this point, so an unhandled raise would
        # leave it queued forever with no bytes and no job. same recovery as a
        # queue outage: mark failed so the Retry button can advance it
        logger.error(
            "upload_store_failed",
            document_id=str(document.id),
            tenant_id=str(tenant.tenant_id),
            error=str(exc),
            exc_info=True,
        )
        await container.documents.set_status(
            tenant.tenant_id,
            document.id,
            DocumentStatus.FAILED,
            "the file could not be stored; retry this document",
        )
        record = await container.documents.get(tenant.tenant_id, document.id)
        return UploadResponse(
            document=DocumentOut(**_document_fields(record)), duplicate=False
        )

    from app.adapters.queue.base import Job

    # the bytes are already durable, so a queue outage must not throw away the
    # upload. the document is marked failed with the reason instead, which is what
    # the dashboard's Retry button re-enqueues — previously this raised 503 and
    # left the row stuck in `queued` with nothing able to advance it
    try:
        await container.queue.enqueue(
            Job(
                kind="ingest_document",
                tenant_id=tenant.tenant_id,
                payload={"document_id": str(document.id), "storage_path": path},
            )
        )
    except Exception as exc:
        logger.error(
            "ingest_enqueue_failed",
            document_id=str(document.id),
            tenant_id=str(tenant.tenant_id),
            error=str(exc),
            exc_info=True,
        )
        await container.documents.set_status(
            tenant.tenant_id,
            document.id,
            DocumentStatus.FAILED,
            "could not be queued for processing; retry this document",
        )

    record = await container.documents.get(tenant.tenant_id, document.id)
    return UploadResponse(document=DocumentOut(**_document_fields(record)), duplicate=False)


@router.get("", response_model=list[DocumentOut])
async def list_documents(
    tenant: TenantDep,
    container: ContainerDep,
    limit: int = 50,
    offset: int = 0,
    tag: str | None = None,
    collection: str | None = None,
    source_id: UUID | None = None,
    has_source: bool | None = None,
) -> list[DocumentOut]:
    """The workspace's documents, newest first.

    `source_id` narrows to one source's children; `has_source=false` keeps crawled
    and authored pages out of the Files list. Both default to None, and None means
    no clause at all — a caller passing neither gets exactly today's result set.
    """
    rows = await container.documents.list_documents(
        tenant.tenant_id,
        limit,
        offset,
        tag=tag,
        collection=collection,
        source_id=source_id,
        has_source=has_source,
    )
    return [DocumentOut(**_document_fields(row)) for row in rows]


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: UUID, tenant: TenantDep, container: ContainerDep
) -> DocumentOut:
    record = await container.documents.get(tenant.tenant_id, document_id)
    if record is None:
        raise NotFoundError("no such document")
    return DocumentOut(**_document_fields(record))


class DocumentPatch(BaseModel):
    """Editable retrieval facets. Every field is optional; only the ones present
    in the request are written, so a caller can retag without touching the
    collection or metadata. metadata is merged, matching the repo contract."""

    tags: list[str] | None = Field(default=None)
    # present-with-null clears the collection; absent leaves it unchanged
    collection: str | None = Field(default=None)
    metadata: dict[str, Any] | None = Field(default=None)


@router.patch("/{document_id}", response_model=DocumentOut)
async def update_document(
    document_id: UUID, body: DocumentPatch, tenant: TenantDep, container: ContainerDep
) -> DocumentOut:
    """Set a document's tags, collection, or metadata — the retrieval facets.

    Purely a metadata write: it never re-parses or re-indexes, so it is cheap and
    safe to call as often as a tenant edits labels.
    """
    record = await container.documents.get(tenant.tenant_id, document_id)
    if record is None:
        raise NotFoundError("no such document")

    updated = await container.documents.update_metadata(
        tenant.tenant_id,
        document_id,
        tags=body.tags,
        collection=body.collection,
        collection_set="collection" in body.model_fields_set,
        metadata=body.metadata,
    )
    return DocumentOut(**_document_fields(updated))


@router.get("/{document_id}/source")
async def source_url(
    document_id: UUID, tenant: TenantDep, container: ContainerDep
) -> dict[str, str]:
    """Short-lived link to the original file.

    Citations point here, so a user can verify an answer against the source
    rather than taking the quoted chunk on trust.
    """
    record = await container.documents.get(tenant.tenant_id, document_id)
    if record is None:
        raise NotFoundError("no such document")

    from app.adapters.storage.base import document_path

    url = await container.storage.signed_url(document_path(tenant.tenant_id, document_id))
    return {"url": url, "filename": record["filename"]}


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: UUID, tenant: TenantDep, container: ContainerDep
) -> None:
    """Remove a document everywhere it exists.

    Vectors first: a row without vectors is merely incomplete, whereas vectors
    without a row are unattributable and would keep answering questions.
    """
    record = await container.documents.get(tenant.tenant_id, document_id)
    if record is None:
        raise NotFoundError("no such document")

    await container.indexer.remove(tenant.tenant_id, document_id)
    await container.documents.soft_delete(tenant.tenant_id, document_id)

    from app.adapters.storage.base import document_path

    try:
        await container.storage.delete(document_path(tenant.tenant_id, document_id))
    except Exception:
        # the record is gone and the vectors are gone; an orphaned blob is
        # recoverable by a sweep and must not fail the caller's delete
        logger.warning("storage_delete_failed", document_id=str(document_id), exc_info=True)


@router.post("/{document_id}/reindex", status_code=202)
async def reindex(
    document_id: UUID, tenant: TenantDep, container: ContainerDep
) -> dict[str, str]:
    """Re-run ingestion from the stored bytes.

    The point of keeping the original: changing embedding model or enabling OCR
    should not require the tenant to upload anything again.
    """
    record = await container.documents.get(tenant.tenant_id, document_id)
    if record is None:
        raise NotFoundError("no such document")

    from app.adapters.queue.base import Job
    from app.adapters.storage.base import document_path

    # back to queued before the enqueue, so the dashboard's poll shows the retry
    # took effect and the previous failure message is cleared
    await container.documents.set_status(
        tenant.tenant_id, document_id, DocumentStatus.QUEUED, None
    )

    try:
        await container.queue.enqueue(
            Job(
                kind="ingest_document",
                tenant_id=tenant.tenant_id,
                payload={
                    "document_id": str(document_id),
                    "storage_path": document_path(tenant.tenant_id, document_id),
                },
            )
        )
    except Exception as exc:
        logger.error(
            "reindex_enqueue_failed",
            document_id=str(document_id),
            error=str(exc),
            exc_info=True,
        )
        await container.documents.set_status(
            tenant.tenant_id,
            document_id,
            DocumentStatus.FAILED,
            "could not be queued for processing; retry this document",
        )
        raise
    return {"status": "queued", "document_id": str(document_id)}


def _document_fields(row: dict) -> dict:
    """Project a database row onto the wire schema.

    Explicit rather than `**row` so a new internal column is never published by
    accident.
    """
    if row is None:
        raise NotFoundError("no such document")
    return {
        "id": row["id"],
        "filename": row["filename"],
        "mime": row["mime"],
        "size_bytes": row["size_bytes"],
        "status": row["status"],
        "error": row.get("error"),
        "page_count": row.get("page_count", 0),
        "chunk_count": row.get("chunk_count", 0),
        "tier_counts": row.get("tier_counts") or {},
        "tags": row.get("tags") or [],
        "collection": row.get("collection"),
        "metadata": row.get("metadata") or {},
        "created_at": row.get("created_at"),
        "indexed_at": row.get("indexed_at"),
    }
