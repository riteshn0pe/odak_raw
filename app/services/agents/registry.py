"""The agent registry: one place that says what every agent is.

Before this module the swarm's truth lived in three drifting copies —
``modelconfig.AGENT_KEYS`` (labels), ``graph.AGENT_DESCRIPTIONS`` (one-liners)
and each service's own module constants (prompts nobody could edit). This
registry is the single source: every one of those now derives from
:data:`AGENTS`, and the graph's request-time half-registration assert
(``app/api/v1/graph.py:agent_entries``) is backed by an import-time validation
here that fails at process start instead of when someone opens a page.

What a spec is *not*: it is not a runner. ``prompt`` is a template with
``{{holes}}`` that :class:`~app.services.agents.service.AgentPromptService`
renders per tenant at invocation; the service that executes the agent is
wherever it already lives. The spec is the card, the goal, the rules and the
activation contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.agents import defaults
from app.services.agents.facts import FACT_FIELDS, NAMESPACES
from app.services.agents.render import holes_in

# Approval features the code actually reads levels for
# (app/services/approvals/service.py FEATURE_JOBS and HitlLevelsBlock).
_APPROVAL_FEATURES = frozenset({"outbound_batch", "campaign", "review_ask"})

# The tool universe a spec may name: the builtin-things capability keys plus
# the shipped action-library keys (app/services/actions/library.LIBRARY) and
# the platform's reserved procedure tools. Spelled out here rather than
# imported, because importing the action library at module import would drag
# the whole domain layer into every process that touches the registry — the
# unit test cross-checks this list against the real ones instead.
_CAPABILITY_KEYS = frozenset(
    {
        "retrieve", "send_email", "send_whatsapp", "crm_push", "store_orders",
        "verify_email", "render_report",
    }
)
_ACTION_KEYS = frozenset(
    {
        "escalate_to_human", "collect_lead", "collect_data", "notify_slack",
        "web_search", "check_availability", "book_meeting", "suggested_replies",
        "custom_button", "lookup_order", "start_return", "issue_refund",
    }
)
TOOL_UNIVERSE = _CAPABILITY_KEYS | _ACTION_KEYS


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """One agent, fully described. See ``agent.md`` §8 for the roster's intent."""

    key: str
    name: str
    goal: str
    description: str
    category: str  # identity | customer | outbound | insight | platform
    kind: str  # chat | swarm
    status: str  # implemented | planned
    prompt: str
    requires: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    hands_over_to: tuple[str, ...] = ()
    handover_rule: str = ""
    approval_feature: str | None = None
    avoid: tuple[str, ...] = ()
    model_role: str = "primary"
    # Outbound agents are not applicable to a pure D2C tenant (type B): the
    # research/compose/follow-up pipeline has no strangers to write to.
    outbound_only: bool = False
    glyph: str = ""  # the dashboard card's icon, same vocabulary as the templates


AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        key="orchestrator",
        name="Orchestrator",
        goal="Live in the owner's Telegram chat: report on every agent, and invoke them on command.",
        description=(
            "Your chat front door: ask it about leads, reports, approvals or "
            "what the agents are doing, and it answers from live data or "
            "queues the work you name."
        ),
        category="platform",
        kind="swarm",
        status="implemented",
        prompt=defaults.ORCHESTRATOR,
        # No tenant_type: the owner may never choose one, and the front door
        # must answer regardless — nothing gates on an unchosen type.
        requires=("company_name", "languages"),
        triggers=(
            "cron:growth_dispatch", "cron:growth_report_due",
            "cron:growth_insights_due", "webhook:inbound",
        ),
        hands_over_to=(
            "growth_research", "growth_compose", "growth_followup",
            "growth_replies", "growth_inbound", "growth_campaign",
            "growth_insights", "growth_report",
        ),
        handover_rule="Never starts a blocked agent; never sends past a pending approval.",
        avoid=("exceeding send caps", "sending outside working hours"),
        glyph="◈",
    ),
    AgentSpec(
        key="growth_profile",
        name="Business identity",
        goal="Fill the tenant's fact base from the tenant's own website so every other agent can speak as this business.",
        description=(
            "Reads your website and writes down who you are: what you sell, who "
            "buys it, prices, hours, contact details and policies. Never "
            "overwrites what you typed yourself."
        ),
        category="identity",
        kind="swarm",
        status="implemented",
        prompt=defaults.GROWTH_PROFILE,
        requires=(),
        tools=("retrieve", "web_search"),
        triggers=("job:growth_profile", "crawl:ready"),
        handover_rule="Every agent reads what it writes; it hands over nothing itself.",
        avoid=("guessing a field", "overwriting the owner", "writing tone, tenant type or brand voice"),
        glyph="◎",
    ),
    AgentSpec(
        key="support",
        name="Customer support",
        goal="Answer a customer's question from this business's own knowledge, in their language, and hand over the moment it cannot.",
        description=(
            "Answers customers on WhatsApp and email from your documents, and "
            "passes the conversation to a person with the history attached."
        ),
        category="customer",
        kind="chat",
        status="implemented",
        prompt=defaults.SUPPORT,
        requires=("company_name", "sells", "audience", "languages", "escalation_contact"),
        tools=("escalate_to_human", "collect_lead", "suggested_replies"),
        triggers=("webhook:whatsapp", "webhook:email", "widget", "api"),
        hands_over_to=("growth_replies",),
        handover_rule=(
            "Growth replies run first on outreach threads; everything the documents "
            "cannot answer goes to a person via escalate_to_human."
        ),
        glyph="◆",
    ),
    AgentSpec(
        key="growth_research",
        name="Prospect research",
        goal="Find companies that match this business's customer profile, verify they are real and reachable, and write one grounded reason to contact each.",
        description=(
            "Finds new prospect companies that fit who you sell to, checks the "
            "contacts are real, and queues them for your approval."
        ),
        category="outbound",
        kind="swarm",
        status="implemented",
        prompt=defaults.GROWTH_RESEARCH,
        requires=("company_name", "sells", "audience", "icp.industry", "icp.location"),
        tools=("web_search", "verify_email", "retrieve"),
        triggers=("cron:growth_dispatch", "on_demand"),
        hands_over_to=("growth_compose",),
        handover_rule="Hands the approved batch to the first-touch writer.",
        approval_feature="outbound_batch",
        avoid=("inventing a company or contact", "contacting anyone"),
        outbound_only=True,
        glyph="⌕",
    ),
    AgentSpec(
        key="growth_compose",
        name="First-touch writer",
        goal="Write one short, specific first email per approved lead, in the tenant's own voice, citing a real reason for writing.",
        description=(
            "Writes the first email to each approved lead — short, specific, and "
            "only from facts that were actually found."
        ),
        category="outbound",
        kind="swarm",
        status="implemented",
        prompt=defaults.GROWTH_COMPOSE,
        requires=(
            "company_name", "sells", "audience", "tone", "languages",
            "sender_name", "postal_address",
        ),
        tools=("retrieve", "crm_push"),
        triggers=("approval:outbound_batch",),
        hands_over_to=("growth_followup", "growth_replies"),
        handover_rule="After send, follow-ups take over; any answer goes to reply triage.",
        approval_feature="outbound_batch",
        avoid=("claiming what research does not support", "stating a price outside the range", "filler openings"),
        outbound_only=True,
        glyph="✎",
    ),
    AgentSpec(
        key="growth_followup",
        name="Follow-up",
        goal="Add something new on each follow-up, or stop.",
        description=(
            "Sends your day-3/7/14 follow-ups — each one has to add a new fact or "
            "it does not go out."
        ),
        category="outbound",
        kind="swarm",
        status="implemented",
        prompt=defaults.GROWTH_FOLLOWUP,
        requires=(
            "company_name", "sells", "audience", "tone", "languages",
            "sender_name", "postal_address",
        ),
        tools=("retrieve", "crm_push"),
        triggers=("cron:growth_dispatch",),
        hands_over_to=("growth_replies",),
        handover_rule="Any answer goes to reply triage; silence ends the sequence.",
        approval_feature="outbound_batch",
        avoid=("repeating the previous email", "manufacturing urgency"),
        outbound_only=True,
        glyph="↻",
    ),
    AgentSpec(
        key="growth_replies",
        name="Reply triage",
        goal="Read every answer, decide what it means, and do the right small thing — draft, park, suppress, or wake the owner.",
        description=(
            "Reads every reply, sorts it (interested, later, not now, "
            "unsubscribe…) and either drafts your answer or flags it for you "
            "immediately."
        ),
        category="outbound",
        kind="swarm",
        status="implemented",
        prompt=defaults.GROWTH_REPLIES,
        requires=("company_name", "sells", "tone", "languages", "escalation_contact"),
        tools=("retrieve", "crm_push", "notify_slack"),
        triggers=("webhook:inbound",),
        hands_over_to=("growth_crm", "growth_talkprep"),
        handover_rule=(
            "Every outcome is written to the CRM; a hot lead or a meeting "
            "request wakes the owner; a rejection ends the thread."
        ),
        approval_feature="outbound_batch",
        avoid=("arguing with a rejection", "re-contacting a suppression", "promising an unpriced discount"),
        glyph="⇄",
    ),
    AgentSpec(
        key="growth_inbound",
        name="Speed to lead",
        goal="Answer a new enquiry within a minute and tell the owner.",
        description=(
            "Picks up enquiries from your website form, JustDial and IndiaMART, "
            "qualifies them once, acknowledges the customer and pings you."
        ),
        category="outbound",
        kind="swarm",
        status="implemented",
        prompt=defaults.GROWTH_INBOUND,
        requires=("company_name", "sells", "languages", "contact_phone", "hours"),
        tools=("send_whatsapp", "send_email", "crm_push", "notify_slack"),
        triggers=("webhook:ingress",),
        hands_over_to=("growth_replies",),
        handover_rule="The owner is pinged on every enquiry; a lead that answers goes to reply triage.",
        avoid=("quoting an unverifiable price or lead time"),
        glyph="⚡",
    ),
    AgentSpec(
        key="growth_campaign",
        name="Campaigns",
        goal="Turn an approved idea into messages the owner's customers actually receive.",
        description=(
            "Plans festival and promo campaigns for your customer segments, clamps "
            "the offer to your range, writes the copy and queues it for approval."
        ),
        category="outbound",
        kind="swarm",
        status="implemented",
        prompt=defaults.GROWTH_CAMPAIGN,
        requires=(
            "company_name", "sells", "audience", "tone", "languages",
            "offer_min", "offer_max", "sender_name",
        ),
        tools=("store_orders", "send_whatsapp", "send_email", "notify_slack"),
        # Only firing sites that exist. A draft starts on demand — the composer
        # endpoint (which the insights panel's "act on it" deep-links into with
        # the audience pre-filled) and the orchestrator's `draft_campaign` tool,
        # both landing on the `growth_campaign_draft` job. The approved campaign
        # is then released by `approval:campaign` (FEATURE_JOBS), and
        # `cron:growth_dispatch` is what drains the queued messages under the
        # cap and the window. An insight being acted on deliberately does *not*
        # enqueue by itself: see `insights/service.py::_draft` — the owner sees
        # the audience before anything is drafted.
        triggers=("on_demand", "approval:campaign", "cron:growth_dispatch"),
        hands_over_to=(),
        handover_rule="Nothing sends without the owner's approval.",
        approval_feature="campaign",
        avoid=("discounting outside the range", "inventing a deadline or product"),
        glyph="◐",
    ),
    AgentSpec(
        key="growth_insights",
        name="Insights watcher",
        goal="Surface only what is sourced, actionable and new — and push nothing on a quiet day.",
        description=(
            "Watches your numbers, your competitors' sites, news that matters and "
            "the festival calendar. Tells you only when something is worth acting on."
        ),
        category="insight",
        kind="swarm",
        status="implemented",
        prompt=defaults.GROWTH_INSIGHTS,
        requires=("company_name", "sells", "audience", "industry", "city"),
        tools=("web_search", "retrieve", "notify_slack"),
        triggers=("cron:growth_insights_due",),
        hands_over_to=("growth_campaign",),
        handover_rule="An insight the owner acts on becomes a campaign draft.",
        avoid=("reporting an uncomputed number", "repeating yesterday's insight"),
        glyph="◔",
    ),
    AgentSpec(
        key="growth_report",
        name="Reports",
        goal="Tell the owner what actually happened, in numbers from the database and one grounded paragraph.",
        description=(
            "Your weekly and monthly report: leads, replies, meetings, revenue, "
            "cost — as a PDF on WhatsApp and Telegram."
        ),
        category="insight",
        kind="swarm",
        status="implemented",
        prompt=defaults.GROWTH_REPORT,
        requires=("company_name", "sender_name"),
        tools=("render_report", "send_whatsapp", "send_email"),
        triggers=("cron:growth_report_due", "telegram:/report"),
        hands_over_to=(),
        handover_rule="The report is the end of its own line.",
        avoid=("stating a figure that is not a SQL result", "claiming attribution it cannot show"),
        model_role="utility",
        glyph="▤",
    ),
    AgentSpec(
        key="growth_crm",
        name="CRM sync",
        goal="Mirror every lead change to the owner's CRM, retried, never blocking.",
        description="Pushes every lead and outcome to HubSpot or Zoho.",
        category="platform",
        kind="swarm",
        status="implemented",
        prompt=defaults.GROWTH_CRM,
        requires=(),
        tools=("crm_push",),
        triggers=("lead:changed",),
        hands_over_to=(),
        handover_rule="A failed push is retried, never dropped silently.",
        glyph="⇉",
    ),
    AgentSpec(
        key="builtin_things",
        name="Capability provider",
        goal="Serve the platform's ready-made capabilities to the other agents.",
        description=(
            "Hands the other agents the platform's ready-made tools: retrieve, "
            "send, verify, push to CRM, store orders, render a report."
        ),
        category="platform",
        kind="swarm",
        status="implemented",
        prompt=defaults.BUILTIN_THINGS,
        requires=(),
        tools=tuple(sorted(_CAPABILITY_KEYS)),
        triggers=("called_by:agents",),
        hands_over_to=(),
        handover_rule="Infrastructure: it hands capabilities, not conversations.",
        avoid=("invoking a paid capability without a declared need"),
        glyph="⌸",
    ),
    AgentSpec(
        key="growth_talkprep",
        name="Talk prep",
        goal="Before a meeting with a prospect, assemble who they are, what was found, what was promised, and the open questions.",
        description="Planned. Appears as a card but does not run yet.",
        category="insight",
        kind="swarm",
        status="planned",
        prompt=defaults.GROWTH_TALKPREP,
        requires=(),
        triggers=("meeting:upcoming",),
        hands_over_to=(),
        handover_rule="Planned — not built.",
        glyph="◌",
    ),
    AgentSpec(
        key="growth_reviews",
        name="Review ask",
        goal="After a delivered order, decide whether and how to ask the customer for a review, respecting the suppression list and the owner's approval level.",
        description="Planned. Appears as a card but does not run yet.",
        category="outbound",
        kind="swarm",
        status="planned",
        prompt=defaults.GROWTH_REVIEWS,
        requires=(),
        triggers=("order:delivered",),
        hands_over_to=(),
        handover_rule="Planned — not built.",
        approval_feature="review_ask",
        glyph="◌",
    ),
)

