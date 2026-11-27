"""Cross-encoder reranking.

Hybrid search is recall-oriented: it casts wide and fuses two rankings that
never saw each other. A reranker is the precision step — it reads the query and
each candidate *together*, which a bi-encoder cannot do, and reorders on actual
relevance rather than vector proximity.

This is the last stage before evidence reaches the model, so its failure mode
matters: a reranker outage must degrade ranking quality, never fail the answer.
Callers already treat an exception as "keep fusion order", and these adapters
preserve that by raising rather than returning something half-ranked.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from app.core.breaker import guard
from app.core.config import Settings
from app.core.errors import (
    ConfigError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.core.logging import get_logger
from app.domain.entities import RetrievedChunk
from app.domain.ports import RerankProvider

logger = get_logger(__name__)

# a reranker reads every candidate in full, so cost scales with text sent.
# truncating each document bounds that without losing the passage's topic
MAX_DOC_CHARS = 4000


def _classify(provider: str, status: int, body: str) -> Exception:
    snippet = body[:300]
    if status == 429:
        return ProviderRateLimitError(provider, f"rate limited: {snippet}")
    if status in (500, 502, 503, 529):
        return ProviderUnavailableError(provider, f"upstream error {status}: {snippet}")
    if status == 504:
        return ProviderTimeoutError(provider, f"gateway timeout: {snippet}")
    return ProviderUnavailableError(provider, f"{status}: {snippet}")


def _document_text(chunk: RetrievedChunk) -> str:
    """What the reranker scores.

    The heading path is prepended because a passage lifted out of its section
    often loses the noun the query is about — "the limit is 30 days" scores far
    better as "Refunds > Eligibility: the limit is 30 days".
    """
    heading = " > ".join(chunk.heading_path) if chunk.heading_path else ""
    body = chunk.context_text.strip()[:MAX_DOC_CHARS]
    return f"{heading}: {body}" if heading else body


class CohereReranker:
    """Cohere Rerank. Strong multilingual behaviour and a generous free tier."""

    def __init__(
        self,
        api_key: str,
        model: str = "rerank-v3.5",
        *,
        base_url: str = "https://api.cohere.com/v2",
        timeout: float = 30.0,
    ) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
        )

    @property
    def name(self) -> str:
        return f"cohere:{self._model}"

    async def rerank(
        self, query: str, documents: Sequence[RetrievedChunk], top_n: int
    ) -> list[RetrievedChunk]:
        if not documents:
            return []

        payload = {
            "model": self._model,
            "query": query,
            "documents": [_document_text(chunk) for chunk in documents],
            "top_n": min(top_n, len(documents)),
        }

        # a rerank outage degrades ranking rather than failing the answer, so the
        # breaker's job here is to stop paying the timeout on every query while
        # the vendor is down — the caller already treats a raise as "keep fusion
        # order", and CircuitOpenError is just a faster raise
        with guard("cohere_rerank"):
            try:
                response = await self._client.post("/rerank", json=payload)
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(self.name, "rerank timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(self.name, str(exc)) from exc

            if response.status_code >= 400:
                raise _classify(self.name, response.status_code, response.text)

        results = response.json().get("results") or []
        ranked: list[RetrievedChunk] = []
        for entry in results:
            index = int(entry.get("index", -1))
            if 0 <= index < len(documents):
                chunk = documents[index]
                # overwrite the fusion score. downstream confidence gating reads
                # chunk.score, and a relevance score is the better signal
                chunk.score = float(entry.get("relevance_score", chunk.score))
                ranked.append(chunk)
        return ranked

    async def close(self) -> None:
        await self._client.aclose()


class JinaReranker:
    """Jina Rerank. Same contract, different vendor — kept so a Cohere outage or
    quota exhaustion is a config change rather than an incident."""

    def __init__(
        self,
        api_key: str,
        model: str = "jina-reranker-v2-base-multilingual",
        *,
        base_url: str = "https://api.jina.ai/v1",
        timeout: float = 30.0,
    ) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
        )

    @property
    def name(self) -> str:
        return f"jina:{self._model}"

    async def rerank(
        self, query: str, documents: Sequence[RetrievedChunk], top_n: int
    ) -> list[RetrievedChunk]:
        if not documents:
            return []

        payload = {
            "model": self._model,
            "query": query,
            "documents": [_document_text(chunk) for chunk in documents],
            "top_n": min(top_n, len(documents)),
        }

        with guard("jina_rerank"):
            try:
                response = await self._client.post("/rerank", json=payload)
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(self.name, "rerank timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(self.name, str(exc)) from exc

            if response.status_code >= 400:
                raise _classify(self.name, response.status_code, response.text)

        results = response.json().get("results") or []
        ranked: list[RetrievedChunk] = []
        for entry in results:
            index = int(entry.get("index", -1))
            if 0 <= index < len(documents):
                chunk = documents[index]
                chunk.score = float(entry.get("relevance_score", chunk.score))
                ranked.append(chunk)
        return ranked

    async def close(self) -> None:
        await self._client.aclose()


def build_reranker(settings: Settings) -> RerankProvider | None:
    """None is a valid outcome — retrieval then keeps fusion order.

    Hybrid + RRF is already a good ordering, so a deployment without a reranker
    is degraded rather than broken.
    """
    backend = settings.RERANK_BACKEND

    if backend == "none":
        return None

    if backend == "cohere":
        if not settings.COHERE_API_KEY:
            logger.warning("rerank_disabled", reason="COHERE_API_KEY not set")
            return None
        return CohereReranker(settings.COHERE_API_KEY)  # type: ignore[return-value]

    if backend == "jina":
        if not settings.JINA_API_KEY:
            logger.warning("rerank_disabled", reason="JINA_API_KEY not set")
            return None
        return JinaReranker(settings.JINA_API_KEY)  # type: ignore[return-value]

    raise ConfigError(f"unknown rerank backend {backend!r}")
