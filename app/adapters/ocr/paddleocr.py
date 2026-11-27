"""PaddleOCR-VL via Baidu's hosted endpoint. The platform default.

Chosen because it leads the open field on document parsing (~94.5 on
OmniDocBench v1.5 at ~0.9B parameters) while being served, so no GPU is needed
to start. The self-hosted variant below runs the same model on a serverless GPU
and returns identical output — the switch is a config change, not a rewrite.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from app.core.breaker import guard
from app.core.errors import (
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.core.logging import get_logger
from app.domain.ports import OCRResult

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.baidu.com/paddleocr/v1"


def _classify(provider: str, status: int, body: str) -> Exception:
    snippet = body[:300]
    if status == 429:
        return ProviderRateLimitError(provider, f"rate limited: {snippet}")
    if status in (500, 502, 503):
        return ProviderUnavailableError(provider, f"upstream error {status}: {snippet}")
    if status == 504:
        return ProviderTimeoutError(provider, f"gateway timeout: {snippet}")
    return ProviderUnavailableError(provider, f"{status}: {snippet}")


class PaddleOCRProvider:
    """Hosted PaddleOCR-VL.

    The endpoint returns markdown directly, which is what the chunker wants, so
    no layout reconstruction is needed on our side.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = "PaddleOCR-VL",
        timeout: float = 120.0,
        name: str = "paddleocr_hosted",
    ) -> None:
        self._model = model
        self._name = name
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
        return self._name

    async def extract(self, image: bytes, mime: str = "image/png") -> OCRResult:
        payload: dict[str, Any] = {
            "model": self._model,
            "image": base64.b64encode(image).decode(),
            "output_format": "markdown",
        }

        with guard(self._name):
            try:
                response = await self._client.post("/ocr", json=payload)
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(self._name, "ocr request timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(self._name, str(exc)) from exc

            if response.status_code >= 400:
                raise _classify(self._name, response.status_code, response.text)

        data = response.json()
        markdown = data.get("markdown") or data.get("text") or ""
        confidence = float(data.get("confidence", 1.0))

        return OCRResult(markdown=markdown, confidence=confidence)

    async def close(self) -> None:
        await self._client.aclose()


def paddleocr_selfhost(base_url: str, api_key: str = "", **kwargs: Any) -> PaddleOCRProvider:
    """Same model behind a self-hosted inference server.

    Worth switching to when sustained page volume makes per-token cost exceed the
    GPU's hourly rate. Outputs are identical, so no re-indexing is implied.
    """
    return PaddleOCRProvider(
        api_key or "unused", base_url=base_url, name="paddleocr_selfhost", **kwargs
    )
