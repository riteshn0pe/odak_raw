"""Retrieval: query to grounded context.

Hybrid by default. Dense catches paraphrase, sparse catches exact tokens a user
copies verbatim (order ids, error codes, section numbers). Either alone loses a
whole class of query, which is why this is the default rather than an upsell.

Everything here takes tenant_id as a required argument. There is no code path
that can search without one.

Two kinds of filter meet here, and they are not interchangeable:

  document facets   `tags`, `collection`, `metadata` — properties of a document,
                    editable after indexing. Resolved live from Postgres into a
                    document-id restriction, because a copy of them in the chunk
                    payload would be stale the moment a tenant retags.
  payload filters   anything else in `RetrievalConfig.filters` — matched against
                    the chunk payload in the vector store, where the field was
                    written at index time and cannot drift.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.core.constants import ChunkKind
from app.core.logging import get_logger
from app.domain.agent_config import RetrievalConfig
from app.domain.chunking import dedupe_key
from app.domain.entities import Citation, RetrievedChunk
from app.domain.ports import (
    EmbeddingProvider,
    RerankProvider,
    SparseEncoder,
    VectorStore,
)

logger = get_logger(__name__)

# Keys in RetrievalConfig.filters that name a document property rather than a
# chunk payload field. Kept in one place so the split is not re-derived per call
# site — the Documents API writes exactly these three (see DocumentPatch).
DOCUMENT_FACET_KEYS = frozenset({"tags", "collection", "metadata"})

# A facet that matches this many documents is no longer a useful restriction and
# the id list starts to dominate the vector-store request. It is still applied —
# dropping it would silently widen the search — but it is logged, because it
# usually means the agent's facet filter wants narrowing.
BROAD_FACET_WARN_AT = 500


def split_filters(
    filters: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate document facets from chunk-payload filters.

    Empty values are dropped rather than treated as a restriction: a config that
    carries ``{"collection": null}`` from a cleared form field means "no
    collection filter", not "documents whose collection is null".
    """
    facets: dict[str, Any] = {}
    payload: dict[str, Any] = {}
    for key, value in filters.items():
        if value in (None, "", [], {}):
            continue
        if key in DOCUMENT_FACET_KEYS:
            facets[key] = value
        else:
            payload[key] = value
    return facets, payload


@dataclass(slots=True)
class RetrievalResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    confidence: float = 0.0
    strategy: str = "hybrid"

    @property
    def is_empty(self) -> bool:
        return not self.chunks

    def context_block(self, max_chars: int = 12000) -> str:
        """Render retrieved text for the prompt.

        Every chunk is delimited and numbered so the model can cite by index, and
        so document content is visibly data rather than instructions.
        """
        parts: list[str] = []
        used = 0
        for index, chunk in enumerate(self.chunks, start=1):
            heading = " > ".join(chunk.heading_path) if chunk.heading_path else chunk.filename
            body = chunk.context_text.strip()
            block = (
                f"[{index}] source: {chunk.filename}"
                f"{f' | section: {heading}' if heading else ''}"
                f" | page: {chunk.page_from}\n{body}"
            )
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
        return "\n\n---\n\n".join(parts)


