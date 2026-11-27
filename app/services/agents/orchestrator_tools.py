"""The orchestrator's tool set: the dashboard's reads and buttons, as tools.

Every tool is one thing the owner can already do on the panel, phrased for the
model: the reads are the panel's screens (leads, report, agent states,
approvals, insights, campaign stats, message counts, the profile) and the
actions are the panel's buttons (run report, find leads, draft a campaign,
look now, approve/reject). No tool invents a capability the panel does not
have — the orchestrator is a new front door, not a new feature.

``get_agent_activity`` is the one deliberate exception, and it earns the
exception by being a read of records the platform already keeps rather than a
new capability: the decisions log and the tool-call ledger are both written by
the work itself, and neither has a screen. The owner asking "what have you
done" is the only way to reach them without a database client.

Shape rules, stated once:

* every tool is ``async (container, tenant_id, **kwargs) -> dict`` and all DB
  access happens inside the repos/services it calls, under their own
  ``tenant_scope`` — no SQL here, and never raw SQL in the prompt;
* every failure is a ``{"error": …}`` return, never a raise: one tool failing
  must cost the owner one apologetic line, not the whole turn;
* the queued actions only queue. The job gate in ``app/api/internal/jobs.py``
  still decides whether a gated agent runs, and the tool's job is to report
  what it queued — the model's job is to phrase it, not to claim it finished.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.logging import get_logger

logger = get_logger(__name__)

# The read tools are bounded the way the panel's screens are bounded: a chat
# window cannot use fifty rows, and a tool result is prompt prefill.
DEFAULT_ROWS = 20
MAX_ROWS = 50


def _limit(value: Any, default: int, cap: int) -> int:
    """A row count from the model, clamped. A nonsense value is the default,
    not an error — the model retries on the error text and burns a round."""
    try:
        count = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(1, min(count, cap))


def _since(days: Any) -> datetime | None:
    """A ``days`` window from the model into a UTC cutoff, or None for all time."""
    if days in (None, "", 0):
        return None
    try:
        window = int(days)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if window <= 0:
        return None
    return datetime.now(UTC) - timedelta(days=window)


# count_sent_since takes a required datetime, so "all time" is the epoch rather
# than a None — the column has no meaningful earlier value.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _opt_str(value: Any) -> str | None:
    """A filter value the model may omit: None for "no filter"."""
    text = str(value or "").strip()
    return text or None


def _aware(value: Any) -> datetime | None:
    """A DB datetime normalised to UTC — rows may come back naive depending on
    the driver, and a naive/aware comparison raises rather than filtering."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# ------------------------------------------------------------------ read tools


async def list_leads(
    container: Any, tenant_id: UUID, *, status: Any = None, days: Any = None,
    limit: Any = None,
) -> dict[str, Any]:
    """The leads board: recent rows plus the by-status counters.

    ``leads.list`` has no date filter, so the window is applied here on
    ``created_at`` — the rows fetched are bounded by ``limit`` either way."""
    rows = await container.growth.leads.list(
        tenant_id,
        status=_opt_str(status),
        limit=_limit(limit, DEFAULT_ROWS, MAX_ROWS),
    )
    since = _since(days)
    if since is not None:
        rows = [row for row in rows if (created := _aware(row.get("created_at"))) and created >= since]
    by_status = await container.growth.leads.count_by_status(tenant_id)
    return {
        "leads": [
            {
                "company": row.get("company"),
                "person": row.get("person"),
                "email": row.get("email"),
                "status": row.get("status"),
                "source": row.get("source"),
                "created_at": str(row.get("created_at") or ""),
            }
            for row in rows
        ],
        "returned": len(rows),
        "by_status": by_status,
        "total": sum(by_status.values()),
    }


async def get_latest_report(container: Any, tenant_id: UUID) -> dict[str, Any]:
    """The newest report row: period, status, summary and its computed numbers."""
    rows = await container.growth.reports.list(tenant_id, limit=1)
    if not rows:
        return {"report": None, "note": "no report has been built yet"}
    row = rows[0]
    return {
        "period": row.get("period"),
        "period_start": str(row.get("period_start") or ""),
        "status": row.get("status"),
        "summary": row.get("summary"),
        "numbers": row.get("numbers"),
    }


