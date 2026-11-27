"""Indexing: parsed document to searchable vectors.

Two invariants that make replay safe:
  deterministic ids   a re-run overwrites the same points instead of duplicating
  no orphans          any failure deletes every vector for that document before
                      the job is marked failed, so a retry starts from clean state

A document is only marked ready after the upsert is verified by count. A
partially indexed document is never queryable, because silent partial success is
worse than a visible failure.
"""

from dataclasses import dataclass
from uuid import UUID

from app.core.constants import ChunkKind, ParseTier
from app.core.logging import get_logger
from app.domain.chunking import (
    build_parent_child_chunks,
    build_structured_chunks,
    estimate_tokens,
)
from app.domain.entities import Chunk, ParsedDocument
from app.domain.ports import EmbeddingProvider, SparseEncoder, VectorRecord, VectorStore

logger = get_logger(__name__)


@dataclass(slots=True)
class IndexResult:
    chunk_count: int
    vector_count: int
    token_count: int


def collection_name(prefix: str, embedding_model: str) -> str:
    """One collection per embedding model.

    Dimensions are fixed at collection creation, so a model change means a new
    collection and a background re-embed, never an in place mutation.
    """
    safe = embedding_model.replace("/", "_").replace(":", "_").replace(".", "_")
    return f"{prefix}_{safe}"


def has_structure(parsed: ParsedDocument) -> bool:
    """Heading markers mean the structured splitter will produce better bounds
    than a blind character split."""
    return any(line.startswith("#") for line in parsed.markdown.split("\n"))


def build_chunks(
    tenant_id: UUID, document_id: UUID, parsed: ParsedDocument, parent_child: bool = True
) -> list[Chunk]:
    """Choose a strategy from the shape of the content, then attach page numbers.

    Page attribution matters because a citation has to point at a real location
    a user can open, not just a document.
    """
    if has_structure(parsed):
        chunks = build_structured_chunks(tenant_id, document_id, parsed.markdown)
    elif parent_child:
        chunks = build_parent_child_chunks(tenant_id, document_id, parsed.markdown)
    else:
        chunks = build_structured_chunks(tenant_id, document_id, parsed.markdown)

    _attach_pages(chunks, parsed)
    return chunks


def _attach_pages(chunks: list[Chunk], parsed: ParsedDocument) -> None:
    """Map chunk text back to the pages it came from by walking offsets.

    Cheap and approximate at the boundaries, which is acceptable: a citation that
    is one page off is still useful, a citation with no page is not.
    """
    if not parsed.pages:
        return

    spans: list[tuple[int, int, int]] = []
    cursor = 0
    for page in parsed.pages:
        length = len(page.markdown)
        spans.append((cursor, cursor + length, page.page_number))
        cursor += length + 2  # the join in ParsedDocument.markdown

    full = parsed.markdown
    search_from = 0
    for chunk in chunks:
        probe = chunk.text[:120]
        position = full.find(probe, search_from)
        if position == -1:
            position = full.find(probe)
        if position == -1:
            chunk.page_from = chunk.page_to = parsed.pages[0].page_number
            continue

        end = position + len(chunk.text)
        search_from = position + 1
        touched = [number for start, stop, number in spans if start < end and stop > position]
        if touched:
            chunk.page_from, chunk.page_to = touched[0], touched[-1]
        else:
            chunk.page_from = chunk.page_to = parsed.pages[0].page_number


class Indexer:
    def __init__(
        self,
        vector_store: VectorStore,
        embeddings: EmbeddingProvider,
        sparse: SparseEncoder | None,
        collection_prefix: str,
        batch_size: int = 32,
    ) -> None:
        self._store = vector_store
        self._embeddings = embeddings
        self._sparse = sparse
        self._prefix = collection_prefix
        self._batch_size = batch_size

    @property
    def collection(self) -> str:
        return collection_name(self._prefix, self._embeddings.model_id)

    async def ensure_ready(self) -> None:
        await self._store.ensure_collection(self.collection, self._embeddings.dimensions)

    async def index(
        self,
        tenant_id: UUID,
        document_id: UUID,
        chunks: list[Chunk],
        filename: str,
        extra_payload: dict[str, object] | None = None,
    ) -> IndexResult:
        if not chunks:
            return IndexResult(0, 0, 0)

        await self.ensure_ready()

        # replace rather than append. re-indexing an updated document must not
        # leave the previous version's vectors behind
        await self._store.delete_by_document(self.collection, tenant_id, document_id)

        total_vectors = 0
        try:
            for start in range(0, len(chunks), self._batch_size):
                batch = chunks[start : start + self._batch_size]
                texts = [chunk.text_to_embed for chunk in batch]

                dense = await self._embeddings.embed_documents(texts)
                sparse_vectors = (
                    await self._sparse.encode_documents(texts) if self._sparse else [None] * len(batch)
                )

                records = [
                    VectorRecord(
                        id=chunk.id,
                        tenant_id=tenant_id,
                        dense=dense[index],
                        sparse=sparse_vectors[index],
                        payload={
                            "document_id": str(document_id),
                            "chunk_id": chunk.id,
                            "parent_id": chunk.parent_id,
                            "kind": chunk.kind.value,
                            "ordinal": chunk.ordinal,
                            "filename": filename,
                            "heading_path": chunk.heading_path,
                            "page_from": chunk.page_from,
                            "page_to": chunk.page_to,
                            # text_preview keeps a retrieval hit renderable even
                            # if the postgres lookup is skipped
                            "text_preview": chunk.text[:500],
                            **(extra_payload or {}),
                        },
                    )
                    for index, chunk in enumerate(batch)
                ]
                total_vectors += await self._store.upsert(self.collection, records)

            # verify before declaring success. a silent partial upsert would make
            # the document look ready while answering from half its content
            stored = await self._store.count(self.collection, tenant_id, document_id)
            if stored < len(chunks):
                raise RuntimeError(
                    f"upsert verification failed: expected {len(chunks)}, stored {stored}"
                )

        except Exception:
            logger.error(
                "index_failed_cleaning_up",
                tenant_id=str(tenant_id),
                document_id=str(document_id),
                exc_info=True,
            )
            await self._surgical_delete(tenant_id, document_id)
            raise

        token_count = sum(chunk.token_count or estimate_tokens(chunk.text) for chunk in chunks)
        logger.info(
            "index_complete",
            tenant_id=str(tenant_id),
            document_id=str(document_id),
            chunks=len(chunks),
            vectors=total_vectors,
        )
        return IndexResult(len(chunks), total_vectors, token_count)

    async def _surgical_delete(self, tenant_id: UUID, document_id: UUID) -> None:
        """Remove only this document's vectors. No other document is touched."""
        try:
            await self._store.delete_by_document(self.collection, tenant_id, document_id)
        except Exception:
            logger.error("orphan_cleanup_failed", document_id=str(document_id), exc_info=True)

    async def remove(self, tenant_id: UUID, document_id: UUID) -> None:
        await self._store.delete_by_document(self.collection, tenant_id, document_id)


def tier_cost_units(parsed: ParsedDocument) -> dict[str, int]:
    """Billable ingestion units by tier. Free tiers are counted but cost zero."""
    counts = parsed.tier_counts()
    return {
        "free_pages": counts.get(ParseTier.FAST.value, 0)
        + counts.get(ParseTier.STRUCTURED.value, 0),
        "ocr_pages": counts.get(ParseTier.OCR.value, 0)
        + counts.get(ParseTier.VLM.value, 0)
        + counts.get(ParseTier.OCR_FALLBACK.value, 0),
    }
