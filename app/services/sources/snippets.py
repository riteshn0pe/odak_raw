"""Text snippets: a `sources` row plus exactly one markdown document.

There is no snippet table and no snippet ingest path. A snippet *is* a small
markdown file, so it goes through the machinery a file upload already uses:
`sniff_mime` resolves `text/markdown` from the filename, `TextParser` passes
markdown through untouched, `has_structure` picks the structured splitter when the
body has headings, and `/reindex`, `/documents/{id}/source`, tags, collections,
citations and quota all work with no special case.

Editing replaces the bytes and calls `replace_content` — the same path a recrawled
page that changed takes — because a re-index needs the new bytes and the *same*
document id: chunks, `source_links.document_id` and citations already written into
message history all point at it.

The body is data, never instruction. It reaches an LLM prompt inside the evidence
block, which is already rendered as delimited numbered documents, so an incoming
`<script>` or "ignore previous instructions" is stored and shown verbatim as
markdown text rather than sanitised into something the tenant did not write.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from app.adapters.queue.base import Job
from app.adapters.storage.base import document_path
from app.core.constants import DocumentStatus
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

MARKDOWN_MIME = "text/markdown"

# a snippet is authored in a textarea. half a megabyte of markdown is ~80k words,
# far past anything typed by hand, and a tenant with more than that has a file to
# upload instead — which is a better path for it anyway
SNIPPET_MAX_BYTES = 512 * 1024

# the filename is what a citation shows, so it carries the title rather than an id
SNIPPET_FILENAME_MAX = 120


class SnippetService:
    """Create and edit text snippets. Constructed with the container."""

    def __init__(self, container: Any) -> None:
        self._container = container

    @property
    def _sources(self) -> Any:
        return self._container.sources

    @property
    def _documents(self) -> Any:
        return self._container.documents

    async def create(
        self,
        tenant_id: UUID,
        *,
        title: str,
        body: str,
        tags: list[str] | None = None,
        collection: str | None = None,
        agent_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Create the parent source, store the bytes, queue the ordinary ingest."""
        clean_title, data = _validate(title, body)

        source: dict[str, Any] = await self._sources.create(
            tenant_id,
            kind="snippet",
            title=clean_title,
            tags=tags,
            collection=collection,
            agent_id=agent_id,
            enqueue=False,
        )
        source_id = UUID(str(source["id"]))

        document = await self._documents.create(
            tenant_id=tenant_id,
            filename=_filename(clean_title),
            mime=MARKDOWN_MIME,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            storage_path="",
            agent_id=agent_id,
            metadata={"snippet": True},
            source="snippet",
            source_id=source_id,
        )
        document_id = UUID(str(document.id))

        path = document_path(tenant_id, document_id)
        await self._container.storage.put(path, data, MARKDOWN_MIME)
        await self._enqueue(tenant_id, document_id, path)

        source["document_id"] = str(document_id)
        logger.info("snippet_created", source_id=str(source_id), bytes=len(data))
        return source

    async def update(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        title: str | None = None,
        body: str | None = None,
        tags: list[str] | None = None,
        collection: str | None = None,
        collection_set: bool = False,
    ) -> dict[str, Any]:
        """Replace a snippet's bytes and re-index, keeping the document id.

        `replace_content` is what makes the hash on the row match the bytes in the
        store. Without it, `find_by_hash` misses forever and every later edit
        re-ingests content that never changed.
        """
        document = await self._child(tenant_id, source_id)

        source: dict[str, Any] = await self._sources.update(
            tenant_id,
            source_id,
            title=title,
            tags=tags,
            collection=collection,
            collection_set=collection_set,
        )

        if body is not None:
            clean_title, data = _validate(title or str(source.get("title") or "snippet"), body)
            document_id = UUID(str(document["id"]))
            path = document_path(tenant_id, document_id)
            await self._container.storage.put(path, data, MARKDOWN_MIME)
            await self._documents.replace_content(
                tenant_id,
                document_id,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                mime=MARKDOWN_MIME,
            )
            await self._enqueue(tenant_id, document_id, path)
            source["document_id"] = str(document_id)
            logger.info(
                "snippet_updated", source_id=str(source_id), title=clean_title, bytes=len(data)
            )

        return source

    async def read(self, tenant_id: UUID, source_id: UUID) -> dict[str, Any]:
        """The editor's GET: the parent row plus the markdown body from storage."""
        document = await self._child(tenant_id, source_id)
        document_id = UUID(str(document["id"]))
        try:
            raw = await self._container.storage.get(document_path(tenant_id, document_id))
        except Exception:
            logger.warning("snippet_body_missing", document_id=str(document_id), exc_info=True)
            raw = b""
        return {
            "document_id": str(document_id),
            "body": raw.decode("utf-8", errors="replace"),
        }

    # ----------------------------------------------------------------- internals

    async def _child(self, tenant_id: UUID, source_id: UUID) -> dict[str, Any]:
        """The snippet's single document row.

        Listed rather than stored on the source, because `documents.source_id` is
        already the authority for "which documents belong to this source" and a
        second pointer would be a second thing to keep consistent.
        """
        rows = await self._documents.list_documents(tenant_id, limit=1, source_id=source_id)
        if not rows:
            raise NotFoundError("snippet not found")
        return dict(rows[0])

    async def _enqueue(self, tenant_id: UUID, document_id: UUID, path: str) -> None:
        """Queue the ordinary `ingest_document` job.

        The bytes are already durable, so a queue outage marks the document failed
        with the reason rather than losing the snippet: the dashboard's Retry
        button re-enqueues exactly this job, which is what `documents.py:264` does
        for an upload.
        """
        try:
            await self._container.queue.enqueue(
                Job(
                    kind="ingest_document",
                    tenant_id=tenant_id,
                    payload={"document_id": str(document_id), "storage_path": path},
                )
            )
        except Exception as exc:
            logger.error("snippet_enqueue_failed", document_id=str(document_id), error=str(exc))
            await self._documents.set_status(
                tenant_id, document_id, DocumentStatus.FAILED, "could not queue indexing"
            )


def _validate(title: str, body: str) -> tuple[str, bytes]:
    """Trim, check and encode. Raises `ValidationError` for a 422."""
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValidationError("title is required")

    text = (body or "").strip()
    if not text:
        raise ValidationError("the snippet body is empty")

    data = text.encode("utf-8")
    if len(data) > SNIPPET_MAX_BYTES:
        raise ValidationError(
            f"a snippet is limited to {SNIPPET_MAX_BYTES // 1024} KB; upload a file instead"
        )
    return clean_title, data


def _filename(title: str) -> str:
    """`title.md`, because the extension is the mime hint the parser registry uses."""
    stem = title[:SNIPPET_FILENAME_MAX].strip() or "snippet"
    return stem if stem.lower().endswith(".md") else f"{stem}.md"