async def get_agent_states(container: Any, tenant_id: UUID) -> dict[str, Any]:
    """Every agent's activation state — the same map the dashboard cards show."""
    from app.services.agents.registry import AGENT_KEYS

    states = await container.agent_prompts.state_for(tenant_id)
    return {
        "agents": [
            {
                "key": state.key,
                "name": AGENT_KEYS.get(state.key, state.key),
                "state": state.state,
                "reason": state.reason,
            }
            for state in states.values()
        ]
    }


async def list_pending_approvals(container: Any, tenant_id: UUID) -> dict[str, Any]:
    """What is waiting on the owner — the Approvals screen's list."""
    rows = await container.approvals.pending_for(tenant_id, limit=10)
    return {
        "approvals": [
            {
                "id": row.get("id"),
                "feature": row.get("feature"),
                "summary": (row.get("payload") or {}).get("summary"),
                "status": row.get("status"),
                "created_at": str(row.get("created_at") or ""),
            }
            for row in rows
        ]
    }


async def list_insights(container: Any, tenant_id: UUID) -> dict[str, Any]:
    """The open insights — the Insights screen's ranked list."""
    listed = await container.growth_insights.list(tenant_id, limit=10, open_only=True)
    return {
        "insights": [
            {
                "id": item.get("id"),
                "summary": item.get("summary"),
                "date": item.get("date"),
            }
            for item in listed.get("items") or []
        ],
        "open": listed.get("open"),
    }


async def campaign_stats(
    container: Any, tenant_id: UUID, *, days: Any = None
) -> dict[str, Any]:
    """Recent campaigns with their per-status message counts."""
    since = _since(days)
    rows = await container.growth.campaigns.list(tenant_id, limit=10)
    campaigns: list[dict[str, Any]] = []
    for row in rows:
        created = _aware(row.get("created_at"))
        if since is not None and created is not None and created < since:
            continue
        stats = await container.growth.messages.campaign_stats(
            tenant_id, UUID(str(row["id"]))
        )
        campaigns.append(
            {
                "name": row.get("name"),
                "status": row.get("status"),
                "created_at": str(row.get("created_at") or ""),
                "messages": stats,
            }
        )
    return {"campaigns": campaigns}


async def messages_sent(
    container: Any, tenant_id: UUID, *, days: Any = None
) -> dict[str, Any]:
    """Outbound message count: everything that left, including bounces — the
    same count the daily send cap is measured against."""
    since = _since(days) or _EPOCH
    sent = await container.growth.messages.count_sent_since(tenant_id, since)
    return {"sent": sent}


async def get_profile_facts(container: Any, tenant_id: UUID) -> dict[str, Any]:
    """The whoami agent's fact base: what every other agent reads."""
    settings = await container.growth_profile.get(tenant_id)
    return {"profile": settings.get("profile") or {}}


async def get_agent_activity(
    container: Any, tenant_id: UUID, *, agent: Any = None, days: Any = None,
    limit: Any = None,
) -> dict[str, Any]:
    """What the agents have actually done, newest first — the two logs together.

    Two sources, because they answer different halves of one question. The
    decisions log carries what each agent chose and the reason it wrote at the
    time, including the work it declined — the row that makes an observability
    claim checkable rather than decorative. The tool-call ledger carries what
    the answer path actually dispatched, with its status and latency.

    Neither invents activity: an agent with no rows returns an empty list, and
    that empty list is the answer. ``agent`` narrows the decisions log only —
    the ledger names its caller by id rather than by key, so filtering it by a
    key would quietly mean something else, and quietly wrong is worse than
    unfiltered.
    """
    count = _limit(limit, DEFAULT_ROWS, MAX_ROWS)
    since = _since(days)

    rows = await container.growth.audit.recent(tenant_id, agent=_opt_str(agent), limit=count)
    if since is not None:
        # The window is applied here rather than in the repo because the repo has
        # no date filter — the same trade `list_leads` makes, and a row that
        # cannot be dated is dropped rather than silently counted as recent.
        rows = [
            row
            for row in rows
            if (at := _aware(row.get("created_at"))) is not None and at >= since
        ]
    activity: list[dict[str, Any]] = [
        {
            "at": str(row.get("created_at") or ""),
            "agent": row.get("agent"),
            "action": row.get("action"),
            "why": row.get("reason"),
        }
        for row in rows
    ]

    # The ledger is optional in two ways: a tenant with no actions wired has the
    # repo and no rows, and a container that does not carry one at all should
    # still answer from the decisions log rather than fail the whole tool.
    calls: list[dict[str, Any]] = []
    repo = getattr(container, "actions_repo", None)
    if repo is not None:
        for row in await repo.recent_calls(tenant_id, limit=count):
            at = _aware(row.get("ts"))
            if since is not None and at is not None and at < since:
                continue
            calls.append(
                {
                    "at": str(row.get("ts") or ""),
                    "action": row.get("action_name"),
                    "status": row.get("status"),
                    "ms": row.get("duration_ms"),
                    "error": row.get("error_message"),
                }
            )

    return {"activity": activity, "tool_calls": calls, "returned": len(activity)}


