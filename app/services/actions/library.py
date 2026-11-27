"""The starter action library: the platform's actions as data, plus the handlers.

Two things live here. `build_handlers(service)` is what
`ActionService._handler_for` imports — a mapping from `system_handler` value to
the coroutine the dispatcher awaits. And `LIBRARY` is the shipped catalogue: the
nine prebuilt actions from §2.1-§2.8 and the three tenant-scaffolded HTTP
templates from §2.9, as `ActionDefinition` values.

The catalogue is code rather than a platform table, for the reason §2.9 gives:
shipping a new template should be a deploy, not a data migration, and a platform
table holding tenant-facing rows would need RLS it cannot have. `library_key`
on the `actions` row records which entry seeded it.

The scaffolded templates point at the `.invalid` TLD, which can never resolve, so
an accidental enable cannot become an SSRF or a call to a stranger's API. They
carry `setup_required=True` so the UI can badge "Finish setup" deterministically
rather than inferring it from the URL.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.core.constants import Channel
from app.core.logging import get_logger
from app.domain.actions import (
    SYSTEM_HANDLERS,
    ActionAuth,
    ActionDefinition,
    ActionEffect,
    ActionHttp,
    ActionKind,
    ActionParameter,
    ActionResponse,
    DataAccess,
    ParamType,
    WidgetRender,
)
from app.services.actions.handlers import FACTORIES, Handler

logger = get_logger(__name__)

ALL_CHANNELS = [Channel.WIDGET, Channel.WHATSAPP, Channel.API]
WIDGET_ONLY = [Channel.WIDGET]
# Never resolvable, by IANA reservation. An accidentally enabled scaffold fails
# DNS instead of reaching someone else's API.
SCAFFOLD_BASE = "https://api.example.invalid"


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    """One catalogue entry, and the install policy that comes with it."""

    key: str
    definition: ActionDefinition
    # whether a fresh install turns it on. The agent templates in the UI
    # pre-enable a subset; anything needing a credential starts off.
    enabled_by_default: bool = False
    # a real column on `actions`, not inferred from the URL
    setup_required: bool = False
    # what the tenant must connect before the row is offered to a model. Shown on
    # the Actions card as the reason it reads "Not configured".
    requires: str = ""


def _p(
    name: str,
    description: str,
    *,
    type: ParamType = ParamType.STRING,
    required: bool = False,
    max_length: int = 200,
    enum: list[str] | None = None,
) -> ActionParameter:
    """Terse parameter constructor. Reads as a table at the call sites below."""
    return ActionParameter(
        name=name,
        description=description,
        type=type,
        required=required,
        max_length=max_length,
        enum=enum or [],
    )


# --------------------------------------------------------- system handlers

ESCALATE_TO_HUMAN = ActionDefinition(
    name="escalate_to_human",
    display_name="Hand over to a person",
    description=(
        "Hand this conversation to a human on the team. Use this when the visitor "
        "asks for a person, when you cannot answer from the documents, when the "
        "visitor is upset or the matter is sensitive, or when a procedure tells you "
        "to. Summarise the problem in `reason` — the team sees only what you write "
        "plus the transcript."
    ),
    kind=ActionKind.SYSTEM,
    effect=ActionEffect.LOCAL,
    system_handler="escalate_to_human",
    parameters=[
        _p(
            "reason",
            "Why this needs a person, in one or two sentences. 10-500 characters.",
            required=True,
            max_length=500,
        ),
        _p(
            "urgency",
            "How quickly a person is needed. Use high only when someone is stuck "
            "or upset.",
            type=ParamType.ENUM,
            enum=["normal", "high"],
        ),
        _p(
            "summary",
            "What has been tried so far, so the person does not repeat it.",
            max_length=1000,
        ),
    ],
    channels=ALL_CHANNELS,
    rate_limit_per_minute=6,
)

COLLECT_LEAD = ActionDefinition(
    name="collect_lead",
    display_name="Save contact details",
    description=(
        "Save the visitor's contact details so the team can follow up. Only call "
        "this after the visitor has given you the details and you have read them "
        "back to them. Do not guess an email address."
    ),
    kind=ActionKind.SYSTEM,
    effect=ActionEffect.LOCAL,
    system_handler="collect_lead",
    parameters=[
        _p("name", "The visitor's full name, as they gave it.", max_length=120),
        _p(
            "email",
            "The visitor's email address, exactly as they spelled it out.",
            type=ParamType.EMAIL,
        ),
        _p("phone", "The visitor's phone number, with country code if given.", max_length=32),
        _p("company", "The company the visitor is with, if they mentioned one.", max_length=120),
        _p("note", "One line on what they want, for the person who follows up.", max_length=500),
    ],
    channels=ALL_CHANNELS,
    rate_limit_per_minute=6,
)

COLLECT_DATA = ActionDefinition(
    name="collect_data",
    display_name="Record request details",
    description=(
        "Record the details the team needs for this request. Ask for the fields "
        "conversationally, one or two at a time — never as a form. Call this once "
        "you have them all."
    ),
    kind=ActionKind.SYSTEM,
    effect=ActionEffect.LOCAL,
    system_handler="collect_data",
    # The only system action whose input schema the tenant defines. This one
    # starter field keeps it functional before the tenant has declared theirs;
    # `setup_required` is what asks them to.
    parameters=[
        _p(
            "request_details",
            "What the visitor is asking for, in their own words.",
            required=True,
            max_length=500,
        )
    ],
    channels=ALL_CHANNELS,
    rate_limit_per_minute=10,
)

NOTIFY_SLACK = ActionDefinition(
    name="notify_slack",
    display_name="Post to Slack",
    description=(
        "Post a short note to the team's Slack channel. Use this when a procedure "
        "tells you to, or when something needs a human's attention but the visitor "
        "does not need to wait for a reply."
    ),
    kind=ActionKind.SYSTEM,
    effect=ActionEffect.LOCAL,
    system_handler="notify_slack",
    parameters=[
        _p(
            "message",
            "What the team needs to know, in 10-1000 characters.",
            required=True,
            max_length=1000,
        ),
        _p(
            "urgency",
            "Use high only when someone should look now.",
            type=ParamType.ENUM,
            enum=["normal", "high"],
        ),
    ],
    channels=ALL_CHANNELS,
    rate_limit_per_minute=10,
)

WEB_SEARCH = ActionDefinition(
    name="web_search",
    display_name="Search the web",
    description=(
        "Search the public web. Use this only when the answer is not in the "
        "documents and is likely to have changed recently — prices, availability, "
        "news. Say in your answer that the information came from the web."
    ),
    kind=ActionKind.SYSTEM,
    effect=ActionEffect.READ,
    system_handler="web_search",
    parameters=[
        _p(
            "query",
            "What to search for, 3-200 characters. Keep it specific.",
            required=True,
            max_length=200,
        )
    ],
    channels=ALL_CHANNELS,
    rate_limit_per_minute=10,
)

CHECK_AVAILABILITY = ActionDefinition(
    name="check_availability",
    display_name="Check available times",
    description=(
        "Look up real open appointment times before offering any to the visitor. "
        "Never suggest a time you have not seen here. Ask for a date range first if "
        "the visitor has not given one."
    ),
    kind=ActionKind.SYSTEM,
    effect=ActionEffect.READ,
    system_handler="check_availability",
    parameters=[
        _p(
            "service",
            "Which service or meeting type, if the tenant offers more than one.",
            max_length=120,
        ),
        _p(
            "from_date",
            "First date to look from, as YYYY-MM-DD.",
            type=ParamType.DATE,
            required=True,
            max_length=32,
        ),
        _p(
            "to_date",
            "Last date to look at, as YYYY-MM-DD. Up to seven days after from_date.",
            type=ParamType.DATE,
            max_length=32,
        ),
    ],
    channels=ALL_CHANNELS,
    rate_limit_per_minute=12,
)

BOOK_MEETING = ActionDefinition(
    name="book_meeting",
    display_name="Book a meeting",
    description=(
        "Book a meeting at a time the visitor has agreed to. Only call this after "
        "`check_availability` has given you real slots and the visitor has picked "
        "one. Never invent a time."
    ),
    kind=ActionKind.SYSTEM,
    effect=ActionEffect.WRITE,
    system_handler="book_meeting",
    requires_confirmation=True,
    parameters=[
        _p(
            "slot_start",
            "The exact `start` value of the slot the visitor picked, ISO 8601 with "
            "an offset.",
            required=True,
            max_length=40,
        ),
        _p("name", "The name to put on the booking.", required=True, max_length=120),
        _p(
            "email",
            "The email address for the confirmation, read back to the visitor "
            "first.",
            type=ParamType.EMAIL,
            required=True,
        ),
        _p("phone", "A phone number for the booking, if the visitor gave one.", max_length=32),
        _p("notes", "Anything the person taking the meeting should know.", max_length=500),
        _p("service", "Which service or meeting type this booking is for.", max_length=120),
    ],
    channels=ALL_CHANNELS,
    rate_limit_per_minute=6,
)


# ---------------------------------------------------------- widget actions

# No parameters on either widget action, and that is forced rather than chosen:
# `ActionDefinition._kind_coherent` refuses parameters on a widget whose render is
# not a form. So the tenant authors the content and the model decides only
# whether to render — which is also the §2.8 rule for the button's URL.
SUGGESTED_REPLIES = ActionDefinition(
    name="suggested_replies",
    display_name="Offer quick replies",
    description=(
        "Offer the visitor up to four short replies to tap instead of typing. Use "
        "them for clear next steps, not as a substitute for answering."
    ),
    kind=ActionKind.WIDGET,
    effect=ActionEffect.READ,
    render=WidgetRender(
        type="suggested_replies",
        label="Quick replies",
        options=["Talk to a person", "Something else"],
    ),
    channels=WIDGET_ONLY,
    rate_limit_per_minute=20,
)

CUSTOM_BUTTON = ActionDefinition(
    name="custom_button",
    display_name="Show a button",
    description=(
        "Show the visitor a button that opens a page — a tracking link, a payment "
        "page, a booking page. Only use it when a procedure or the agent's "
        "instructions say to."
    ),
    kind=ActionKind.WIDGET,
    effect=ActionEffect.READ,
    # The URL is fixed at authoring time and is never model-supplied: the model
    # decides whether to render, not where it points. HTTPS is enforced by
    # WidgetRender and re-validated by the widget before it sets href.
    render=WidgetRender(
        type="button",
        label="Open",
        url=f"{SCAFFOLD_BASE}/link",
    ),
    channels=WIDGET_ONLY,
    rate_limit_per_minute=20,
)


# ------------------------------------------------- tenant-scaffolded HTTP

LOOKUP_ORDER = ActionDefinition(
    name="lookup_order",
    display_name="Look up an order",
    description=(
        "Look up the status of an order the visitor gives you the number for. Ask "
        "for the order number if they have not given one; do not guess it."
    ),
    kind=ActionKind.SERVER,
    effect=ActionEffect.READ,
    parameters=[
        _p(
            "order_id",
            "The order number, exactly as the visitor gave it.",
            required=True,
            max_length=64,
        ),
        _p(
            "email",
            "The email address on the order, if the shop needs it as well.",
            type=ParamType.EMAIL,
        ),
    ],
    http=ActionHttp(
        method="GET",
        url=f"{SCAFFOLD_BASE}/orders/{{{{input.order_id}}}}",
        timeout_seconds=10.0,
    ),
    # Prefilled projection: only these leaves reach the prompt, so a field the
    # tenant never chose stays invisible to the model.
    response=ActionResponse(
        data_access=DataAccess.LIMITED,
        allowed_fields=[
            "status",
            "carrier",
            "tracking_url",
            "placed_at",
            "total",
            "items",
        ],
    ),
    auth=ActionAuth(),
    channels=ALL_CHANNELS,
)

START_RETURN = ActionDefinition(
    name="start_return",
    display_name="Start a return",
    description=(
        "Start a return for an item on an order. Confirm the order number, the item "
        "and the reason with the visitor before calling this."
    ),
    kind=ActionKind.SERVER,
    effect=ActionEffect.WRITE,
    requires_confirmation=True,
    parameters=[
        _p("order_id", "The order the return is for.", required=True, max_length=64),
        _p("item", "Which item is being returned.", required=True, max_length=200),
        _p(
            "reason",
            "Why it is being returned, in the visitor's words.",
            required=True,
            max_length=300,
        ),
    ],
    http=ActionHttp(
        method="POST",
        url=f"{SCAFFOLD_BASE}/returns",
        body={
            "order_id": "{{input.order_id}}",
            "item": "{{input.item}}",
            "reason": "{{input.reason}}",
        },
        timeout_seconds=10.0,
    ),
    auth=ActionAuth(),
    channels=ALL_CHANNELS,
)

ISSUE_REFUND = ActionDefinition(
    name="issue_refund",
    display_name="Issue a refund",
    description=(
        "Issue a refund against an order. Only a refund procedure may trigger this, "
        "and only after the visitor has confirmed the amount."
    ),
    kind=ActionKind.SERVER,
    effect=ActionEffect.WRITE,
    requires_confirmation=True,
    only_in_procedures=True,
    parameters=[
        _p("order_id", "The order being refunded.", required=True, max_length=64),
        _p(
            "amount",
            "The amount to refund, in the shop's currency.",
            type=ParamType.NUMBER,
            required=True,
        ),
        _p("reason", "Why the refund is being issued.", required=True, max_length=300),
    ],
    http=ActionHttp(
        method="POST",
        url=f"{SCAFFOLD_BASE}/refunds",
        body={
            "order_id": "{{input.order_id}}",
            "amount": "{{input.amount}}",
            "reason": "{{input.reason}}",
        },
        timeout_seconds=10.0,
    ),
    auth=ActionAuth(),
    channels=ALL_CHANNELS,
)


# ------------------------------------------------------------- the catalogue

LIBRARY: tuple[LibraryEntry, ...] = (
    LibraryEntry(
        key="escalate_to_human",
        definition=ESCALATE_TO_HUMAN,
        enabled_by_default=True,
        requires="escalation",
    ),
    LibraryEntry(
        key="collect_lead", definition=COLLECT_LEAD, enabled_by_default=True
    ),
    LibraryEntry(
        key="collect_data",
        definition=COLLECT_DATA,
        enabled_by_default=True,
        setup_required=True,
    ),
    LibraryEntry(
        key="notify_slack",
        definition=NOTIFY_SLACK,
        setup_required=True,
        requires="slack_webhook",
    ),
    LibraryEntry(
        key="web_search",
        definition=WEB_SEARCH,
        enabled_by_default=True,
        requires="web_search_provider",
    ),
    LibraryEntry(
        key="check_availability",
        definition=CHECK_AVAILABILITY,
        setup_required=True,
        requires="calendar",
    ),
    LibraryEntry(
        key="book_meeting",
        definition=BOOK_MEETING,
        setup_required=True,
        requires="calendar",
    ),
    LibraryEntry(
        key="suggested_replies",
        definition=SUGGESTED_REPLIES,
        enabled_by_default=True,
    ),
    LibraryEntry(
        key="custom_button", definition=CUSTOM_BUTTON, setup_required=True
    ),
    LibraryEntry(key="lookup_order", definition=LOOKUP_ORDER, setup_required=True),
    LibraryEntry(key="start_return", definition=START_RETURN, setup_required=True),
    LibraryEntry(key="issue_refund", definition=ISSUE_REFUND, setup_required=True),
)

BY_KEY: Mapping[str, LibraryEntry] = {entry.key: entry for entry in LIBRARY}

# §2.10's checklist. The agent templates pre-enable exactly these ten ids;
# `check_availability` installs as `book_meeting`'s companion and no template
# references it directly, and `issue_refund` is here because the refund_request
# procedure template needs it.
TEMPLATE_IDS: tuple[str, ...] = (
    "escalate_to_human",
    "suggested_replies",
    "collect_lead",
    "collect_data",
    "notify_slack",
    "web_search",
    "book_meeting",
    "custom_button",
    "lookup_order",
    "start_return",
)


def entry(key: str) -> LibraryEntry | None:
    """One catalogue entry by `library_key`."""
    return BY_KEY.get(key)


def definitions() -> tuple[ActionDefinition, ...]:
    """Every shipped definition, in install order."""
    return tuple(item.definition for item in LIBRARY)


def missing_template_ids() -> tuple[str, ...]:
    """§2.10's checklist, as a check rather than a comment.

    An id an agent template pre-enables but the library does not ship would
    install an agent with a dangling reference, which shows up as a tool the model
    is told about and cannot call. Returning the gap lets the installer refuse.
    """
    return tuple(name for name in TEMPLATE_IDS if name not in BY_KEY)


def unhandled_system_actions() -> tuple[str, ...]:
    """Shipped `system` definitions with no handler behind them.

    Catches the one mistake this split invites: adding a definition whose
    `system_handler` is not in `SYSTEM_HANDLERS` or has no factory in
    `app.services.actions.handlers`.
    """
    gaps: list[str] = []
    for item in LIBRARY:
        handler = item.definition.system_handler
        if item.definition.kind is not ActionKind.SYSTEM or handler is None:
            continue
        if handler not in SYSTEM_HANDLERS or handler not in FACTORIES:
            gaps.append(item.key)
    return tuple(gaps)


def build_handlers(service: object) -> Mapping[str, Handler]:
    """The dispatcher's entry point: `system_handler` value -> handler.

    Keyed by handler name rather than action name, because a tenant may rename an
    action and the code behind it is still ours. A factory that raises is skipped
    rather than allowed to take the whole library down: one broken handler should
    cost one action, not every system action on the deployment.
    """
    handlers: dict[str, Handler] = {}
    for name, factory in FACTORIES.items():
        try:
            handlers[name] = factory(service)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("action_handler_build_failed", handler=name, error=str(exc))
    gaps = unhandled_system_actions()
    if gaps:
        logger.warning("action_library_unhandled", actions=",".join(gaps))
    return handlers

