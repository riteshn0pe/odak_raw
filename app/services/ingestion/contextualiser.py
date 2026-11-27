"""Contextual retrieval.

A chunk pulled out of its document loses the context that made it meaningful.
"The limit is 30 days" is unfindable by "refund window" once it is severed from
the section it lived in. Anthropic's contextual retrieval fixes this by
prepending a short, model-written blurb that situates each chunk in its source,
and embedding *that* — reported to cut top-20 retrieval failures by around a
third on its own, more with sparse search and reranking on top.

The cost is one LLM call per chunk at index time, which is why this is a
per-tenant tier feature rather than always on. Three things keep the cost down:
the cheapest model the tenant has, a bounded fan-out, and a cache keyed by chunk
content so a re-index pays nothing.

Only `embed_text` is changed. The canonical `text` the model reads at answer
time is never touched, so a bad blurb can hurt recall but can never corrupt an
answer.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from app.core.constants import OCR_MAX_CONCURRENCY
from app.core.logging import get_logger
from app.domain.entities import Chunk

logger = get_logger(__name__)

# the document prefix the model reads to situate a chunk. capped because the
# whole point is a cheap call, and a 300-page contract would blow the budget
MAX_DOCUMENT_CHARS = 8000

CONTEXT_PROMPT = (
    "Here is a document, then one chunk taken from it. Write a short standalone "
    "sentence (at most 25 words) that situates the chunk within the document — "
    "what section or topic it belongs to and what it is about — so the chunk can "
    "be found on its own. Reply with only that sentence, no preamble.\n\n"
    "<document>\n{document}\n</document>\n\n"
    "<chunk>\n{chunk}\n</chunk>"
)


class Contextualiser:
    def __init__(self, llm: Any, cache: Any = None, max_concurrency: int = OCR_MAX_CONCURRENCY) -> None:
        self._llm = llm
        self._cache = cache
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def contextualise(self, document_markdown: str, chunks: list[Chunk]) -> list[Chunk]:
        """Fill each chunk's `embed_text` with `context + text`.

        Best-effort per chunk: a failed or empty blurb leaves that chunk to embed
        on its plain text, which is exactly the non-contextual behaviour. One
        provider hiccup must not fail a whole document's ingestion.
        """
        if not chunks:
            return chunks

        # the prefix of the document is enough to situate a chunk, and it is the
        # part most likely to name the document and its opening sections
        document = document_markdown[:MAX_DOCUMENT_CHARS]

        async def one(chunk: Chunk) -> None:
            blurb = await self._blurb(document, chunk)
            if blurb:
                chunk.embed_text = f"{blurb}\n\n{chunk.text}"

        await asyncio.gather(*(one(chunk) for chunk in chunks))

        contextualised = sum(1 for chunk in chunks if chunk.embed_text)
        logger.info(
            "contextualised", total=len(chunks), contextualised=contextualised
        )
        return chunks

    async def _blurb(self, document: str, chunk: Chunk) -> str:
        # keyed by document prefix + chunk text, so a re-index with the same
        # content is a cache hit and costs nothing
        cache_key = None
        if self._cache is not None:
            digest = hashlib.sha256(f"{document}\x00{chunk.text}".encode()).hexdigest()
            cache_key = f"ctx:{digest}"
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached

        from app.adapters.llm.base import Message

        prompt = CONTEXT_PROMPT.format(document=document, chunk=chunk.text)
        async with self._semaphore:
            try:
                completion = await self._llm.complete(
                    [
                        Message(role="system", content="You write concise retrieval context."),
                        Message(role="user", content=prompt),
                    ],
                    max_tokens=80,
                )
            except Exception:
                # degrade to plain-text embedding for this chunk
                logger.warning("contextualise_failed", chunk_id=chunk.id, exc_info=True)
                return ""

        blurb = completion.text.strip()
        if cache_key is not None and blurb:
            # a document rarely changes, so a long ttl is safe and maximises reuse
            await self._cache.set(cache_key, blurb, ttl_seconds=CONTEXT_CACHE_TTL_SECONDS)
        return blurb


CONTEXT_CACHE_TTL_SECONDS = 30 * 24 * 3600