BY_KEY: dict[str, AgentSpec] = {spec.key: spec for spec in AGENTS}

# The labels and one-liners the old three copies carried, derived once.
AGENT_KEYS: dict[str, str] = {spec.key: spec.name for spec in AGENTS}
AGENT_DESCRIPTIONS: dict[str, str] = {
    spec.key: spec.description for spec in AGENTS
}
# The swarm rows the dashboard materializes: everything with a card except the
# chat kind, whose persona is the tenant's own agent row.
SWARM_SPECS: tuple[AgentSpec, ...] = tuple(
    spec for spec in AGENTS if spec.kind == "swarm"
)


def validate_registry() -> None:
    """Fail at import when a spec contradicts the code it depends on.

    Everything checked here is a silent failure otherwise: a ``{{company.nope}}``
    typo renders as an empty string in a sent email, a ``hands_over_to`` key that
    does not exist draws a dead edge, an ``approval_feature`` typo reads as
    level 0. Loud at startup is the cheapest place any of these can fail.
    """
    problems: list[str] = []

    for spec in AGENTS:
        where = spec.key
        if not spec.name.strip():
            problems.append(f"{where}: empty name")
        if not spec.goal.strip():
            problems.append(f"{where}: empty goal")
        if spec.kind not in ("chat", "swarm"):
            problems.append(f"{where}: unknown kind {spec.kind!r}")
        if spec.status not in ("implemented", "planned"):
            problems.append(f"{where}: unknown status {spec.status!r}")
        if spec.model_role not in ("primary", "utility"):
            problems.append(f"{where}: unknown model_role {spec.model_role!r}")
        if spec.approval_feature is not None and spec.approval_feature not in _APPROVAL_FEATURES:
            problems.append(f"{where}: unknown approval_feature {spec.approval_feature!r}")
        for path in spec.requires:
            if path not in FACT_FIELDS:
                problems.append(f"{where}: requires unknown fact {path!r}")
        for tool in spec.tools:
            if tool not in TOOL_UNIVERSE:
                problems.append(f"{where}: unknown tool {tool!r}")
        for key in spec.hands_over_to:
            if key not in BY_KEY:
                problems.append(f"{where}: hands over to unknown agent {key!r}")
        for hole in holes_in(spec.prompt):
            namespace, _, rest = hole.partition(".")
            if namespace not in NAMESPACES:
                problems.append(f"{where}: hole {hole!r} uses unknown namespace")
            elif namespace == "company" and rest not in FACT_FIELDS:
                problems.append(f"{where}: hole {hole!r} names unknown fact")
            elif namespace == "owner" and rest not in ("name", "email"):
                problems.append(f"{where}: hole {hole!r} names unknown owner field")

    if len(BY_KEY) != len(AGENTS):
        problems.append("duplicate agent keys in the registry")

    if problems:
        raise RuntimeError(
            "agent registry invalid:\n  - " + "\n  - ".join(problems)
        )


validate_registry()
