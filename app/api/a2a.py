"""The A2A agent card — the swarm, machine-readable, at the well-known path.

``GET /.well-known/agent-card.json`` is an advertisement: public, no auth, one
JSON document that says what this swarm is, which agents run in it, what the
platform can do, and how another agent (or a judge with curl) calls it.

Everything in the card is *generated*, never hand-maintained:

* ``agents`` comes from the same ``GRAPH_NODES`` / ``AGENT_KEYS`` /
  ``AGENT_DESCRIPTIONS`` triple ``GET /v1/graph`` serves (via
  ``agent_entries()``, which also carries the registration assert) — the card
  and the graph cannot disagree — filtered to implemented agents, because a
  card entry is a promise that the agent answers, and a planned one does not;
* ``capabilities.registry`` is ``BuiltinService.describe()`` verbatim — the
  card cannot claim a capability the registry does not dispatch;
* the chat request/response shapes are the Pydantic models' own JSON schemas —
  the card cannot describe a field the endpoint does not accept.

On shape: this follows the A2A agent-card top half (name, description, version,
url, authentication.schemes, authentication.credentials) but keeps a simpler,
flatter bottom half instead of the full skill/protocol envelope — the PS story
is "curl it and see the swarm", and the README says so. Where the spec and the
story conflicted, the story won. Two deliberate scope notes: ``streaming`` is
``False`` because it describes ``POST /v1/chat`` (one JSON request, one
complete JSON answer) — the SSE surface is ``/v1/chat/resume``, which only
continues a prior turn that ended waiting on a client action and is not a
general streaming entry point; and the card is cached per process because every
source it is built from (the registry, the graph constants, the Pydantic
schemas) is process-static — a public route should not rebuild JSON schemas on
every request.

Auth on the callable endpoints is the platform's existing API-key path: an
``sk_live_…`` key in ``Authorization: Bearer`` (the one ``scripts/seed.py``
prints for the demo tenant). The card itself carries no secret and names no
tenant — it is the same for whoever fetches it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Request

from app.api.schemas import ChatRequest, ChatResponse
from app.api.v1.graph import agent_entries
from app.api.v1.orchestrator_api import OrchestratorAskRequest, OrchestratorAskResponse
from app.services.agents.registry import BY_KEY
from app.services.growth.builtin import BuiltinService

router = APIRouter(tags=["a2a"])

DESCRIPTION = (
    "A swarm of autonomous agents that runs a small business's growth work "
    "every day on a shared knowledge base — researching prospects, writing and "
    "sending outreach, triaging replies, qualifying inbound leads, running "
    "campaigns, watching offers, stock and festivals, pushing outcomes to the "
    "CRM, and reporting to the owner on Telegram or WhatsApp. Machine callers "
    "get the orchestrator agent — the owner's front door — over POST "
    "/v1/orchestrator/ask and the A2A message/send envelope; POST /v1/chat "
    "remains for the support agent."
)

_LOCAL_FALLBACK = "http://localhost:8000"


def _advertised_agents() -> list[dict[str, str]]:
    """The card's agent list: implemented agents only.

    A card is an advertisement of what is callable *now*. The graph shows
    planned agents (greyed, "not built yet") because the owner should see what
    is coming; a machine reading the well-known card would read a planned entry
    as a capability that answers, which it does not. Status comes from the
    registry — pure code, no tenant, no database — so this filter is as
    process-static as the rest of the card and the no-tenant path
    (``BuiltinService(None).describe()``) is untouched.
    """
    return [
        entry
        for entry in agent_entries()
        if BY_KEY.get(str(entry["key"])) is None
        or BY_KEY[str(entry["key"])].status != "planned"
    ]


def build_agent_card(
    *,
    name: str,
    version: str,
    base_url: str,
    capabilities: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the card from its live sources. Pure, so tests prove the
    card/registry/graph agreement without an app or a database.

    ``base_url`` is used verbatim: an empty one yields relative URLs rather
    than an invented host — the localhost fallback is the route's dev-only
    decision, not the builder's.
    """
    base = base_url.rstrip("/")
    # skills are the orchestrator's tool surface — what a personal agent can
    # make the swarm do through the front door. Imported lazily to keep the
    # card module import-light and avoid circular imports at startup.
    try:
        from app.services.agents.orchestrator_tools import TOOL_SPECS as _SKILLS
    except Exception:
        _SKILLS = ()
    return {
        "name": name,
        "description": DESCRIPTION,
        "version": version,
        # A2A's `url` is the orchestrator endpoint — the single front door a
        # personal agent should call. The support chat remains advertised under
        # endpoints.chat for backwards compat.
        "url": f"{base}/v1/orchestrator/ask",
        "defaultAgent": "orchestrator",
        "authentication": {
            "schemes": ["bearer"],
            "credentials": "sk_live_… API key as Authorization: Bearer <key>",
        },
        "capabilities": {
            "streaming": False,
            "registry": capabilities,
        },
        "agents": _advertised_agents(),
        "skills": list(_SKILLS),
        "endpoints": {
            "orchestrator": {
                "rest": {
                    "method": "POST",
                    "url": f"{base}/v1/orchestrator/ask",
                    "auth": "Authorization: Bearer <api key>",
                    "description": "Orchestrator: one question in, one grounded answer out (reads + queue actions). No conversations row.",
                    "request": OrchestratorAskRequest.model_json_schema(),
                    "response": OrchestratorAskResponse.model_json_schema(),
                },
                "a2a": {
                    "method": "POST",
                    "url": f"{base}/a2a/message/send",
                    "auth": "Authorization: Bearer <api key>",
                    "description": "A2A message/send — same orchestrator, synchronous Task envelope.",
                    "request": {
                        "type": "object",
                        "properties": {
                            "jsonrpc": {"type": "string", "const": "2.0"},
                            "id": {"type": ["string", "integer"]},
                            "method": {"type": "string", "const": "message/send"},
                            "params": {
                                "type": "object",
                                "properties": {
                                    "message": {
                                        "type": "object",
                                        "properties": {
                                            "role": {"type": "string"},
                                            "parts": {"type": "array"},
                                            "contextId": {"type": "string"},
                                        },
                                    }
                                },
                            },
                        },
                    },
                },
                "mcp": {
                    "transport": "stdio",
                    "command": "python mcp/server.py",
                    "description": "MCP stdio server exposing orchestrator.ask — see mcp/README.md",
                },
            },
            "chat": {
                "method": "POST",
                "url": f"{base}/v1/chat",
                "auth": "Authorization: Bearer <api key>",
                "description": "Support agent: one question in, one complete answer out, with citations.",
                "request": ChatRequest.model_json_schema(),
                "response": ChatResponse.model_json_schema(),
            },
            "graph": {
                "method": "GET",
                "url": f"{base}/v1/graph",
                "auth": "Authorization: Bearer <api key>",
                "description": (
                    "The swarm's shape — nodes, edges and per-agent descriptions. "
                    "Tenant-invariant: the same graph for every caller."
                ),
            },
        },
    }


@lru_cache(maxsize=4)
def _card_for(name: str, version: str, base_url: str) -> dict[str, Any]:
    """The cached card. Every input is process-static (settings, app version,
    the registry, the graph constants, the Pydantic schemas), so one build per
    configuration serves every request — this is a public unauthenticated
    route, and rebuilding JSON schemas per request is pure waste."""
    return build_agent_card(
        name=name,
        version=version,
        base_url=base_url,
        capabilities=BuiltinService(None).describe(),
    )


@router.get("/.well-known/agent-card.json")
async def agent_card(request: Request) -> dict[str, Any]:
    settings = request.app.state.container.settings
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    # dev-only fallback: in production an unset PUBLIC_BASE_URL yields relative
    # URLs rather than silently advertising localhost
    if not base and not settings.is_production:
        base = _LOCAL_FALLBACK
    return _card_for(settings.PRODUCT_NAME, str(request.app.version), base)
