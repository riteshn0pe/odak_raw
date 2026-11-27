"""The orchestrator's machine front doors: REST and A2A JSON-RPC.

Two shapes over one agent. ``POST /v1/orchestrator/ask`` is the plain JSON
door for a personal agent that just wants an answer; ``POST /a2a/message/send``
is the A2A `message/send` shape the PS names as the ideal interface — a thin
wrapper that validates the JSON-RPC envelope, runs the same agent, and returns
the same answer in a synchronous A2A ``Task``.

Owner-only by construction, the same rule the Telegram channel keeps: the
credential must carry the ``orchestrator:ask`` scope (see ``_require_scope``),
so a support-chat key cannot reach the owner's front door. No ``conversations``
row is ever written — that table is the customer-facing channels' memory; the
one observability row is the orchestrator's own audit entry.

Failures follow the platform's fail-open doctrine: the agent itself returns the
honest one-liner on a failed turn, so a handler only has to map the platform's
own errors to HTTP/JSON-RPC statuses and never leak internals.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ContainerDep, TenantDep
from app.core.errors import AuthenticationError, AuthorizationError, ValidationError
from app.core.logging import get_logger
from app.domain.entities import TenantContext

logger = get_logger(__name__)

router = APIRouter(tags=["orchestrator"])
a2a_router = APIRouter(tags=["a2a"])

# per-tenant ceiling, same as /v1/chat — abuse control, not a billing meter
ORCHestrator_RATE_LIMIT_RPM = 60

# The scope a credential must carry to call the owner's front door. Issued with
# the key (POST /v1/keys), checked here on every call.
ORCHESTRATOR_SCOPE = "orchestrator:ask"


def require_orchestrator_scope(tenant: TenantContext) -> None:
    """A key without the scope is not an owner credential for this door.

    Dashboard JWTs are rejected outright: the orchestrator's contract is
    server-to-server, and the Telegram gate's analogue is the scoped key, not a
    browser session.
    """
    if tenant.role is not None:
        raise AuthorizationError("this endpoint accepts an API key only")
    if tenant.api_key_id is None:
        raise AuthenticationError("api key required")


class OrchestratorAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=128)


class OrchestratorAskResponse(BaseModel):
    answer: str
    conversation_id: str | None = None


async def _ask(container: Any, tenant: TenantContext, question: str) -> str:
    """The one service call both doors share."""
    await container.rate_limiter.check(
        "orchestrator", str(tenant.tenant_id), ORCHestrator_RATE_LIMIT_RPM, 60
    )
    return await container.orchestrator_agent.answer(
        tenant_id=tenant.tenant_id,
        chat_id="api",
        text=question,
    )


@router.post("/orchestrator/ask", response_model=OrchestratorAskResponse)
async def orchestrator_ask(
    payload: OrchestratorAskRequest, tenant: TenantDep, container: ContainerDep
) -> OrchestratorAskResponse:
    require_orchestrator_scope(tenant)
    answer = await _ask(container, tenant, payload.question)
    return OrchestratorAskResponse(
        answer=answer, conversation_id=payload.conversation_id
    )


# ------------------------------------------------------------------- A2A door

JSONRPC_ERRORS = {"parse": -32700, "invalid_request": -32600, "method": -32601}

# The synchronous Task shape a stateless v1 can answer with. `completed` when
# the agent produced an answer (including its honest failure line — the turn
# succeeded even when the answer is an apology), `failed` only when the
# envelope itself was unusable.
TASK_STATE_COMPLETED = "completed"


def _task(text: str, message: dict[str, Any]) -> dict[str, Any]:
    """Build the A2A Task object for a finished turn."""
    return {
        "id": str(uuid.uuid4()),
        "contextId": message.get("contextId") or str(uuid.uuid4()),
        "status": {"state": TASK_STATE_COMPLETED},
        "kind": "task",
        "artifacts": [
            {
                "artifactId": str(uuid.uuid4()),
                "name": "answer",
                "parts": [{"kind": "text", "text": text}],
            }
        ],
        "history": [message],
    }


def _rpc(result: Any, request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(code: int, message: str, request_id: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


@a2a_router.post("/a2a/message/send")
async def a2a_message_send(payload: dict[str, Any], tenant: TenantDep, container: ContainerDep) -> dict[str, Any]:
    """A2A ``message/send`` over the orchestrator.

    Envelope-only wrapper: the agent's contract (one question, one answer,
    fail-open) is unchanged — the personal agent's ecosystem sees a task in
    the shape its SDKs expect.
    """
    require_orchestrator_scope(tenant)
    request_id = payload.get("id")
    if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        return _rpc_error(JSONRPC_ERRORS["invalid_request"], "not a JSON-RPC 2.0 request", request_id)
    if payload["method"] != "message/send":
        return _rpc_error(JSONRPC_ERRORS["method"], f"unsupported method {payload['method']!r}", request_id)

    params = payload.get("params") or {}
    message = params.get("message") or {}
    parts = message.get("parts") or []
    text = "".join(
        str(part.get("text") or "") for part in parts if isinstance(part, dict)
    ).strip()
    if not text:
        return _rpc_error(JSONRPC_ERRORS["invalid_request"], "message has no text part", request_id)

    try:
        answer = await _ask(container, tenant, text[:4000])
    except ValidationError as exc:
        return _rpc_error(JSONRPC_ERRORS["invalid_request"], str(exc), request_id)
    return _rpc(_task(answer, message), request_id)
