"""LLM adapter base: retry, fallback, and cost accounting.

Every provider fails differently. This layer turns provider specific failures
into one taxonomy so the agent above it never branches on vendor.

Two rules that shape the design:

Retry only what can succeed on a second attempt. A rate limit or a 503 is
transient. A malformed request or a refused prompt is not, and retrying it
burns quota to arrive at the same answer.

A fallback chain must cross providers. Retrying a second model inside the same
provider during that provider's outage fails the same way.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import (
    LLMError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.core.logging import get_logger
from app.domain.ports import LLMProvider

logger = get_logger(__name__)

# transient. worth a second attempt
RETRYABLE = (ProviderRateLimitError, ProviderTimeoutError, ProviderUnavailableError)


@dataclass(slots=True)
class Message:
    role: str
    content: str

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
        )


@dataclass(slots=True)
class Completion:
    text: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    finish_reason: str = "stop"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def was_truncated(self) -> bool:
        """Truncated output is a silent failure mode.

        The text looks complete, so it reaches the user mid sentence unless the
        caller checks. Surfaced as a property rather than buried in the reason.
        """
        return self.finish_reason in ("length", "max_tokens")


async def with_retry(
    operation: Any,
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    label: str = "llm",
) -> Any:
    """Exponential backoff with jitter, retrying transient failures only.

    Jitter matters under load: without it, every request that hit the same rate
    limit retries at the same instant and re-triggers it.
    """
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except RETRYABLE as exc:
            last = exc
            if attempt == attempts:
                break
            # a rate limit that tells us when to come back beats our guess
            hinted = getattr(exc, "retry_after", None)
            delay = float(hinted) if hinted else min(base_delay * 2 ** (attempt - 1), max_delay)
            delay += random.uniform(0, delay * 0.25)
            logger.warning(
                "llm_retry",
                label=label,
                attempt=attempt,
                of=attempts,
                delay_s=round(delay, 2),
                error=type(exc).__name__,
            )
            await asyncio.sleep(delay)
        except LLMError:
            # permanent. a second identical request gets the same answer
            raise

    assert last is not None
    raise last


class FallbackLLM(LLMProvider):
    """Tries each provider in order, moving on when one is unavailable.

    Ordered cheapest-capable first, so the expensive model is what degradation
    costs rather than the default. Only provider level failures advance the
    chain: a refused prompt or an oversized request fails the same way
    everywhere, so it raises immediately instead of paying three times.
    """

    def __init__(self, providers: Sequence[LLMProvider]) -> None:
        if not providers:
            raise ValueError("fallback chain needs at least one provider")
        self._providers = list(providers)

    @property
    def model_id(self) -> str:
        return self._providers[0].model_id

    @property
    def provider_name(self) -> str:
        return self._providers[0].provider_name

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Completion:
        """`tools` is named rather than left to **kwargs on purpose.

        It used to ride in the catch all, where a provider whose signature did
        not mention it swallowed it silently and the model answered as though no
        tool existed. Naming it makes the chain forward it deliberately.
        """
        errors: list[str] = []

        for index, provider in enumerate(self._providers):
            try:
                completion: Completion = await with_retry(
                    lambda p=provider: p.complete(messages, tools=tools, **kwargs),
                    label=provider.provider_name,
                )
                return completion
            except RETRYABLE as exc:
                errors.append(f"{provider.provider_name}: {exc}")
                remaining = len(self._providers) - index - 1
                logger.warning(
                    "llm_provider_exhausted",
                    provider=provider.provider_name,
                    remaining=remaining,
                    error=type(exc).__name__,
                )
                if remaining == 0:
                    raise ProviderUnavailableError(
                        "fallback_chain",
                        "every provider in the fallback chain failed",
                        attempted=errors,
                    ) from exc

        raise ProviderUnavailableError(
            "fallback_chain", "no provider produced a completion"
        )

    async def stream(self, messages: Sequence[Message], **kwargs: Any) -> AsyncIterator[str]:
        """Fall back only before the first token.

        Once bytes have reached the client, switching providers would restart the
        answer mid sentence. After that point the stream fails visibly instead.
        """
        for index, provider in enumerate(self._providers):
            started = False
            try:
                async for token in provider.stream(messages, **kwargs):
                    started = True
                    yield token
                return
            except RETRYABLE as exc:
                if started or index == len(self._providers) - 1:
                    raise
                logger.warning(
                    "llm_stream_fallback",
                    provider=provider.provider_name,
                    error=type(exc).__name__,
                )
                continue
