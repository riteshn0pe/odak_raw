"""OCR provider construction.

The default chain is PaddleOCR-VL first, a vision model second. That ordering is
the cost lever: the dedicated model is cheaper per page and better at plain
scans, and the vision model is only reached when the cheap one produced
something illegible.
"""

from __future__ import annotations

from typing import Any

from app.adapters.ocr.base import FallbackOCR
from app.adapters.ocr.paddleocr import PaddleOCRProvider, paddleocr_selfhost
from app.adapters.ocr.vlm import AnthropicVisionOCR, OpenAIVisionOCR, gemini_vision
from app.core.config import Settings
from app.core.errors import ConfigError
from app.core.logging import get_logger
from app.domain.ports import OCRProvider

logger = get_logger(__name__)


def build_ocr(backend: str, settings: Settings, **options: Any) -> OCRProvider:
    match backend:
        case "paddleocr_hosted":
            if not settings.PADDLEOCR_API_KEY:
                raise ConfigError("PADDLEOCR_API_KEY required for paddleocr_hosted")
            base_url = settings.PADDLEOCR_BASE_URL or None
            return PaddleOCRProvider(
                settings.PADDLEOCR_API_KEY,
                **({"base_url": base_url} if base_url else {}),
                **options,
            )

        case "paddleocr_selfhost":
            if not settings.PADDLEOCR_BASE_URL:
                raise ConfigError("PADDLEOCR_BASE_URL required for paddleocr_selfhost")
            return paddleocr_selfhost(
                settings.PADDLEOCR_BASE_URL, settings.PADDLEOCR_API_KEY, **options
            )

        case "anthropic":
            if not settings.ANTHROPIC_API_KEY:
                raise ConfigError("ANTHROPIC_API_KEY required for anthropic ocr")
            model = options.pop("model", settings.OCR_VISION_MODEL)
            return AnthropicVisionOCR(settings.ANTHROPIC_API_KEY, model, **options)

        case "gemini":
            if not settings.GEMINI_API_KEY:
                raise ConfigError("GEMINI_API_KEY required for gemini ocr")
            model = options.pop("model", settings.OCR_VISION_MODEL)
            return gemini_vision(settings.GEMINI_API_KEY, model, **options)

        case "openai":
            if not settings.OPENAI_API_KEY:
                raise ConfigError("OPENAI_API_KEY required for openai ocr")
            model = options.pop("model", settings.OCR_VISION_MODEL)
            return OpenAIVisionOCR(settings.OPENAI_API_KEY, model, **options)

        case "none":
            raise ConfigError("ocr backend is disabled")

        case _:
            raise ConfigError(f"unknown ocr backend {backend!r}")


def build_ocr_chain(settings: Settings) -> OCRProvider | None:
    """Build the configured chain, skipping providers with no credentials.

    Returns None when nothing is configured. That is a valid deployment: the
    pipeline then keeps whatever text the free tier recovered and marks scanned
    pages degraded, rather than failing the document outright.
    """
    backends = [name.strip() for name in settings.OCR_BACKENDS.split(",") if name.strip()]
    providers: list[OCRProvider] = []

    for backend in backends:
        try:
            providers.append(build_ocr(backend, settings))
        except ConfigError as exc:
            # a missing key means "not configured", not "broken". log and skip so
            # a partial chain still works
            logger.info("ocr_backend_skipped", backend=backend, reason=str(exc))

    if not providers:
        logger.warning("ocr_disabled", detail="no ocr backend configured; scans stay degraded")
        return None
    if len(providers) == 1:
        return providers[0]
    return FallbackOCR(providers)  # type: ignore[return-value]
