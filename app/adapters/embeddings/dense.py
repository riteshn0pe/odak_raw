"""Dense embedding providers.

Dimensions are a property of the collection, so switching model means a new
collection plus a background re-embed from the chunks table. Never mutate a
collection in place.
"""

import hashlib
import math
from collections.abc import Sequence

import httpx

from app.core.breaker import guard
from app.core.config import Settings
from app.core.errors import ProviderError
from app.core.logging import get_logger

logger = get_logger(__name__)


class JinaEmbeddings:
    """Jina v3. Matryoshka truncation keeps storage small at little quality cost,
    and task adapters make query and passage embeddings asymmetric."""

    URL = "https://api.jina.ai/v1/embeddings"

    def __init__(self, settings: Settings, model: str = "jina-embeddings-v3", dimensions: int = 512) -> None:
        self._model = model
        self._dimensions = dimensions
        self._client = httpx.AsyncClient(
            timeout=settings.HTTP_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {settings.JINA_API_KEY}"},
        )

    @property
    def model_id(self) -> str:
        return f"{self._model}-{self._dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def _embed(self, texts: Sequence[str], task: str) -> list[list[float]]:
        if not texts:
            return []
        payload = {
            "model": self._model,
            "input": list(texts),
            "dimensions": self._dimensions,
            "task": task,
        }
        # the breaker wraps the mapping too, so what it records is the classified
        # ProviderError rather than the raw httpx exception
        with guard("jina_embeddings"):
            try:
                response = await self._client.post(self.URL, json=payload)
            except httpx.TimeoutException as exc:
                raise ProviderError("jina", "embedding request timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderError("jina", str(exc)) from exc

            if response.status_code != 200:
                raise ProviderError("jina", f"{response.status_code}: {response.text[:200]}")

        data = response.json()["data"]
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        return [item["embedding"] for item in ordered]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts, "retrieval.passage")

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed([text], "retrieval.query")
        return vectors[0] if vectors else []

    async def close(self) -> None:
        await self._client.aclose()


class OpenAIEmbeddings:
    URL = "https://api.openai.com/v1/embeddings"

    def __init__(
        self, settings: Settings, model: str = "text-embedding-3-small", dimensions: int = 512
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        self._client = httpx.AsyncClient(
            timeout=settings.HTTP_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
        )

    @property
    def model_id(self) -> str:
        return f"{self._model}-{self._dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        with guard("openai_embeddings"):
            try:
                response = await self._client.post(
                    self.URL,
                    json={
                        "model": self._model,
                        "input": list(texts),
                        "dimensions": self._dimensions,
                    },
                )
            except httpx.HTTPError as exc:
                raise ProviderError("openai", str(exc)) from exc

            if response.status_code != 200:
                raise ProviderError("openai", f"{response.status_code}: {response.text[:200]}")

        data = sorted(response.json()["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in data]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed([text])
        return vectors[0] if vectors else []

    async def close(self) -> None:
        await self._client.aclose()


class FakeEmbeddings:
    """Deterministic hash embeddings for development and tests.

    Not semantic. Good enough to exercise the pipeline end to end with no key and
    no network, and stable across processes so ingest and query agree.
    """

    def __init__(self, dimensions: int = 256) -> None:
        self._dimensions = dimensions

    @property
    def model_id(self) -> str:
        return f"fake-{self._dimensions}"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _vector(self, text: str) -> list[float]:
        from app.adapters.embeddings.bm25 import tokenize

        vector = [0.0] * self._dimensions
        for token in tokenize(text) or [text.lower()]:
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    async def close(self) -> None:
        return None
