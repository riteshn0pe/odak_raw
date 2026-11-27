"""Anthropic provider.

Raw HTTP rather than the SDK: this codebase already speaks httpx to every other
provider, and one transport is easier to reason about than two.

Model IDs are config, never hardcoded here. Current Claude models take
`thinking: {type: "adaptive"}` and reject `budget_tokens`, `temperature`,
`top_p`, and `top_k`, so those are not sent at all.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.adapters.llm.base import Completion, Message, Usage
from app.core.breaker import guard
from app.core.errors import (
    ContextLengthError,
    LLMError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

API_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"


def _split_system(messages: Sequence[Message]) -> tuple[str | None, list[dict[str, str]]]:
    """Anthropic takes the system prompt as a top level field, not a message."""
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []

    for message in messages:
        if message.role == "system":
            system_parts.append(message.content)
        else:
            turns.append({"role": message.role, "content": message.content})

    return ("\n\n".join(system_parts) if system_parts else None), turns


def _classify(status: int, body: str) -> Exception:
    """Map a provider status onto the shared taxonomy.

    The distinction that matters is retryable vs not. A 429 or 5xx is worth
    another attempt; a 400 means the request itself is wrong and a retry only
    spends quota to reach the same answer.
    """
    snippet = body[:400]
    if status == 429:
        return ProviderRateLimitError("anthropic", f"rate limited: {snippet}")
    if status in (500, 502, 503, 529):
        return ProviderUnavailableError("anthropic", f"upstream error {status}: {snippet}")
    if status == 504:
        return ProviderTimeoutError("anthropic", f"gateway timeout: {snippet}")
    if status == 413 or "context" in body.lower() and "long" in body.lower():
        return ContextLengthError(f"request exceeds context window: {snippet}")
    return LLMError(f"anthropic {status}: {snippet}")


class AnthropicProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 120.0,
        max_tokens: int = 4096,
        thinking: bool = False,
        effort: str | None = None,
        prompt_caching: bool = False,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._thinking = thinking
        self._effort = effort
        # when on, the system prompt is sent as a cacheable content block. the
        # platform's prompt layout already keeps the system prompt static across
        # turns (evidence rides on the final user turn precisely for this), so
        # marking it costs one breakpoint and discounts every repeat prefill.
        self._prompt_caching = prompt_caching
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_id(self) -> str:
        return self._model

    @staticmethod
    def _tools(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalise the internal tool list to Anthropic's wire shape.

        Internal shape already is Anthropic's — {"name", "description",
        "input_schema"} — so this is mostly a pass through. It also unwraps an
        OpenAI style {"type": "function", "function": {...}} entry, because a
        caller that fetched a definition from a vendor native source should not
        have to know which provider is about to receive it.
        """
        normalised: list[dict[str, Any]] = []
        for tool in tools:
            source = tool.get("function") if tool.get("type") == "function" else tool
            if not isinstance(source, dict):
                continue
            entry: dict[str, Any] = {
                "name": source["name"],
                "input_schema": source.get("input_schema")
                or source.get("parameters")
                or {"type": "object", "properties": {}},
            }
            description = source.get("description")
            if description:
                entry["description"] = description
            normalised.append(entry)
        return normalised

    def _payload(
        self,
        messages: Sequence[Message],
        max_tokens: int | None,
        stream: bool,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        system, turns = _split_system(messages)
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens or self._max_tokens,
            "messages": turns,
        }
        if system:
            if self._prompt_caching:
                # a content block with a cache_control marker caches everything
                # up to and including it; the system prompt is the static prefix
                # shared by every turn of a conversation
                payload["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                payload["system"] = system
        # absent means absent. the api rejects an empty tools array, and a request
        # carrying tools: [] is a different request from one carrying none
        if tools:
            payload["tools"] = self._tools(tools)
        if stream:
            payload["stream"] = True
        if self._thinking:
            # adaptive lets the model decide depth per request. budget_tokens is
            # rejected on current models
            payload["thinking"] = {"type": "adaptive"}
        if self._effort:
            payload["output_config"] = {"effort": self._effort}
        return payload

    async def complete(
        self,
        messages: Sequence[Message],
        max_tokens: int | None = None,
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        **_: Any,
    ) -> Completion:
        # the breaker spans the request and its status classification, so an
        # outage trips it while a refusal or an oversized prompt does not
        with guard("anthropic"):
            try:
                response = await self._client.post(
                    "/v1/messages",
                    json=self._payload(messages, max_tokens, stream=False, tools=tools),
                )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError("anthropic", "request timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError("anthropic", str(exc)) from exc

            if response.status_code >= 400:
                raise _classify(response.status_code, response.text)

        data = response.json()

        # a safety decline arrives as a 200 with stop_reason refusal and possibly
        # empty content, so stop_reason is checked before content is indexed
        stop_reason = data.get("stop_reason", "")
        blocks = data.get("content", [])
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")

        if stop_reason == "refusal":
            details = data.get("stop_details") or {}
            raise LLMError(
                "the model declined this request",
                provider="anthropic",
                category=details.get("category"),
            )

        usage_data = data.get("usage", {})
        return Completion(
            text=text,
            usage=Usage(
                input_tokens=usage_data.get("input_tokens", 0),
                output_tokens=usage_data.get("output_tokens", 0),
                cached_input_tokens=usage_data.get("cache_read_input_tokens", 0),
            ),
            model=data.get("model", self._model),
            provider="anthropic",
            finish_reason=stop_reason or "stop",
            tool_calls=[block for block in blocks if block.get("type") == "tool_use"],
        )

    async def stream(
        self,
        messages: Sequence[Message],
        max_tokens: int | None = None,
        *,
        usage_out: Usage | None = None,
        **_: Any,
    ) -> AsyncIterator[str]:
        """Text deltas, optionally reporting final usage into `usage_out`.

        Usage on a stream arrives split across frames — input and cache-read on
        `message_start`, output on the final `message_delta` — so the slot is
        filled incrementally as the frames pass; a caller that abandons the
        stream keeps whatever landed.
        """
        payload = self._payload(messages, max_tokens, stream=True)
        # the guard spans the whole stream: a connection that dies halfway is as
        # much an outage as one that never opened. an abandoned generator raises
        # GeneratorExit, which is not an Exception, so a caller walking away
        # neither counts as a failure nor as a success
        with guard("anthropic"):
            try:
                async with self._client.stream("POST", "/v1/messages", json=payload) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode(errors="replace")
                        raise _classify(response.status_code, body)

                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if event.get("type") == "content_block_delta":
                            delta = event.get("delta", {})
                            # thinking deltas are a separate type and are not yielded
                            if delta.get("type") == "text_delta":
                                chunk = delta.get("text", "")
                                if chunk:
                                    yield chunk
                        elif event.get("type") == "message_start":
                            if usage_out is not None:
                                frame = (event.get("message") or {}).get("usage") or {}
                                usage_out.input_tokens = int(frame.get("input_tokens", 0) or 0)
                                usage_out.cached_input_tokens = int(
                                    frame.get("cache_read_input_tokens", 0) or 0
                                )
                        elif event.get("type") == "message_delta":
                            if usage_out is not None:
                                frame = event.get("usage") or {}
                                usage_out.output_tokens = int(
                                    frame.get("output_tokens", 0) or 0
                                )
                        elif event.get("type") == "error":
                            message = event.get("error", {}).get("message", "stream error")
                            raise ProviderUnavailableError("anthropic", message)
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError("anthropic", "stream timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError("anthropic", str(exc)) from exc

    async def close(self) -> None:
        await self._client.aclose()
