"""The agent graph, read-only, for the tenant dashboard's Visualise tab.

The layout (nodes and edges, owned here) is served to the owner without any
model-routing payload: what runs, what triggers it, and which flows pass
through an approval gate. Clicking a node in the UI is answered entirely from
this payload plus the per-agent descriptions here — there is no second call and
nothing to edit.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ContainerDep, TenantDep
from app.core.logging import get_logger
from app.services.agents.registry import AGENT_DESCRIPTIONS, AGENTS, BY_KEY
from app.services.growth.modelconfig import AGENT_KEYS

logger = get_logger(__name__)

router = APIRouter(prefix="/graph", tags=["graph"])

# The graph the Visualise tab draws. Nodes are positioned here rather than by a
# layout library: the shape is small, fixed and meaningful — reading left to
# right is the actual data flow — and a force layout would move the boxes every
# refresh, which is the opposite of what a diagram someone navigates by should
# do. It also keeps the dashboard's three-runtime-dependency budget intact.
#
# The agent nodes themselves derive from the registry (one per AgentSpec, all
# fifteen); only the coordinates are hand-placed, in this dict. New coordinates
# follow the canvas rules the tests pin (Visualise.tsx: NODE_W=176, NODE_H=64,
# CANVAS_W=920, CANVAS_H=500) and must not overlap an existing box:
# - `orchestrator` sits at the top, centre-ish, above the agent rows — the
#   dispatch spine that starts everything. It cannot sit at the exact centre
#   (x=372) because the `webhook:inbound` trigger occupies (480, 60), so it
#   takes the top slot clear of both it and `support`.
# - `growth_followup` sits just below `growth_replies`, the free slot nearest
#   `growth_compose` in a row that is already full.
_AGENT_POSITIONS: dict[str, tuple[int, int]] = {
    "orchestrator": (280, 0),
    "growth_profile": (700, 410),
    "support": (40, 40),
    "growth_research": (40, 150),
    "growth_compose": (260, 150),
    "growth_followup": (480, 220),
    "growth_replies": (480, 150),
    "growth_inbound": (700, 40),
    "growth_campaign": (40, 410),
    "growth_insights": (260, 300),
    "growth_report": (40, 300),
    "growth_crm": (700, 300),
    "builtin_things": (480, 410),
    "growth_talkprep": (700, 150),
    "growth_reviews": (480, 300),
}

# The trigger and gate nodes are graph chrome, not agents: they stay
# hand-placed and hand-wired, and their edges (below) stay exactly as they
# were. The capability provider sits under the replies row, not at the end of a
# flow: every agent may call it, so it reads as infrastructure rather than as
# one more pipeline stage. y=410 keeps max y + NODE_H at 474, inside the tenant
# canvas's 500 (Visualise.tsx) — the campaign-row clip bug came from forgetting
# exactly that check.
_NON_AGENT_NODES: tuple[dict[str, Any], ...] = (
    {"key": "cron:growth_dispatch", "x": 40, "y": 240, "kind": "trigger"},
    {"key": "hitl:outbound_batch", "x": 150, "y": 220, "kind": "gate"},
    {"key": "hitl:campaign", "x": 150, "y": 410, "kind": "gate"},
    {"key": "webhook:inbound", "x": 480, "y": 60, "kind": "trigger"},
)

GRAPH_NODES: tuple[dict[str, Any], ...] = (
    *(
        {
            "key": spec.key,
            "x": _AGENT_POSITIONS[spec.key][0],
            "y": _AGENT_POSITIONS[spec.key][1],
            "kind": "agent",
        }
        for spec in AGENTS
    ),
    *_NON_AGENT_NODES,
)

# Agent-to-agent edges derive from the registry's `hands_over_to`: the diagram
# draws the handovers the specs actually declare, so a new handover appears
# here the moment it is registered and a removed one stops drawing a dead edge.
_AGENT_EDGES: tuple[dict[str, str], ...] = tuple(
    {"from": spec.key, "to": target, "label": "hands over"}
    for spec in AGENTS
    for target in spec.hands_over_to
)

# Edges are the real flows, not a tidy picture: cron and webhook triggers into
# the agents they start, and the approval gate where one sits between an agent
# and the send. The trigger/gate edges are hand-wired and unchanged; everything
# agent-to-agent above comes from the registry.
GRAPH_EDGES: tuple[dict[str, str], ...] = (
    {"from": "cron:growth_dispatch", "to": "growth_research", "label": "queue low"},
    {"from": "growth_research", "to": "hitl:outbound_batch", "label": "batch"},
    {"from": "hitl:outbound_batch", "to": "growth_compose", "label": "approved"},
    {"from": "webhook:inbound", "to": "growth_inbound", "label": "new lead"},
    {"from": "webhook:inbound", "to": "growth_replies", "label": "reply"},
    {"from": "growth_campaign", "to": "hitl:campaign", "label": "draft"},
    {"from": "hitl:campaign", "to": "cron:growth_dispatch", "label": "approved, sends"},
    {"from": "cron:growth_dispatch", "to": "growth_report", "label": "weekly"},
    {"from": "cron:growth_dispatch", "to": "growth_insights", "label": "daily"},
    {"from": "cron:growth_dispatch", "to": "growth_reviews", "label": "after order"},
    *_AGENT_EDGES,
)

KIND_DESCRIPTIONS: dict[str, str] = {
    "trigger": "What starts the flow — a schedule or an incoming webhook.",
    "gate": "A human approval step. The flow holds until the owner decides.",
}


def _label(key: str) -> str:
    prefix, _, rest = key.partition(":")
    if not rest:
        return AGENT_KEYS.get(key, key)
    pretty = rest.replace("_", " ")
    return {
        "cron": f"cron · {pretty}",
        "webhook": f"webhook · {pretty}",
        "hitl": f"approval · {pretty}",
    }.get(prefix, key)


class GraphNodeOut(BaseModel):
    key: str
    label: str
    kind: str
    description: str
    x: int
    y: int
    # what the agent is *for* (registry-owned; empty on trigger/gate nodes)
    goal: str = ""
    # "implemented" | "planned" (registry status)
    status: str = ""
    # identity | customer | outbound | insight | platform
    category: str = ""
    # this tenant's activation state (active/blocked/disabled/…) and the
    # human-readable reason when it is anything but active; empty when the
    # state could not be resolved — the graph degrades to the static shape
    # rather than 500ing on a facts read
    state: str = ""
    reason: str = ""


class GraphEdgeOut(BaseModel):
    source: str = Field(alias="from")
    target: str = Field(alias="to")
    label: str = ""

    model_config = ConfigDict(populate_by_name=True)


class GraphOut(BaseModel):
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]


def agent_entries() -> list[dict[str, str]]:
    """The swarm's agents as plain data: key, label, description, one each.

    Shared by the tenant graph and the public agent card (a2a.py), so the two
    can never disagree about what the swarm is. The registry validates its own
    consistency at import (every key, hole and handover resolvable), and
    ``GRAPH_NODES`` is built from it — so the check that remains here is the
    drift catch: an agent node whose key is not in ``AGENT_KEYS`` (and, through
    it, the registry) is a half registration, and failing loudly turns it into
    an error instead of a card with a blank panel (audit G5/I4).
    """
    for node in GRAPH_NODES:
        if node["kind"] == "agent" and str(node["key"]) not in AGENT_KEYS:
            raise RuntimeError(
                f"agent {node['key']!r} is in GRAPH_NODES but not registered in "
                "the agent registry (via modelconfig.AGENT_KEYS) — see the "
                "registration checklist"
            )
    return [
        {
            "key": str(node["key"]),
            "label": AGENT_KEYS[str(node["key"])],
            "description": AGENT_DESCRIPTIONS[str(node["key"])],
        }
        for node in GRAPH_NODES
        if node["kind"] == "agent"
    ]


@router.get("", response_model=GraphOut)
async def get_graph(tenant: TenantDep, container: ContainerDep) -> GraphOut:
    """The whole swarm as a diagram, for the owner. Read-only.

    The layout and the definitions are the same for every tenant; what varies
    is each agent's activation *state* for this tenant, resolved here in one
    call. Fail-open by design: a facts read that cannot be served leaves the
    state fields empty rather than taking the whole diagram down — the graph is
    static information first and live status second.
    """
    states: dict[str, Any] = {}
    try:
        states = await container.agent_prompts.state_for(tenant.tenant_id)
    except Exception:
        logger.warning(
            "graph_state_unavailable", tenant_id=str(tenant.tenant_id), exc_info=True
        )

    def _node_out(node: dict[str, Any]) -> GraphNodeOut:
        key = str(node["key"])
        spec = BY_KEY.get(key) if node["kind"] == "agent" else None
        state = states.get(key) if spec is not None else None
        return GraphNodeOut(
            key=key,
            label=_label(key),
            kind=str(node["kind"]),
            description=AGENT_DESCRIPTIONS.get(
                key, KIND_DESCRIPTIONS.get(str(node["kind"]), "")
            ),
            x=int(node["x"]),
            y=int(node["y"]),
            goal=spec.goal if spec else "",
            status=spec.status if spec else "",
            category=spec.category if spec else "",
            state=str(state.state) if state is not None else "",
            reason=str(state.reason) if state is not None else "",
        )

    return GraphOut(
        nodes=[_node_out(node) for node in GRAPH_NODES],
        edges=[GraphEdgeOut(**edge) for edge in GRAPH_EDGES],
    )
