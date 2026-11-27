"""Shared OCR concerns: quality gating and provider fallback.

OCR output is not trustworthy by construction. A provider that fails to read a
page rarely says so — it returns confident garbage: ligature soup, repeated
n-grams, or a wall of control characters. Accepting that into the index is worse
than accepting nothing, because it embeds cleanly and then answers questions
with nonsense. So every result passes a legibility check before it is used.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from app.core.constants import (
    MAX_NGRAM_REPEAT_RATIO,
    MIN_PRINTABLE_RATIO,
    OCR_MIN_CHARS_PER_PAGE,
)
from app.core.logging import get_logger
from app.domain.ports import OCRResult

logger = get_logger(__name__)


def printable_ratio(text: str) -> float:
    """Share of characters that are letters, digits, punctuation, or spaces.

    Failed OCR of a photograph or a heavily skewed scan shows up here as a flood
    of symbol and control categories.
    """
    if not text:
        return 0.0
    good = 0
    for char in text:
        if char.isspace():
            good += 1
            continue
        category = unicodedata.category(char)
        if category[0] in ("L", "N", "P"):
            good += 1
    return good / len(text)


def ngram_repeat_ratio(text: str, size: int = 3) -> float:
    """How much of the text is the same short token repeated.

    A stuck OCR decoder emits runs like "the the the" or a single glyph across
    the page. One dominant n-gram is the signature.
    """
    tokens = text.split()
    if len(tokens) < size * 2:
        return 0.0
    grams: dict[str, int] = {}
    total = 0
    for index in range(len(tokens) - size + 1):
        gram = " ".join(tokens[index : index + size]).lower()
        grams[gram] = grams.get(gram, 0) + 1
        total += 1
    if total == 0:
        return 0.0
    return max(grams.values()) / total


def is_legible(text: str) -> tuple[bool, list[str]]:
    """Decide whether recovered text is worth indexing.

    Returns the verdict plus the reasons, so a rejected page can explain itself
    to the tenant instead of vanishing.
    """
    stripped = text.strip()
    reasons: list[str] = []

    if len(stripped) < OCR_MIN_CHARS_PER_PAGE:
        reasons.append(f"recovered only {len(stripped)} characters")

    ratio = printable_ratio(stripped)
    if stripped and ratio < MIN_PRINTABLE_RATIO:
        reasons.append(f"{(1 - ratio):.0%} of characters are not readable text")

    repeat = ngram_repeat_ratio(stripped)
    if repeat > MAX_NGRAM_REPEAT_RATIO:
        reasons.append(f"{repeat:.0%} of the page is one repeated phrase")

    return (not reasons), reasons


class FallbackOCR:
    """Try providers in order until one returns legible text.

    Ordered cheapest first. A provider that returns illegible text is treated as
    a failure and the next one is tried, because "returned something unusable" and
    "raised an error" have the same consequence for the index.
    """

    def __init__(self, providers: Sequence[object]) -> None:
        if not providers:
            raise ValueError("FallbackOCR needs at least one provider")
        self._providers = list(providers)

    @property
    def name(self) -> str:
        return "+".join(getattr(p, "name", "unknown") for p in self._providers)

    async def extract(self, image: bytes, mime: str = "image/png") -> OCRResult:
        collected: list[str] = []

        for provider in self._providers:
            provider_name = getattr(provider, "name", "unknown")
            try:
                result = await provider.extract(image, mime)  # type: ignore[attr-defined]
            except Exception as exc:
                collected.append(f"{provider_name}: {exc}")
                logger.warning("ocr_provider_failed", provider=provider_name, exc_info=True)
                continue

            legible, reasons = is_legible(result.markdown)
            if legible:
                return result

            collected.append(f"{provider_name}: {'; '.join(reasons)}")
            logger.info("ocr_result_rejected", provider=provider_name, reasons=reasons)

        # every provider failed or produced unusable text. return empty rather
        # than the best of a bad set — the pipeline treats empty as "keep the
        # text the cheap tier already had"
        return OCRResult(markdown="", confidence=0.0, warnings=collected)
