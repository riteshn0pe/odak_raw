"""Gemini through its native GenerateContent surface on Vertex AI.

The platform's Gemini key is an API key scoped to
``aiplatform.googleapis.com`` (the publisher models endpoint), not the AI
Studio ``generativelanguage`` API — the OpenAI-compatibility layer over there
answers 403 for it. This adapter speaks GenerateContent directly, which is
also the more standard integration for a Vertex-issued key: no compatibility
shim, the protocol the model itself is documented against.

Everything the LLM port needs is translated here, in one direction each:

* request: the port's Anthropic-shaped tools become ``functionDeclarations``,
  and the OpenAI-shaped message list the agent loop emits (``as_dict`` with
  ``tool_calls`` / ``tool`` payloads — see ``services/agent/loop.py``'s
  ``_WireMessage``) becomes ``contents`` with ``functionCall`` /
  ``functionResponse`` parts. System messages become ``systemInstruction``.
* response: ``functionCall`` parts become the OpenAI tool-call shape the loop's
  ``_parse_tool_calls`` already understands, so no agent code branches on
  Gemini.

Gemini 3.x parts may carry a ``thoughtSignature`` alongside text and calls;
it is one-turn state for thinking continuity and is deliberately dropped —
the port has no field for it, and sending it back is optional.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from app.adapters.llm.base import Completion, Message, Usage
from app.adapters.llm.openai_compatible import _classify
from app.core.breaker import guard
from app.core.errors import LLMError, ProviderTimeoutError, ProviderUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)

# Vertex AI publisher models: an API key on the x-goog-api-key header, the
# model in the path. This is the endpoint a Vertex-scoped key can reach; the
# per-tenant BYOK case with an AI Studio key is served by the same protocol on
# the other host, so the base URL stays injectable.
GEMINI_NATIVE_BASE = "https://aiplatform.googleapis.com/v1/publishers/google/models"

_FINISH_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "RECITATION": "content_filter",
}


class GeminiNativeProvider:
    """Chat completion through ``{model}:generateContent``."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        provider_name: str = "gemini",
        base_url: str = "",
        timeout: float = 120.0,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> None:
        self._model = model
        self._provider = provider_name
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._client = httpx.AsyncClient(
            base_url=(base_url or GEMINI_NATIVE_BASE).rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"x-goog-api-key": api_key, "content-type": "application/json"},
        )

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def model_id(self) -> str:
        return self._model

    # ------------------------------------------------------------ translation

    @staticmethod
    def _as_dict(message: Any) -> dict[str, Any]:
        """One message as the loop serialises it.

        ``Message`` carries role and content only; the loop's ``_WireMessage``
        duck-types ``as_dict()`` and may put an OpenAI-shaped payload (assistant
        ``tool_calls``, ``role="tool"`` results) in its place. Both arrive here.
        """
        if hasattr(message, "as_dict"):
            return message.as_dict()
        return {"role": getattr(message, "role", ""), "content": getattr(message, "content", "")}

    @classmethod
    def _contents(
        cls, messages: Sequence[Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Messages into (systemInstruction parts, contents).

        A tool result only knows the call id it answers; Gemini matches a
        ``functionResponse`` by function *name*, so the assistant turn's calls
        are collected first and each result's id is looked up in them. A name
        that cannot be resolved falls back to the id itself — a request the
        model will reject as a mismatch, which is more honest than silently
        dropping the result.
        """
        system: list[dict[str, Any]] = []
        contents: list[dict[str, Any]] = []
        call_names: dict[str, str] = {}
        # Consecutive tool results belong to ONE user turn: Gemini rejects N
        # separate user contents answering one model turn with N functionCalls
        # ("number of function response parts is equal to the number of
        # function call parts"). Buffer them and flush as a single entry.
        pending_tool_parts: list[dict[str, Any]] = []

        def _flush_tools() -> None:
            if pending_tool_parts:
                contents.append({"role": "user", "parts": pending_tool_parts.copy()})
                pending_tool_parts.clear()

        for message in messages:
            data = cls._as_dict(message)
            role = str(data.get("role") or "")

            if role == "system":
                if data.get("content"):
                    system.append({"text": str(data["content"])})
                continue

            if role == "tool":
                call_id = str(data.get("tool_call_id") or "")
                name = call_names.get(call_id, call_id)
                pending_tool_parts.append(
                    {
                        "functionResponse": {
                            "name": name,
                            "id": call_id,
                            "response": {"result": str(data.get("content") or "")},
                        }
                    }
                )
                continue

            _flush_tools()
            gemini_role = "model" if role == "assistant" else "user"

            if isinstance(data.get("tool_calls"), list) and data["tool_calls"]:
                # the assistant turn that requested tools, replayed back
                parts: list[dict[str, Any]] = []
                text = data.get("content")
                if text:
                    parts.append({"text": str(text)})
                for call in data["tool_calls"]:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "")
                    call_id = str(call.get("id") or "")
                    call_names[call_id] = name
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments or "{}")
                        except (ValueError, TypeError):
                            arguments = {}
                    function_call: dict[str, Any] = {
                        "name": name,
                        "args": arguments if isinstance(arguments, dict) else {},
                        **({"id": call_id} if call_id else {}),
                    }
                    signature = call.get("thoughtSignature")
                    part: dict[str, Any] = {"functionCall": function_call}
                    if isinstance(signature, str) and signature:
                        part["thoughtSignature"] = signature
                    parts.append(part)
                contents.append({"role": gemini_role, "parts": parts})
                continue

            text = data.get("content")
            if isinstance(text, list):
                # a block list (the anthropic wire shape) — the text blocks are
                # all a GenerateContent request can carry
                text = "".join(
                    str(block.get("text") or "")
                    for block in text
                    if isinstance(block, dict)
                )
            if text:
                contents.append({"role": gemini_role, "parts": [{"text": str(text)}]})

        _flush_tools()
        return system, contents

    @staticmethod
    def _tools(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """The port's Anthropic-shaped tool list into functionDeclarations.

        An entry already in OpenAI's wrapper shape is unwrapped, so a caller
        holding either dialect is accepted. Absent stays absent: an empty
        declarations list is not the same request as no tools at all.
        """
        declarations: list[dict[str, Any]] = []
        for tool in tools:
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                tool = tool["function"]
            declaration: dict[str, Any] = {
                "name": str(tool.get("name") or ""),
                "parameters": tool.get("input_schema")
                or tool.get("parameters")
                or {"type": "object", "properties": {}},
            }
            if tool.get("description"):
                declaration["description"] = str(tool["description"])
            if declaration["name"]:
                declarations.append(declaration)
        if not declarations:
            return None
        return [{"functionDeclarations": declarations}]

    # Gemini 3 counts thinking tokens against maxOutputTokens, so a small
    # output budget truncates to finish=length and fails JSON parse. Accuracy
    # is the point of every agent call here, so thinking defaults to HIGH and
    # is never allowed below MEDIUM (MINIMAL is not even supported on 3.7/3.8
    # Flash). The 20000 floor means no caller can starve thinking + answer:
    # it is a ceiling, not usage — the model stops when it is done, and a
    # starved run costs a retry while a generous ceiling costs nothing extra.
    _THINKING_FLOOR = 20000
    _THINKING_DEFAULT = "HIGH"
    _THINKING_MINIMUM = "MEDIUM"
    _THINKING_ORDER = ("MINIMAL", "LOW", "MEDIUM", "HIGH")

    @classmethod
    def _thinking_level(cls, requested: str | None) -> str:
        """The thinking level to send: HIGH unless asked, never below MEDIUM."""
        level = str(requested or cls._THINKING_DEFAULT).upper()
        if level not in cls._THINKING_ORDER:
            return cls._THINKING_DEFAULT
        if cls._THINKING_ORDER.index(level) < cls._THINKING_ORDER.index(cls._THINKING_MINIMUM):
            return cls._THINKING_MINIMUM
        return level

    def _payload(
        self,
        messages: Sequence[Any],
        max_tokens: int | None,
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        thinking_level: str | None = None,
    ) -> dict[str, Any]:
        system, contents = self._contents(messages)
        requested = max_tokens or self._max_tokens
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": self._temperature,
                "maxOutputTokens": max(int(requested), self._THINKING_FLOOR),
                "thinkingConfig": {"thinkingLevel": self._thinking_level(thinking_level)},
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": system}
        declarations = self._tools(tools) if tools else None
        if declarations:
            payload["tools"] = declarations
        return payload

    @staticmethod
    def _completion(model: str, data: dict[str, Any]) -> Completion:
        candidates = data.get("candidates") or []
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if not candidates:
            if blocked:
                raise LLMError(f"gemini refused the prompt: {blocked}")
            raise LLMError("gemini returned no candidates")

        candidate = candidates[0]
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for index, part in enumerate((candidate.get("content") or {}).get("parts") or []):
            if isinstance(part.get("text"), str) and part["text"]:
                text_parts.append(part["text"])
            function_call = part.get("functionCall")
            if isinstance(function_call, dict) and function_call.get("name"):
                call: dict[str, Any] = {
                    "id": str(function_call.get("id") or f"call_{index}"),
                    "type": "function",
                    "function": {
                        "name": str(function_call["name"]),
                        "arguments": json.dumps(
                            function_call.get("args") or {}, default=str
                        ),
                    },
                }
                # Gemini 3 refuses a replayed call without it — carried through
                # the loop's ToolCall and put back on the wire below. It rides
                # the part, a sibling of functionCall, not inside it.
                signature = part.get("thoughtSignature")
                if isinstance(signature, str) and signature:
                    call["thoughtSignature"] = signature
                tool_calls.append(call)

        usage_meta = data.get("usageMetadata") or {}
        finish = str(candidate.get("finishReason") or "STOP")
        return Completion(
            text="".join(text_parts),
            usage=Usage(
                input_tokens=int(usage_meta.get("promptTokenCount", 0) or 0),
                output_tokens=int(usage_meta.get("candidatesTokenCount", 0) or 0),
                cached_input_tokens=int(usage_meta.get("cachedContentTokenCount", 0) or 0),
            ),
            model=str(data.get("modelVersion") or model),
            provider="gemini",
            finish_reason=_FINISH_REASONS.get(finish, finish.lower()),
            tool_calls=tool_calls,
        )

    # ---------------------------------------------------------------- calls

    async def complete(
        self,
        messages: Sequence[Message],
        max_tokens: int | None = None,
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        thinking_level: str | None = None,
        **_: Any,
    ) -> Completion:
        with guard(self._provider):
            try:
                response = await self._client.post(
                    f"/{self._model}:generateContent",
                    json=self._payload(
                        messages, max_tokens, tools=tools,
                        thinking_level=thinking_level,
                    ),
                )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(self._provider, "request timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(self._provider, str(exc)) from exc

            if response.status_code >= 400:
                raise _classify(self._provider, response.status_code, response.text)

        return self._completion(self._model, response.json())

    async def stream(
        self,
        messages: Sequence[Message],
        max_tokens: int | None = None,
        *,
        usage_out: Usage | None = None,
        thinking_level: str | None = None,
        **_: Any,
    ) -> AsyncIterator[str]:
        payload = self._payload(messages, max_tokens, thinking_level=thinking_level)
        with guard(self._provider):
            try:
                async with self._client.stream(
                    "POST", f"/{self._model}:streamGenerateContent?alt=sse", json=payload
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode(errors="replace")
                        raise _classify(self._provider, response.status_code, body)

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

                        for part in (event.get("candidates") or [{}])[0].get(
                            "content", {}
                        ).get("parts", []):
                            if isinstance(part.get("text"), str) and part["text"]:
                                yield part["text"]

                        if usage_out is not None and event.get("usageMetadata"):
                            meta = event["usageMetadata"]
                            usage_out.input_tokens = int(meta.get("promptTokenCount", 0) or 0)
                            usage_out.output_tokens = int(
                                meta.get("candidatesTokenCount", 0) or 0
                            )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(self._provider, "stream timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(self._provider, str(exc)) from exc

    async def close(self) -> None:
        await self._client.aclose()


def gemini_native(api_key: str, model: str, **kwargs: Any) -> GeminiNativeProvider:
    return GeminiNativeProvider(api_key, model, **kwargs)
