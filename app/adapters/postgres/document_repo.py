"""Document, chunk, and job repositories.

Every method opens a `tenant_scope` transaction, which sets `app.tenant_id` with
`SET LOCAL` so the row-level security policies apply. That is the isolation
boundary: a query that forgets its WHERE clause returns nothing rather than
another tenant's rows.

Canonical chunk text lives here, not in the vector store. Re-embedding with a new
model is then a read from this table — never a re-parse or a re-OCR, which are
the expensive steps.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.constants import DocumentStatus, JobState, JobStep
from app.core.db import tenant_scope
from app.core.logging import get_logger
from app.domain.entities import Chunk, Document, ParsedDocument

logger = get_logger(__name__)


class DocumentRepository:
    """Implements the interface `DocumentPipeline` expects."""

    def __init__(self, storage: Any = None) -> None:
        # optional object store for the raw upload. absent in dev, where the
        # bytes are simply not retained
        self._storage = storage

    async def create(
        self,
        tenant_id: UUID,
        filename: str,
        mime: str,
        size_bytes: int,
        sha256: str,
        storage_path: str,
        agent_id: UUID | None = None,
        page_count: int = 0,
        metadata: dict[str, Any] | None = None,
        source: str = "upload",
        source_id: UUID | None = None,
        source_url: str | None = None,
    ) -> Document:
        document_id = uuid4()
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO documents (
                        id, tenant_id, agent_id, filename, mime, size_bytes,
                        sha256, storage_path, status, page_count, metadata,
                        source, source_id, source_url
                    ) VALUES (
                        :id, :tenant_id, :agent_id, :filename, :mime, :size_bytes,
                        :sha256, :storage_path, :status, :page_count, CAST(:metadata AS jsonb),
                        :source, :source_id, :source_url
                    )
                    """
                ),
                {
                    "id": document_id,
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "filename": filename,
                    "mime": mime,
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                    "storage_path": storage_path,
                    "status": DocumentStatus.QUEUED.value,
                    "page_count": page_count,
                    "metadata": json.dumps(metadata or {}),
                    # a crawled page, snippet or Q&A pair records its parent and its
                    # live URL here; an upload keeps the defaults it always had
                    "source": source,
                    "source_id": source_id,
                    "source_url": source_url,
                },
            )
            # the job row is created in the same transaction as the document, so
            # a document can never exist with no way to advance it
            await session.execute(
                text(
                    """
                    INSERT INTO document_jobs (tenant_id, document_id, step, state)
                    VALUES (:tenant_id, :document_id, :step, :state)
                    ON CONFLICT (document_id) DO UPDATE
                        SET step = EXCLUDED.step, state = EXCLUDED.state, attempt = 0,
                            error = NULL, updated_at = now()
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "document_id": document_id,
                    "step": JobStep.VALIDATE.value,
                    "state": JobState.PENDING.value,
                },
            )

        return Document(
            id=document_id,
            tenant_id=tenant_id,
            filename=filename,
            mime=mime,
            size_bytes=size_bytes,
            sha256=sha256,
            storage_path=storage_path,
            status=DocumentStatus.QUEUED,
            agent_id=agent_id,
            page_count=page_count,
            metadata=metadata or {},
        )

    async def replace_content(
        self,
        tenant_id: UUID,
        document_id: UUID,
        *,
        sha256: str,
        size_bytes: int,
        mime: str | None = None,
        page_count: int | None = None,
    ) -> None:
        """Point an existing document at new bytes after a refetch.

        `update_metadata` cannot do this — it writes only tags, collection and the
        metadata bag — so before this existed, a recrawl overwrote a document's
        bytes in storage while `documents.sha256` kept the OLD hash. `find_by_hash`
        then missed on every subsequent tick, and a page that changed once was
        re-fetched, re-stored and fully re-indexed forever.

        The document id is deliberately kept: chunks, citations already written
        into message history, and `source_links.document_id` all point at it, and
        replacing the row would orphan every one of them. Status returns to
        `queued` and the job row is reset, because the new bytes have not been
        parsed, chunked or indexed yet.
        """
        sets = [
            "sha256 = :sha256",
            "size_bytes = :size_bytes",
            "status = :status",
            "error = NULL",
            "indexed_at = NULL",
        ]
        params: dict[str, Any] = {
            "id": document_id,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "status": DocumentStatus.QUEUED.value,
        }
        if mime is not None:
            sets.append("mime = :mime")
            params["mime"] = mime
        if page_count is not None:
            sets.append("page_count = :page_count")
            params["page_count"] = page_count

        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    f"UPDATE documents SET {', '.join(sets)} "  # noqa: S608 - fixed set, no input
                    "WHERE id = :id AND status <> 'deleted'"
                ),
                params,
            )
            # same transaction as the content change, for the same reason create()
            # writes the job row inline: a document must never exist in `queued`
            # with no job to advance it
            await session.execute(
                text(
                    """
                    INSERT INTO document_jobs (tenant_id, document_id, step, state)
                    VALUES (:tenant_id, :document_id, :step, :state)
                    ON CONFLICT (document_id) DO UPDATE
                        SET step = EXCLUDED.step, state = EXCLUDED.state, attempt = 0,
                            error = NULL, updated_at = now()
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "document_id": document_id,
                    "step": JobStep.VALIDATE.value,
                    "state": JobState.PENDING.value,
                },
            )

    async def find_by_hash(self, tenant_id: UUID, sha256: str) -> dict[str, Any] | None:
        """Dedup lookup, scoped to the tenant.

        Deliberately not global: two tenants uploading the same file are
        independent, and a cross-tenant match would leak that the file exists.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, status, chunk_count FROM documents "
                        "WHERE sha256 = :sha256 AND status <> 'deleted' LIMIT 1"
                    ),
                    {"sha256": sha256},
                )
            ).mappings().first()
        return dict(row) if row else None

    async def store_raw(
        self, tenant_id: UUID, document_id: UUID, data: bytes, mime: str
    ) -> None:
        if self._storage is None:
            return
        path = f"tenants/{tenant_id}/documents/{document_id}"
        await self._storage.put(path, data, mime)

    async def store_parsed(
        self, tenant_id: UUID, document_id: UUID, parsed: ParsedDocument
    ) -> None:
        """Persist parsed markdown and the tier breakdown.

        Parsing is the step that costs money, so its output is written before
        chunking. A retry after this point never pays for OCR twice.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    """
                    UPDATE documents
                       SET page_count  = :page_count,
                           tier_counts = CAST(:tier_counts AS jsonb),
                           metadata    = metadata || CAST(:metadata AS jsonb)
                     WHERE id = :document_id
                    """
                ),
                {
                    "document_id": document_id,
                    "page_count": parsed.page_count,
                    "tier_counts": json.dumps(parsed.tier_counts()),
                    "metadata": json.dumps(
                        {
                            "parsed_title": parsed.metadata.get("title", ""),
                            "degraded_pages": [
                                page.page_number for page in parsed.pages if page.degraded
                            ],
                        }
                    ),
                },
            )

    async def store_chunks(
        self, tenant_id: UUID, document_id: UUID, chunks: list[Chunk]
    ) -> int:
        if not chunks:
            return 0

        async with tenant_scope(tenant_id) as session:
            # replace rather than append. deterministic ids mean a retry produces
            # the same rows, and the delete keeps a re-index from doubling them
            await session.execute(
                text("DELETE FROM chunks WHERE document_id = :document_id"),
                {"document_id": document_id},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO chunks (
                        id, tenant_id, document_id, ordinal, kind, text,
                        parent_text, parent_id, heading_path, page_from, page_to,
                        token_count, metadata
                    ) VALUES (
                        :id, :tenant_id, :document_id, :ordinal, :kind, :text,
                        :parent_text, :parent_id, :heading_path, :page_from, :page_to,
                        :token_count, CAST(:metadata AS jsonb)
                    )
                    """
                ),
                [
                    {
                        "id": chunk.id,
                        "tenant_id": tenant_id,
                        "document_id": document_id,
                        "ordinal": chunk.ordinal,
                        "kind": chunk.kind.value,
                        "text": chunk.text,
                        "parent_text": chunk.parent_text,
                        "parent_id": chunk.parent_id,
                        "heading_path": chunk.heading_path,
                        "page_from": chunk.page_from,
                        "page_to": chunk.page_to,
                        "token_count": chunk.token_count,
                        "metadata": json.dumps(chunk.metadata),
                    }
                    for chunk in chunks
                ],
            )
            await session.execute(
                text("UPDATE documents SET chunk_count = :count WHERE id = :document_id"),
                {"count": len(chunks), "document_id": document_id},
            )
        return len(chunks)

    async def get_chunks(
        self, tenant_id: UUID, chunk_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Hydrate retrieval hits with canonical text.

        The vector store holds a preview; the full text lives here, so a long
        parent chunk is never truncated by a payload size limit.
        """
        if not chunk_ids:
            return {}
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, document_id, text, parent_text, heading_path, "
                        "page_from, page_to, kind FROM chunks WHERE id = ANY(:ids)"
                    ),
                    {"ids": chunk_ids},
                )
            ).mappings().all()
        return {row["id"]: dict(row) for row in rows}

    async def set_status(
        self,
        tenant_id: UUID,
        document_id: UUID,
        status: DocumentStatus,
        error: str | None = None,
    ) -> None:
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    """
                    UPDATE documents
                       SET status     = :status,
                           error      = :error,
                           indexed_at = CASE WHEN :status = 'ready' THEN now() ELSE indexed_at END
                     WHERE id = :document_id
                    """
                ),
                {"status": status.value, "error": error, "document_id": document_id},
            )

    async def mark_step(
        self,
        tenant_id: UUID,
        document_id: UUID,
        step: JobStep,
        state: JobState,
        elapsed_ms: int = 0,
        error: str | None = None,
    ) -> None:
        """Record step progress so a retry resumes rather than restarting.

        `attempt` only increments on failure — a successful step advancing the
        machine is not a retry.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    """
                    UPDATE document_jobs
                       SET step       = :step,
                           state      = :state,
                           error      = :error,
                           attempt    = CASE WHEN :state = 'failed' THEN attempt + 1 ELSE attempt END,
                           cursor     = cursor || CAST(:cursor AS jsonb),
                           updated_at = now()
                     WHERE document_id = :document_id
                    """
                ),
                {
                    "step": step.value,
                    "state": state.value,
                    "error": error,
                    "cursor": json.dumps({f"{step.value}_ms": elapsed_ms}),
                    "document_id": document_id,
                },
            )

    async def record_usage(
        self,
        tenant_id: UUID,
        document_id: UUID,
        chunk_count: int = 0,
        token_count: int = 0,
        cost_units: dict[str, int] | None = None,
    ) -> None:
        """Append billable quantities.

        OCR pages are recorded separately from free pages because they are the
        one ingestion step with real marginal cost.
        """
        units = cost_units or {}
        events: list[dict[str, Any]] = [
            {
                "tenant_id": tenant_id,
                "kind": "embed_token",
                "quantity": token_count,
                "meta": json.dumps({"document_id": str(document_id), "chunks": chunk_count}),
            }
        ]
        if units.get("free_pages"):
            events.append(
                {
                    "tenant_id": tenant_id,
                    "kind": "ingest_page",
                    "quantity": units["free_pages"],
                    "meta": json.dumps({"document_id": str(document_id), "tier": "free"}),
                }
            )
        if units.get("ocr_pages"):
            events.append(
                {
                    "tenant_id": tenant_id,
                    "kind": "ocr_page",
                    "quantity": units["ocr_pages"],
                    "meta": json.dumps({"document_id": str(document_id)}),
                }
            )

        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO usage_events (tenant_id, kind, quantity, meta) "
                    "VALUES (:tenant_id, :kind, :quantity, CAST(:meta AS jsonb))"
                ),
                events,
            )

    async def list_documents(
        self,
        tenant_id: UUID,
        limit: int = 50,
        offset: int = 0,
        tag: str | None = None,
        collection: str | None = None,
        source_id: UUID | None = None,
        has_source: bool | None = None,
    ) -> list[dict[str, Any]]:
        # tag/collection are optional narrowing filters. Absent, the query is the
        # original unfiltered list, so existing callers see no change.
        clauses = ["status <> 'deleted'"]
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if tag:
            clauses.append(":tag = ANY(tags)")
            params["tag"] = tag
        if collection:
            clauses.append("collection = :collection")
            params["collection"] = collection
        if source_id is not None:
            clauses.append("source_id = :source_id")
            params["source_id"] = source_id
        # `is False` rather than truthiness: has_source=false is the whole point of
        # the filter — it is what keeps crawled pages out of the Files list — and a
        # falsy check would silently turn it into a no-op
        if has_source is False:
            clauses.append("source_id IS NULL")
        elif has_source is True:
            clauses.append("source_id IS NOT NULL")
        where = " AND ".join(clauses)
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, filename, mime, size_bytes, status, error, "
                        "page_count, chunk_count, tier_counts, tags, collection, "
                        "metadata, created_at, indexed_at "
                        f"FROM documents WHERE {where} "
                        "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def update_metadata(
        self,
        tenant_id: UUID,
        document_id: UUID,
        tags: list[str] | None = None,
        collection: str | None = None,
        collection_set: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Partial update of a document's retrieval facets.

        Each field updates only when supplied, so a caller can set tags without
        disturbing collection or metadata. metadata is merged (`||`) rather than
        replaced, matching how parsed metadata is accumulated at ingest — a caller
        clears a key by writing it null, not by omitting it. `collection_set`
        distinguishes "leave collection alone" from "clear it to NULL".
        """
        sets: list[str] = []
        params: dict[str, Any] = {"id": document_id}
        if tags is not None:
            sets.append("tags = :tags")
            params["tags"] = tags
        if collection_set:
            sets.append("collection = :collection")
            params["collection"] = collection
        if metadata is not None:
            sets.append("metadata = metadata || CAST(:metadata AS jsonb)")
            params["metadata"] = json.dumps(metadata)
        if not sets:
            return await self.get(tenant_id, document_id)
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    f"UPDATE documents SET {', '.join(sets)} "
                    "WHERE id = :id AND status <> 'deleted'"
                ),
                params,
            )
        return await self.get(tenant_id, document_id)

    async def resolve_document_ids(
        self,
        tenant_id: UUID,
        tags: list[str] | None = None,
        collection: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[UUID]:
        """Document ids matching document-level facets, for retrieval scoping.

        Cheap SQL run before search: the matching ids are passed to the retriever
        as its existing `document_ids` restriction. This is deliberately not a
        chunk-payload filter — tags/collection/metadata are mutable after indexing,
        so resolving live from Postgres never goes stale the way a copied payload
        would. Returns every non-deleted document when no facet is given.
        """
        clauses = ["status <> 'deleted'"]
        params: dict[str, Any] = {}
        if tags:
            clauses.append("tags && CAST(:tags AS text[])")
            params["tags"] = tags
        if collection:
            clauses.append("collection = :collection")
            params["collection"] = collection
        if metadata:
            clauses.append("metadata @> CAST(:metadata AS jsonb)")
            params["metadata"] = json.dumps(metadata)
        where = " AND ".join(clauses)
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(f"SELECT id FROM documents WHERE {where}"), params
                )
            ).mappings().all()
        return [row["id"] for row in rows]

    async def get(self, tenant_id: UUID, document_id: UUID) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text("SELECT * FROM documents WHERE id = :id"), {"id": document_id}
                )
            ).mappings().first()
        return dict(row) if row else None

    async def get_agent_config(
        self, tenant_id: UUID, agent_id: UUID
    ) -> dict[str, Any] | None:
        """The agent's config blob, for ingestion-time decisions.

        Ingestion runs on a queue worker with no request context, so it reads the
        config directly rather than through the request-scoped cache in `deps`.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text("SELECT config FROM agents WHERE id = :id"), {"id": agent_id}
                )
            ).mappings().first()
        return dict(row["config"]) if row and row["config"] else None

    async def soft_delete(self, tenant_id: UUID, document_id: UUID) -> None:
        """Mark deleted and drop the chunks.

        Vector removal is the caller's job — it owns the collection name, which
        depends on the embedding model in use.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text("DELETE FROM chunks WHERE document_id = :id"), {"id": document_id}
            )
            await session.execute(
                text("UPDATE documents SET status = 'deleted' WHERE id = :id"),
                {"id": document_id},
            )

    async def usage_totals(self, tenant_id: UUID) -> dict[str, int]:
        """Current consumption, for quota checks at the gate."""
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) AS documents, "
                        "COALESCE(SUM(size_bytes), 0) AS storage_bytes "
                        "FROM documents WHERE status <> 'deleted'"
                    )
                )
            ).mappings().first()
        return {
            "documents": int(row["documents"]) if row else 0,
            "storage_bytes": int(row["storage_bytes"]) if row else 0,
        }