class RetrievalService:
    def __init__(
        self,
        vector_store: VectorStore,
        embeddings: EmbeddingProvider,
        sparse: SparseEncoder | None = None,
        reranker: RerankProvider | None = None,
        collection_prefix: str = "chunks",
        documents: Any | None = None,
    ) -> None:
        self._store = vector_store
        self._embeddings = embeddings
        self._sparse = sparse
        self._reranker = reranker
        self._prefix = collection_prefix
        # optional on purpose: without a document repo the facet keys are ignored
        # rather than silently applied as payload filters, and every other path
        # (tests, an eval harness) still constructs the service with three args
        self._documents = documents

    @property
    def collection(self) -> str:
        from app.services.ingestion.indexer import collection_name

        return collection_name(self._prefix, self._embeddings.model_id)

    async def _facet_document_ids(
        self, tenant_id: UUID, facets: dict[str, Any]
    ) -> list[UUID] | None:
        """Document ids matching the config's facets, or None when not applicable.

        None means "no facet restriction" — either none were configured or there
        is no document repo to resolve them against. An empty list is a real
        answer and means the facets matched nothing.
        """
        if not facets or self._documents is None:
            if facets:
                logger.warning("facet_filters_ignored_no_document_repo", keys=sorted(facets))
            return None

        tags = facets.get("tags")
        if isinstance(tags, str):
            tags = [tags]
        collection = facets.get("collection")
        metadata = facets.get("metadata")
        if not isinstance(metadata, dict):
            metadata = None

        ids = await self._documents.resolve_document_ids(
            tenant_id,
            tags=list(tags) if tags else None,
            collection=str(collection) if collection else None,
            metadata=metadata,
        )
        if len(ids) >= BROAD_FACET_WARN_AT:
            logger.warning(
                "facet_filter_very_broad",
                tenant_id=str(tenant_id),
                matched=len(ids),
                keys=sorted(facets),
            )
        return list(ids)

    async def search(
        self,
        tenant_id: UUID,
        query: str,
        config: RetrievalConfig,
        document_ids: list[UUID] | None = None,
    ) -> RetrievalResult:
        query = query.strip()
        if not query:
            return RetrievalResult(strategy="empty_query")

        facets, payload_filters = split_filters(config.filters)
        facet_ids = await self._facet_document_ids(tenant_id, facets)

        # the caller's explicit document_ids and the config's facets are both
        # restrictions, so they intersect. an empty intersection is a real answer:
        # searching without the restriction would return documents the tenant
        # deliberately scoped out
        if facet_ids is not None:
            allowed = (
                set(facet_ids) & set(document_ids) if document_ids else set(facet_ids)
            )
            if not allowed:
                logger.info(
                    "retrieval_facets_matched_nothing",
                    tenant_id=str(tenant_id),
                    keys=sorted(facets),
                )
                return RetrievalResult(strategy=f"{config.mode}+facets_empty")
            effective_ids: list[UUID] | None = sorted(allowed)
        else:
            effective_ids = document_ids

        filters: dict[str, object] = dict(payload_filters)
        if effective_ids:
            filters["document_id"] = [str(value) for value in effective_ids]

        # dense and sparse are independent encodings of the same query; running
        # them together hides whichever is slower. Today's sparse encoder is a
        # local bm25 (near-free), so the gain is small until a network sparse
        # encoder is swapped in — but the gather costs nothing and is correct.
        async def _dense() -> Any:
            return await self._embeddings.embed_query(query)

        async def _sparse() -> Any:
            if config.mode in ("hybrid", "sparse") and self._sparse is not None:
                return await self._sparse.encode_query(query)
            return None

        dense, sparse = await asyncio.gather(_dense(), _sparse())
        # over fetch, because dedupe and rerank both discard candidates
        fetch_limit = max(config.top_k * 2, config.top_k + 10)

        hits = await self._store.search_hybrid(
            self.collection,
            tenant_id,
            dense,
            sparse,
            limit=fetch_limit,
            filters=filters or None,
        )
        if not hits:
            return RetrievalResult(strategy=config.mode)

        chunks = [self._to_chunk(hit) for hit in hits]
        chunks = self._collapse(chunks)

        if config.rerank_enabled and self._reranker is not None and len(chunks) > 1:
            try:
                chunks = await self._reranker.rerank(query, chunks, config.rerank_top_n)
                strategy = f"{config.mode}+rerank"
            except Exception:
                # a reranker outage degrades ranking quality, it must not fail
                # the request. fusion order is already a good ordering
                logger.warning("rerank_failed_falling_back", exc_info=True)
                chunks = chunks[: config.rerank_top_n]
                strategy = f"{config.mode}+rerank_unavailable"
        else:
            chunks = chunks[: config.max_context_chunks]
            strategy = config.mode

        chunks = chunks[: config.max_context_chunks]
        confidence = self._confidence(chunks)

        return RetrievalResult(
            chunks=chunks,
            citations=self._citations(chunks),
            confidence=confidence,
            strategy=strategy,
        )

    def _to_chunk(self, hit: object) -> RetrievedChunk:
        payload = hit.payload  # type: ignore[attr-defined]
        raw_document = payload.get("document_id")
        return RetrievedChunk(
            chunk_id=str(payload.get("chunk_id") or hit.id),  # type: ignore[attr-defined]
            document_id=UUID(raw_document) if raw_document else UUID(int=0),
            score=float(hit.score),  # type: ignore[attr-defined]
            text=str(payload.get("text_preview", "")),
            parent_text=payload.get("parent_text"),
            heading_path=list(payload.get("heading_path") or []),
            page_from=int(payload.get("page_from", 0)),
            page_to=int(payload.get("page_to", 0)),
            filename=str(payload.get("filename", "")),
            metadata={
                "kind": payload.get("kind"),
                "parent_id": payload.get("parent_id"),
                "ordinal": payload.get("ordinal"),
            },
        )

    def _collapse(self, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Collapse sibling children to one entry per parent.

        Without this, several fragments of one paragraph crowd out every other
        document and the model sees one source instead of many.
        """
        seen: dict[str, RetrievedChunk] = {}
        for chunk in chunks:
            kind_value = chunk.metadata.get("kind")
            kind = ChunkKind(kind_value) if kind_value else ChunkKind.STRUCTURED
            key = dedupe_key(chunk.chunk_id, chunk.metadata.get("parent_id"), kind)
            existing = seen.get(key)
            if existing is None or chunk.score > existing.score:
                seen[key] = chunk
        return sorted(seen.values(), key=lambda item: item.score, reverse=True)

    def _confidence(self, chunks: list[RetrievedChunk]) -> float:
        """Top score, normalised.

        RRF scores are small and unbounded below 1, so they are rescaled against
        the theoretical best rank rather than used raw.
        """
        if not chunks:
            return 0.0
        top = chunks[0].score
        if top <= 0:
            return 0.0
        if top <= 1.0:
            # rrf range. first place in both lists is about 2/(k+1)
            from app.core.constants import RRF_K

            best = 2.0 / (RRF_K + 1)
            return min(top / best, 1.0)
        return min(top, 1.0)

    def _citations(self, chunks: list[RetrievedChunk]) -> list[Citation]:
        citations: list[Citation] = []
        seen: set[str] = set()
        for chunk in chunks:
            key = f"{chunk.document_id}:{chunk.page_from}"
            if key in seen:
                continue
            seen.add(key)
            citations.append(
                Citation(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    filename=chunk.filename,
                    page_from=chunk.page_from,
                    page_to=chunk.page_to,
                    heading_path=chunk.heading_path,
                )
            )
        return citations


def validate_citations(
    answer: str, allowed: list[Citation]
) -> tuple[list[Citation], list[int]]:
    """Keep only citations the model actually referenced, drop invented ones.

    A citation index the model made up would render as a broken link, so it is
    removed and reported rather than shown.
    """
    import re

    referenced = {int(match) for match in re.findall(r"\[(\d+)\]", answer)}
    kept: list[Citation] = []
    invalid: list[int] = []

    for index in sorted(referenced):
        if 1 <= index <= len(allowed):
            kept.append(allowed[index - 1])
        else:
            invalid.append(index)
    return kept, invalid
