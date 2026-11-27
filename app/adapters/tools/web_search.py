"""Live web search, used to supplement indexed evidence.

An agent with the web-search tool enabled can answer from the open web when the
tenant's own documents do not cover a question. This sits beside retrieval, not
inside the model: results are fetched and handed to the generator as additional
numbered sources, so the existing citation, grounding, and verification path
applies to web sources exactly as it does to document chunks.

Failure is never fatal. A search outage, a missing key, or a bad response yields
an empty list and the answer degrades to document-only — the same posture as a
reranker outage degrading to fusion order.

Provider: Tavily by default — it is built for LLM grounding (returns clean
extracted content, not raw HTML), has a free tier that fits a zero-budget start,
and swaps for another backend by writing one adapter and changing one setting.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.breaker import guard
from app.core.config import Settings
from app.core.errors import CircuitOpenError, ProviderUnavailableError
from app.core.logging import get_logger
from app.domain.entities import WebResult
from app.domain.ports import WebSearchProvider

logger = get_logger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
# web content is long; bound each snippet so a handful of results cannot crowd
# the document evidence out of the prompt budget
MAX_CONTENT_CHARS = 2000


class TavilyWebSearch(WebSearchProvider):
    def __init__(self, api_key: str, timeout: float = 8.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)

    async def search(self, query: str, max_results: int = 5) -> list[WebResult]:
        query = query.strip()
        if not query:
            return []
        try:
            payload = await self._fetch(query, max_results)
        except CircuitOpenError:
            # the breaker already knows this provider is down; skip the wait
            logger.info("web_search_circuit_open")
            return []
        except ProviderUnavailableError as exc:
            # network blip or non-JSON body: degrade to document-only, do not raise
            logger.warning("web_search_failed", error=str(exc))
            return []

        results: list[WebResult] = []
        for item in payload.get("results", []):
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            results.append(
                WebResult(
                    title=str(item.get("title") or url),
                    url=url,
                    content=str(item.get("content", ""))[:MAX_CONTENT_CHARS],
                    score=float(item.get("score", 0.0)),
                )
            )
        return results

    async def _fetch(self, query: str, max_results: int) -> dict[str, Any]:
        """The guarded call. Raises so the breaker sees the outcome; `search`
        turns that back into an empty list, keeping web search non-fatal."""
        with guard("tavily"):
            try:
                response = await self._client.post(
                    TAVILY_URL,
                    json={
                        "api_key": self._api_key,
                        "query": query,
                        "max_results": max_results,
                        "search_depth": "basic",
                        "include_answer": False,
                        "include_raw_content": False,
                    },
                )
                if response.status_code != 200:
                    raise ProviderUnavailableError(
                        "tavily", f"http {response.status_code}"
                    )
                return dict(response.json())
            except (httpx.HTTPError, ValueError) as exc:
                raise ProviderUnavailableError("tavily", str(exc)) from exc

    async def close(self) -> None:
        await self._client.aclose()


def build_web_search(settings: Settings) -> WebSearchProvider | None:
    """Web search from settings, or None when unconfigured.

    A None provider is not an error: an agent with the tool enabled simply gets
    no web evidence, exactly as an unconfigured OCR chain leaves scans degraded.
    """
    backend = (settings.WEB_SEARCH_BACKEND or "").lower()
    if backend in ("", "none"):
        return None
    if backend == "tavily":
        if not settings.TAVILY_API_KEY:
            logger.info("web_search_unconfigured", backend=backend)
            return None
        return TavilyWebSearch(settings.TAVILY_API_KEY)
    logger.warning("web_search_unknown_backend", backend=backend)
    return None
