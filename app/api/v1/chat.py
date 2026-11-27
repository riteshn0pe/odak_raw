"""Chat endpoints.

Two shapes over one runner. The JSON form is for server-to-server callers who
want the whole answer; the resume form streams the completion of a turn that
ended waiting on a client action's result.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ContainerDep, TenantDep, load_agent_config
from app.api.schemas import (
    ChatRequest,
    ChatResponse,
    CitationOut,
    FeedbackRequest,
    SearchHitOut,
    SearchRequest,
    SearchResponse,
)
from app.core.constants import (
    ACTION_CONVERSATION_RATE_PER_MINUTE,
    RL_SCOPE_ACTION_CONVERSATION,
)
from app.core.errors import AppError, AuthorizationError
from app.core.logging import get_logger
from app.core.security import verify_token

logger = get_logger(__name__)

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest, tenant: TenantDep, container: ContainerDep
) -> ChatResponse:
    # per-tenant ceiling from the plan, applied before any work happens. a broken
    # cache fails open, so this is abuse control, not a hard billing meter
    await _rate_limit(tenant, container, "chat")

    agent_id, config = await load_agent_config(tenant, container, payload.agent_id)

    with container.telemetry.start_span(
        "chat",
        tenant_id=str(tenant.tenant_id),
        channel=tenant.channel.value,
        agent_id=str(agent_id) if agent_id else None,
    ):
        result = await container.agent.answer(
            tenant_id=tenant.tenant_id,
            config=config,
            question=payload.question,
            agent_id=agent_id,
            conversation_id=payload.conversation_id,
            channel=tenant.channel,
            external_user_ref=tenant.external_user_ref,
            # verified identity, carried in our own session token rather than
            # re-verified per turn. Both default to the refusing values, so a
            # channel that never establishes an identity makes every action that
            # interpolates {{contact.*}} refuse instead of rendering it empty.
            contact_id=tenant.contact_id,
            identity_verified=tenant.identity_verified,
            document_ids=payload.document_ids,
        )

    return ChatResponse(
        answer=result.answer,
        citations=[
            CitationOut(
                chunk_id=citation.chunk_id,
                document_id=citation.document_id,
                filename=citation.filename,
                page_from=citation.page_from,
                page_to=citation.page_to,
                heading_path=citation.heading_path,
                url=citation.url,
            )
            for citation in result.citations
        ],
        confidence=result.confidence,
        grounded=result.grounded,
        conversation_id=result.conversation_id,
        message_id=result.message_id,
        latency_ms=result.latency_ms,
        model=result.model,
        reason=result.reason,
        blocked=result.blocked,
    )


class ResumeRequest(BaseModel):
    """The browser's answer to a client action, plus the ticket that proves we asked."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    call_id: str = Field(min_length=8, max_length=128)
    ticket: str = Field(min_length=16, max_length=4096)
    # the visitor's browser produced this. it is data, never authority
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=200)


def _verify_ticket(payload: ResumeRequest, tenant: Any, container: Any) -> None:
    """Authenticate that *we asked* for this exact call, in this conversation.

    The ticket is a one-shot capability, not a session: it is HMAC-signed with a
    60s TTL and carries the tenant, conversation and call it was minted for, so a
    result cannot be replayed into a different conversation or a different call.
    Verification belongs here rather than in the runner because the runner's own
    guard is the database claim on the suspension row — that stops a replay, but
    only after an unauthenticated caller has already been let in.

    It authenticates the question, never the answer. The visitor can always lie
    about `result`, which is why a client action can never be `effect="write"`.
    """
    try:
        claims = verify_token(payload.ticket, container.settings.SECRET_KEY)
    except AppError as exc:
        raise AuthorizationError("resume ticket is not valid") from exc

    if claims.get("typ") != "client_action":
        raise AuthorizationError("resume ticket is not valid")
    if claims.get("call_id") != payload.call_id:
        raise AuthorizationError("resume ticket does not match that call")
    if claims.get("conversation_id") != str(payload.conversation_id):
        raise AuthorizationError("resume ticket does not match that conversation")
    if claims.get("tenant_id") != str(tenant.tenant_id):
        raise AuthorizationError("resume ticket does not match this tenant")