# --------------------------------------------------------------- action tools


async def run_report(container: Any, tenant_id: UUID) -> dict[str, Any]:
    """The Run now button: the deduped on-demand path, shared with /report and
    the panel. The report job's own fan-out delivers the PDF to this chat."""
    return await container.growth_reports.request_on_demand(tenant_id, via="telegram")


async def run_research(
    container: Any, tenant_id: UUID, *, query: Any = None, limit: Any = None
) -> dict[str, Any]:
    """The Find leads button: queue a research run. The batch arrives for
    approval; whether it runs at all is the job gate's call, not this one's."""
    from app.adapters.queue.base import Job

    payload = {
        "query_seed": str(query or ""),
        "limit": _limit(limit, 25, 25),
    }
    await container.queue.enqueue(
        Job(kind="growth_research", tenant_id=tenant_id, payload=payload)
    )
    return {"status": "queued", "job": "growth_research", "note": "the batch arrives for approval"}


async def draft_campaign(
    container: Any, tenant_id: UUID, *, goal: Any = "", audience: Any = None
) -> dict[str, Any]:
    """Queue a campaign draft, the same job an acted-on insight enqueues. The
    draft lands in approvals before anything is sent."""
    from app.adapters.queue.base import Job
    from app.domain.segments import SEGMENT_KEYS

    name = str(goal or "").strip() or "Telegram campaign"
    key = str(audience or "").strip()
    if key not in SEGMENT_KEYS:
        # an unknown segment is refused by the planner, which would surface as
        # a skipped job the owner never sees — the default segment keeps the
        # queue request honest instead
        key = "new"
    await container.queue.enqueue(
        Job(
            kind="growth_campaign_draft",
            tenant_id=tenant_id,
            payload={"name": name[:200], "trigger": "owner", "segment": {"key": key}},
        )
    )
    return {"status": "queued", "job": "growth_campaign_draft", "audience": key}


async def refresh_insights(container: Any, tenant_id: UUID) -> dict[str, Any]:
    """The Look now button: queue an insights pass, same as the cron's."""
    return await container.growth_insights.request(tenant_id, via="telegram")


async def decide_approval(
    container: Any, tenant_id: UUID, *, approval_id: Any, approve: Any = True
) -> dict[str, Any]:
    """Approve or reject a pending approval — the same decide path the panel
    and the chat's own buttons use.

    No second confirmation: this tool set only ever runs inside the owner's
    chat, behind the sender gate, and the message asking for the decision is
    the confirmation.
    """
    decision = "approved" if str(approve).lower() not in ("false", "no", "0", "reject", "rejected") else "rejected"
    row = await container.approvals.decide(
        tenant_id,
        UUID(str(approval_id)),
        decision=decision,
        decided_by="telegram",
    )
    return {"status": row.get("status"), "feature": row.get("feature"), "decision": decision}


# ------------------------------------------------------------------ dispatch

TOOLS: dict[str, Any] = {
    list_leads.__name__: list_leads,
    get_latest_report.__name__: get_latest_report,
    get_agent_states.__name__: get_agent_states,
    list_pending_approvals.__name__: list_pending_approvals,
    list_insights.__name__: list_insights,
    campaign_stats.__name__: campaign_stats,
    messages_sent.__name__: messages_sent,
    get_profile_facts.__name__: get_profile_facts,
    get_agent_activity.__name__: get_agent_activity,
    run_report.__name__: run_report,
    run_research.__name__: run_research,
    draft_campaign.__name__: draft_campaign,
    refresh_insights.__name__: refresh_insights,
    decide_approval.__name__: decide_approval,
}


