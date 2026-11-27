"""OpenAI-compatible chat completions.

One adapter covers OpenAI, OpenRouter, Gemini's compatibility endpoint, NVIDIA
NIM, and any self-hosted server speaking the same protocol. Only the base URL and
model ID differ, so a separate class per vendor would be four copies of one file.
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

OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
# NVIDIA NIM. This is the hosted API catalogue (build.nvidia.com), which speaks
# the same protocol as a NIM container someone runs themselves — a self-hosted
# deployment only changes the base URL, which is why NVIDIA_BASE_URL is a setting
# rather than a constant at the call site.
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"


def _classify(provider: str, status: int, body: str) -> Exception:
    snippet = body[:400]
    lowered = body.lower()
    if status == 429:
        return ProviderRateLimitError(provider, f"rate limited: {snippet}")
    if status in (500, 502, 503, 529):
        return ProviderUnavailableError(provider, f"upstream error {status}: {snippet}")
    if status == 504:
        return ProviderTimeoutError(provider, f"gateway timeout: {snippet}")
    if "context_length" in lowered or "maximum context" in lowered:
        return ContextLengthError(f"request exceeds context window: {snippet}")
    return LLMError(f"{provider} {status}: {snippet}")


class OpenAICompatibleProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        provider_name: str = "openai",
        base_url: str = OPENAI_BASE_URL,
        timeout: float = 120.0,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._model = model
        self._provider = provider_name
        self._max_tokens = max_tokens
        self._temperature = temperature

        headers = {
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            **(extra_headers or {}),
        }
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(timeout, connect=10.0), headers=headers
        )

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def model_id(self) -> str:
        return self._model

    @staticmethod
    def _tools(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate the internal Anthropic shaped tool list to OpenAI's.

        Internal shape is {"name", "description", "input_schema"}. OpenAI wraps
        every entry in {"type": "function", "function": {...}} and calls the
        schema `parameters`. An entry that already arrives wrapped is passed
        through untouched, so a caller holding a vendor native definition is not
        forced to unwrap it first.
        """
        translated: list[dict[str, Any]] = []
        for tool in tools:
            if tool.get("type") == "function" and "function" in tool:
                translated.append(tool)
                continue
            function: dict[str, Any] = {
                "name": tool["name"],
                "parameters": tool.get("input_schema")
                or tool.get("parameters")
                or {"type": "object", "properties": {}},
            }
            description = tool.get("description")
            if description:
                function["description"] = description
            translated.append({"type": "function", "function": function})
        return translated

    def _payload(
        self,
        messages: Sequence[Message],
        max_tokens: int | None,
        stream: bool,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [message.as_dict() for message in messages],
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": self._temperature,
        }
        # absent means absent. an empty tools array is not the same request as no
        # tools at all, and some gateways reject it outright
        if tools:
            payload["tools"] = self._tools(tools)
        if stream:
            payload["stream"] = True
            # ask for usage on the final chunk so metering works on streams too
            payload["stream_options"] = {"include_usage": True}
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
        # outage trips it while a refused or oversized prompt does not
        with guard(self._provider):
            try:
                response = await self._client.post(
                    "/chat/completions",
                    json=self._payload(messages, max_tokens, stream=False, tools=tools),
                )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(self._provider, "request timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(self._provider, str(exc)) from exc

            if response.status_code >= 400:
                raise _classify(self._provider, response.status_code, response.text)

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self._provider} returned no choices")

        choice = choices[0]
        message = choice.get("message", {})
        usage = data.get("usage", {})

        return Completion(
            text=message.get("content") or "",
            usage=Usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                # automatic prefix caching is on by default server side; reading
                # the count is what makes it visible to usage accounting
                cached_input_tokens=(usage.get("prompt_tokens_details") or {}).get(
                    "cached_tokens", 0
                ),
            ),
            model=data.get("model", self._model),
            provider=self._provider,
            finish_reason=choice.get("finish_reason") or "stop",
            tool_calls=message.get("tool_calls") or [],
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

        The payload already asks for `include_usage`; the final frame carries it.
        Without this slot a streamed turn recorded zero tokens, which is why a
        streamed channel was entirely absent from usage accounting.
        """
        payload = self._payload(messages, max_tokens, stream=True)
        # the guard spans the whole stream: a connection that dies halfway is as
        # much an outage as one that never opened. an abandoned generator raises
        # GeneratorExit, which is not an Exception, so a caller walking away
        # neither counts as a failure nor as a success
        with guard(self._provider):
            try:
                async with self._client.stream(
                    "POST", "/chat/completions", json=payload
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode(errors="replace")
                        raise _classify(self._provider, response.status_code, body)

                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw or raw == "[DONE]":
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        for choice in event.get("choices") or []:
                            chunk = (choice.get("delta") or {}).get("content")
                            if chunk:
                                yield chunk

                        if usage_out is not None and event.get("usage"):
                            frame = event["usage"] or {}
                            usage_out.input_tokens = int(frame.get("prompt_tokens", 0) or 0)
                            usage_out.output_tokens = int(
                                frame.get("completion_tokens", 0) or 0
                            )
                            usage_out.cached_input_tokens = int(
                                (frame.get("prompt_tokens_details") or {}).get(
                                    "cached_tokens", 0
                                )
                                or 0
                            )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(self._provider, "stream timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(self._provider, str(exc)) from exc

    async def close(self) -> None:
        await self._client.aclose()


def openai(api_key: str, model: str, **kwargs: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key, model, provider_name="openai", base_url=OPENAI_BASE_URL, **kwargs
    )


def openrouter(
    api_key: str, model: str, referer: str = "", title: str = "", **kwargs: Any
) -> OpenAICompatibleProvider:
    """OpenRouter fronts hundreds of models behind one integration, which makes
    it the highest leverage adapter for tenants that bring their own key."""
    headers: dict[str, str] = {}
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    return OpenAICompatibleProvider(
        api_key,
        model,
        provider_name="openrouter",
        base_url=OPENROUTER_BASE_URL,
        extra_headers=headers,
        **kwargs,
    )


def gemini(api_key: str, model: str, **kwargs: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key, model, provider_name="gemini", base_url=GEMINI_BASE_URL, **kwargs
    )


def nvidia(
    api_key: str, model: str, base_url: str = "", **kwargs: Any
) -> OpenAICompatibleProvider:
    """NVIDIA NIM, hosted or self-hosted.

    An empty ``base_url`` means the hosted catalogue. A self-hosted NIM container
    is reached by passing its own ``/v1`` URL instead, and nothing else changes:
    the protocol, the model id, and the error classification are identical.
    """
    return OpenAICompatibleProvider(
        api_key,
        model,
        provider_name="nvidia",
        base_url=base_url or NVIDIA_BASE_URL,
        **kwargs,
    )