@router.post("/chat/resume")
async def chat_resume(
    payload: ResumeRequest, tenant: TenantDep, container: ContainerDep
) -> StreamingResponse:
    """Continue a turn that ended waiting on the caller's client action.

    Same SSE shape the paused turn's events used. The per-conversation window is
    applied on top of the tenant window: without it a client that resumes in a
    loop drives the action loop as fast as the network allows, and every other
    ceiling here is per turn or per minute.
    """
    await _rate_limit(tenant, container, "chat")
    _verify_ticket(payload, tenant, container)
    await container.rate_limiter.check(
        RL_SCOPE_ACTION_CONVERSATION,
        f"{tenant.tenant_id}:{payload.conversation_id}",
        ACTION_CONVERSATION_RATE_PER_MINUTE,
        60,
    )

    agent_id, config = await load_agent_config(tenant, container)

    async def events() -> AsyncIterator[str]:
        try:
            async for event in container.agent.resume_client_action(
                tenant.tenant_id,
                config,
                payload.conversation_id,
                payload.call_id,
                agent_id=agent_id,
                channel=tenant.channel,
                result=payload.result,
                error=payload.error,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except AppError as exc:
            logger.error(
                "chat_resume_failed",
                tenant_id=str(tenant.tenant_id),
                code=exc.code,
                exc_info=True,
            )
            error_event = {"type": "error", "code": exc.code, "message": exc.message}
            yield f"data: {json.dumps(error_event)}\n\n"
        except Exception:
            logger.error("chat_resume_failed", tenant_id=str(tenant.tenant_id), exc_info=True)
            error_event = {
                "type": "error",
                "code": "internal_error",
                "message": "the action could not be completed",
            }
            yield f"data: {json.dumps(error_event)}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/search", response_model=SearchResponse)
async def search(
    payload: SearchRequest, tenant: TenantDep, container: ContainerDep
) -> SearchResponse:
    """Retrieval only, no generation."""
    _, config = await load_agent_config(tenant, container)

    retrieval_config = config.retrieval.model_copy(
        update={"top_k": payload.top_k, "max_context_chunks": payload.top_k}
    )
    result = await container.retrieval.search(
        tenant.tenant_id, payload.query, retrieval_config, payload.document_ids
    )

    return SearchResponse(
        hits=[
            SearchHitOut(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                filename=chunk.filename,
                text=chunk.context_text,
                score=chunk.score,
                page_from=chunk.page_from,
                page_to=chunk.page_to,
                heading_path=chunk.heading_path,
            )
            for chunk in result.chunks
        ],
        confidence=result.confidence,
        strategy=result.strategy,
    )


@router.post("/feedback", status_code=204)
async def feedback(
    payload: FeedbackRequest, tenant: TenantDep, container: ContainerDep
) -> None:
    """Thumbs up or down on an answer.

    Feeds the analytics that tell a tenant which documents are failing their
    users — the signal is only useful in aggregate, so it is written and not
    acted on here.
    """
    await container.conversations.set_feedback(
        tenant.tenant_id, payload.message_id, payload.rating
    )


# platform-wide rate limit, per tenant, per minute
CHAT_RATE_LIMIT_RPM = 60


async def _rate_limit(tenant: Any, container: Any, scope: str) -> None:
    """Apply the tenant limit.

    The tenant limit protects our cost against one customer's traffic. A broken
    cache fails open, so this is abuse control, not a hard billing meter.
    """
    await container.rate_limiter.check(
        scope, str(tenant.tenant_id), CHAT_RATE_LIMIT_RPM, 60
    )