def _spec(
    name: str, description: str, properties: dict[str, Any], required: tuple[str, ...] = ()
) -> dict[str, Any]:
    """The canonical Anthropic tool triple; the OpenAI-family adapters
    translate on the way out, so this is the one shape the LLM port carries."""
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
        },
    }


TOOL_SPECS: tuple[dict[str, Any], ...] = (
    _spec(
        "list_leads",
        "Recent leads on the board, with the by-status counters.",
        {
            "status": {"type": "string", "description": "filter by one lead status"},
            "days": {"type": "integer", "description": "only leads created in the last N days"},
            "limit": {"type": "integer", "description": f"rows to return, at most {MAX_ROWS}"},
        },
    ),
    _spec(
        "get_latest_report",
        "The most recent report: period, status, summary and its computed numbers.",
        {},
    ),
    _spec(
        "get_agent_states",
        "Every agent's activation state (active/blocked/disabled) and the reason.",
        {},
    ),
    _spec(
        "list_pending_approvals",
        "The approvals waiting on the owner right now.",
        {},
    ),
    _spec(
        "list_insights",
        "The open insights the watchers surfaced, most urgent first.",
        {},
    ),
    _spec(
        "campaign_stats",
        "Recent campaigns with their message counts by status.",
        {"days": {"type": "integer", "description": "only campaigns created in the last N days"}},
    ),
    _spec(
        "messages_sent",
        "Outbound message counts, by status and by channel.",
        {"days": {"type": "integer", "description": "only messages sent in the last N days"}},
    ),
    _spec(
        "get_profile_facts",
        "The business profile every agent reads: what it sells, to whom, prices, voice.",
        {},
    ),
    _spec(
        "get_agent_activity",
        "What the agents have actually done, newest first: every decision with the "
        "reason the agent wrote at the time, and every tool call that was dispatched.",
        {
            "agent": {
                "type": "string",
                "description": "filter by one agent key, e.g. growth_compose",
            },
            "days": {"type": "integer", "description": "only activity from the last N days"},
            "limit": {"type": "integer", "description": f"rows to return, at most {MAX_ROWS}"},
        },
    ),
    _spec(
        "run_report",
        "Queue the on-demand report; the PDF is delivered to this chat when ready.",
        {},
    ),
    _spec(
        "run_research",
        "Queue a prospect research run; the batch arrives for your approval.",
        {
            "query": {"type": "string", "description": "optional seed for the search"},
            "limit": {"type": "integer", "description": "how many leads to look for, at most 25"},
        },
    ),
    _spec(
        "draft_campaign",
        "Queue a campaign draft for a customer segment; it lands in approvals before anything sends.",
        {
            "goal": {"type": "string", "description": "what the campaign is for, in a few words"},
            "audience": {
                "type": "string",
                "description": "customer segment: new, repeat, high_value, at_risk, lost",
            },
        },
    ),
    _spec(
        "refresh_insights",
        "Queue an insights pass now, same as the daily run.",
        {},
    ),
    _spec(
        "decide_approval",
        "Approve or reject a pending approval by its id.",
        {
            "approval_id": {"type": "string", "description": "the approval's id from list_pending_approvals"},
            "approve": {"type": "boolean", "description": "true to approve, false to reject"},
        },
        ("approval_id",),
    ),
)


async def run_tool(
    container: Any, tenant_id: UUID, name: str, arguments: dict[str, Any] | None
) -> dict[str, Any]:
    """Dispatch one tool call. Never raises: an unknown name or a failing tool
    is a result the model gets to explain, the same contract the chat loop's
    dispatcher keeps."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"'{name}' is not available. Answer with what you have."}
    try:
        return await tool(container, tenant_id, **dict(arguments or {}))
    except Exception as exc:
        logger.warning(
            "orchestrator_tool_failed",
            tool=name,
            tenant_id=str(tenant_id),
            error=type(exc).__name__,
            exc_info=True,
        )
        return {"error": f"{name} failed: {exc}"}
