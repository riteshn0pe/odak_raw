"""Vision-model OCR. The escalation tier above PaddleOCR.

Two jobs. It is the fallback when the dedicated OCR model returns illegible text,
and it is the primary path for `ParseTier.VLM` pages — dense tables and complex
layouts, where reading order matters more than character accuracy and a
general-purpose vision model reconstructs structure better than a line-oriented
OCR engine.

The prompt is deliberately narrow. A vision model asked to "describe this page"
will summarise it; we need transcription, and summarisation silently loses the
facts a RAG answer must cite.
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

TRANSCRIBE_PROMPT = """Transcribe this page to markdown. Rules:

- Reproduce all text exactly. Do not summarise, explain, or comment.
- Render tables as markdown tables, preserving row and column structure.
- Use markdown headings for headings, matching the visual hierarchy.
- Keep the original reading order, including multi-column layouts.
- Preserve numbers, dates, currency amounts, and identifiers character for character.
- If part of the page is illegible, write [illegible] in place of that text only.
- Output nothing but the transcription."""

ANTHROPIC_VERSION = "2023-06-01"


def _classify(provider: str, status: int, body: str) -> Exception:
    snippet = body[:300]
    if status == 429:
        return ProviderRateLimitError(provider, f"rate limited: {snippet}")
    if status in (500, 502, 503, 529):
        return ProviderUnavailableError(provider, f"upstream error {status}: {snippet}")
    if status == 504:
        return ProviderTimeoutError(provider, f"gateway timeout: {snippet}")
    return ProviderUnavailableError(provider, f"{status}: {snippet}")


class AnthropicVisionOCR:
    """Transcription via the Anthropic Messages API.

    Sampling parameters are omitted deliberately: current models reject them, and
    transcription wants the model's default behaviour anyway.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://api.anthropic.com/v1",
        max_tokens: int = 8192,
        timeout: float = 180.0,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )

    @property
    def name(self) -> str:
        return f"anthropic_vision:{self._model}"

    async def extract(self, image: bytes, mime: str = "image/png") -> OCRResult:
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": base64.b64encode(image).decode(),
                            },
                        },
                        {"type": "text", "text": TRANSCRIBE_PROMPT},
                    ],
                }
            ],
        }

        # breaker keyed on the provider's own name, so the OCR chain's fallback
        # (see build_ocr_chain) skips to the next backend fast while one vendor is
        # down instead of paying the vision timeout on every scanned page
        with guard(self.name):
            try:
                response = await self._client.post("/messages", json=payload)
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(self.name, "transcription timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(self.name, str(exc)) from exc

            if response.status_code >= 400:
                raise _classify(self.name, response.status_code, response.text)

        data = response.json()
        warnings: list[str] = []

        # a refusal is a successful response with no usable content. check it
        # before reading content, or an index error masks the real reason
        if data.get("stop_reason") == "refusal":
            logger.warning("vlm_ocr_refused", model=self._model)
            return OCRResult(
                markdown="", confidence=0.0, warnings=["the vision model declined this page"]
            )

        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )

        if data.get("stop_reason") == "max_tokens":
            warnings.append("page transcription hit the output limit and may be cut short")

        return OCRResult(markdown=text, confidence=1.0 if text.strip() else 0.0, warnings=warnings)

    async def close(self) -> None:
        await self._client.aclose()


class OpenAIVisionOCR:
    """Transcription via any chat-completions endpoint that accepts image_url.

    Covers OpenAI, Gemini's compatibility layer, and OpenRouter — the wire format
    is identical, so one adapter serves all three.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        max_tokens: int = 8192,
        timeout: float = 180.0,
        name: str | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._name = name or f"openai_vision:{model}"
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
        data_url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": TRANSCRIBE_PROMPT},
                    ],
                }
            ],
        }

        with guard(self._name):
            try:
                response = await self._client.post("/chat/completions", json=payload)
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(self._name, "transcription timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(self._name, str(exc)) from exc

            if response.status_code >= 400:
                raise _classify(self._name, response.status_code, response.text)

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return OCRResult(markdown="", confidence=0.0, warnings=["provider returned no output"])

        choice = choices[0]
        text = (choice.get("message") or {}).get("content") or ""
        warnings: list[str] = []
        if choice.get("finish_reason") == "length":
            warnings.append("page transcription hit the output limit and may be cut short")

        return OCRResult(markdown=text, confidence=1.0 if text.strip() else 0.0, warnings=warnings)

    async def close(self) -> None:
        await self._client.aclose()


def gemini_vision(api_key: str, model: str, **kwargs: Any) -> GeminiVisionOCR:
    """Gemini through its native GenerateContent surface.

    The platform key is scoped to the Vertex AI publisher endpoint, which the
    AI Studio OpenAI-compatibility layer refuses; speaking generateContent
    directly also carries the image inline as a part, which is how the model's
    own docs send vision input. Long context and low per-image cost make it the
    practical default for bulk page transcription.
    """
    return GeminiVisionOCR(api_key, model, **kwargs)


class GeminiVisionOCR:
    """Transcription via ``{model}:generateContent`` with an inline image."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://aiplatform.googleapis.com/v1/publishers/google/models",
        max_tokens: int = 8192,
        timeout: float = 180.0,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"x-goog-api-key": api_key, "content-type": "application/json"},
        )

    @property
    def name(self) -> str:
        return f"gemini_vision:{self._model}"

    async def extract(self, image: bytes, mime: str = "image/png") -> OCRResult:
        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": mime,
                                "data": base64.b64encode(image).decode(),
                            }
                        },
                        {"text": TRANSCRIBE_PROMPT},
                    ],
                }
            ],
            "generationConfig": {"maxOutputTokens": self._max_tokens},
        }

        # breaker keyed on the provider's own name, so the OCR chain's fallback
        # (see build_ocr_chain) skips to the next backend fast while one vendor is
        # down instead of paying the vision timeout on every scanned page
        with guard(self.name):
            try:
                response = await self._client.post(
                    f"/{self._model}:generateContent", json=payload
                )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(self.name, "transcription timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(self.name, str(exc)) from exc

            if response.status_code >= 400:
                raise _classify(self.name, response.status_code, response.text)

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            reason = f" (prompt blocked: {blocked})" if blocked else ""
            return OCRResult(markdown="", confidence=0.0, warnings=[f"provider returned no output{reason}"])

        candidate = candidates[0]
        text = "".join(
            part["text"]
            for part in (candidate.get("content") or {}).get("parts") or []
            if isinstance(part.get("text"), str)
        )
        warnings: list[str] = []
        if candidate.get("finishReason") == "MAX_TOKENS":
            warnings.append("page transcription hit the output limit and may be cut short")

        return OCRResult(markdown=text, confidence=1.0 if text.strip() else 0.0, warnings=warnings)

    async def close(self) -> None:
        await self._client.aclose()
