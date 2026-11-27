"""The interactive owner bot: commands, callbacks, and the settings menus.

P6's interaction model, in one paragraph: the reply keyboard is the menu, every
actionable item carries an inline keyboard, a press is acked
(``answerCallbackQuery``), acted on inline, and the *source message is edited*
to the outcome — so a message's keyboard is the entire state machine and the
process holds no conversation state. Telegram already delivers full updates to
the ingress webhook; this module is the part that looks past ``message.text``.

The sender gate is one comparison, not machinery (audit R1): the integration
row's ``external_ref`` is the owner's chat id, recorded at connect time, and a
command or callback from any other chat keeps today's 200-ignore. The ingress
key names the tenant; the chat id names the person.

Settings (owner-added requirement, 2026-09-13): every ongoing-configurable
block the panel exposes is also adjustable from chat, through nested inline
menus — one screen per block, current state plus buttons (level pickers,
steppers, toggles), and writes that go through ``growth_profile.put`` as
partial blocks, the panel's own path, so a Telegram change can never clobber a
sibling field edited from the panel a second earlier. One-time registration
(integration secrets, the wizard) and the destructive compliance affordances
stay panel-only by design.

Free text exists only where a value is genuinely free (a currency code, a
timezone, a one-off language): one ``force_reply`` question, one reply, and the
reply is matched by the question it answers — the replied-to message is the
state, same rule as the keyboards.
"""

from __future__ import annotations

import html
import secrets
from typing import Any
from uuid import UUID

from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.services.agents.orchestrator import chunk_reply

logger = get_logger(__name__)

AGENT_KEY = "telegram_bot"

# ------------------------------------------------------------------- surfaces

# The reply keyboard `/menu` (and `/start`) answers with. Each label maps back
# to its command in `MENU_KEY_COMMANDS` — adding a key here and not there is a
# dead button, so the two are edited together.
MENU_KEYBOARD: dict[str, Any] = {
    "keyboard": [
        [{"text": "Report"}, {"text": "Insights"}],
        [{"text": "Approvals"}, {"text": "Profile"}],
        [{"text": "Settings"}, {"text": "Help"}],
    ],
    "resize_keyboard": True,
}

HELP_TEXT = (
    "I run your growth swarm.\n\n"
    "Report — build today's report now\n"
    "Insights — what the watchers found, act on each\n"
    "Approvals — batches waiting on you\n"
    "Profile — who the agents think you are\n"
    "Settings — offers, approval levels, send caps, follow-ups, languages,\n"
    "groundedness, reports, channel order\n\n"
    "Or just ask me about leads, reports, or what the agents are doing.\n"
    "Everything is also in the panel; this chat is the fast path."
)

# Free-text questions. The reply handler matches a reply against these exact
# prefixes — the question message is the state, and anything else typed at the
# bot stays 200-ignored.
CURRENCY_QUESTION = "What currency should offers use? Reply with the 3-letter code (e.g. USD)."
TIMEZONE_QUESTION = "Which timezone? Reply with a zone name (e.g. Asia/Kolkata)."
LANGUAGE_QUESTION = (
    "Which languages should replies be allowed in? Reply with comma-separated\n"
    "codes (e.g. hi, hinglish). English is always included."
)


def sender_allowed(payload: dict[str, Any], owner_chat_id: str) -> bool:
    """Audit R1's one-line gate: the update came from the owner's chat.

    `external_ref` on the telegram integration row is the chat id recorded at
    connect time. Compared as strings because Telegram sends integers and the
    row stores text; a mismatch keeps today's 200-ignore rather than answering,
    so a stranger cannot even learn the bot is listening.
    """
    if not owner_chat_id:
        return False
    message = payload.get("message") or payload.get("edited_message") or {}
    callback = payload.get("callback_query") or {}
    if isinstance(callback, dict) and callback:
        # a callback's chat is on the message it was pressed on
        source = callback.get("message") or {}
        chat = source.get("chat") if isinstance(source, dict) else None
    elif isinstance(message, dict) and message:
        chat = message.get("chat")
    else:
        return False
    if not isinstance(chat, dict):
        return False
    return str(chat.get("id") or "") == str(owner_chat_id).strip()


def _btn(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


# ------------------------------------------------------------ settings tables
#
# The alias tables are the contract the plan fixes: callback_data has a 64-byte
# budget, so codes are `set:<block>:<field>:<value>` over short aliases and
# never carry text. `decode_set` is the only reader; every renderer is the only
# writer, and the tests round-trip the pair.

SETTINGS_MENU: tuple[tuple[str, str], ...] = (
    # (block alias, block name in growth_profile)
    ("of", "offer_range"),
    ("hitl", "hitl_levels"),
    ("cap", "send_caps"),
    ("fu", "follow_up"),
    ("lang", "languages"),
    ("gnd", "grounding"),
    ("rep", "report_prefs"),
    ("ord", "channel_order"),
)
BLOCK_ALIASES = dict(SETTINGS_MENU)
BLOCK_LABELS = {
    "offer_range": "Offers",
    "hitl_levels": "Approval levels",
    "send_caps": "Send caps",
    "follow_up": "Follow-ups",
    "languages": "Languages",
    "grounding": "Groundedness",
    "report_prefs": "Reports",
    "channel_order": "Channel order",
}

LEVEL_FIELDS = {"cold": "outbound_batch", "camp": "campaign", "rev": "review_ask"}
LEVEL_LABELS = {"cold": "Cold outreach", "camp": "Campaigns", "rev": "Review asks"}
LEVEL_CHOICES = ((0, "Auto"), (1, "Batch"), (2, "Every msg"))

# (block alias, field alias) -> the numeric field, its bounds and its step.
STEPPERS: dict[tuple[str, str], dict[str, Any]] = {
    ("of", "dmin"): {
        "field": "discount_min_pct", "label": "Min discount",
        "step": 5, "lo": 0, "hi": 100, "unit": "%",
    },
    ("of", "dmax"): {
        "field": "discount_max_pct", "label": "Max discount",
        "step": 5, "lo": 0, "hi": 100, "unit": "%",
    },
    ("cap", "daily"): {
        "field": "daily_cap", "label": "Daily cap",
        "step": 10, "lo": 0, "hi": 5000, "unit": "/day",
    },
    ("cap", "warm"): {
        "field": "warmup_day", "label": "Warmup day",
        "step": 1, "lo": 0, "hi": 365, "unit": "",
    },
    ("cap", "from"): {
        "field": "start_hour", "label": "Send from",
        "step": 1, "lo": 0, "hi": 23, "unit": ":00",
    },
    ("cap", "until"): {
        "field": "end_hour", "label": "Send until",
        "step": 1, "lo": 1, "hi": 24, "unit": ":00",
    },
    ("fu", "max"): {
        "field": "max_follow_ups", "label": "Max follow-ups",
        "step": 1, "lo": 0, "hi": 5, "unit": "",
    },
}

WEEKDAYS = (("Mon", 0), ("Tue", 1), ("Wed", 2), ("Thu", 3), ("Fri", 4), ("Sat", 5), ("Sun", 6))
FU_OFFSETS = (1, 2, 3, 5, 7, 10, 14, 21, 30)
LANGUAGE_CHOICES = ("hi", "hinglish")
GROUNDING_AGENTS = (
    "growth_research",
    "growth_compose",
    "growth_replies",
    "growth_report",
    "growth_campaign",
    "growth_insights",
)
AGENT_SHORT = {
    "growth_research": "Research",
    "growth_compose": "Cold mail",
    "growth_replies": "Replies",
    "growth_report": "Reports",
    "growth_campaign": "Campaigns",
    "growth_insights": "Insights",
}
REPORT_PREFS = (
    ("weekly", "Weekly"), ("monthly", "Monthly"),
    ("daily", "Daily"), ("on_demand", "On demand"),
)
CURRENCIES = ("USD", "EUR", "INR", "GBP")


def _back_row(target: str = "nav:settings", label: str = "⬅ Settings") -> list[dict[str, str]]:
    return [_btn(label, target)]


def _stepper_row(
    block_alias: str, field_alias: str, label: str, value: int, spec: dict[str, Any]
) -> list[dict[str, str]]:
    """[-step] [label value] [+step] — the value shown is the current state."""
    step = int(spec["step"])
    return [
        _btn(f"-{step}", f"set:{block_alias}:{field_alias}:-{step}"),
        _btn(f"{label} {value}{spec['unit']}", "noop"),
        _btn(f"+{step}", f"set:{block_alias}:{field_alias}:+{step}"),
    ]


def render_settings_home(settings: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    """The Settings root: one button per block, each labelled with its state."""
    offer = settings.get("offer_range") or {}
    hitl = settings.get("hitl_levels") or {}
    caps = settings.get("send_caps") or {}
    lines = [
        "<b>Settings</b>",
        "",
        f"Offers — {_esc(offer.get('currency'))}, {offer.get('discount_min_pct', 0)}"
        f"-{offer.get('discount_max_pct', 0)}% off",
        "Approvals — {}/{} {}".format(
            hitl.get("outbound_batch", 1),
            hitl.get("campaign", 1),
            LEVEL_CHOICES[int(hitl.get("review_ask", 1))][1],
        ),
        f"Send caps — {caps.get('daily_cap', 50)}/day, {_esc(caps.get('timezone'))}",
    ]
    rows = [
        [_btn("Offers", "nav:set:of"), _btn("Approvals", "nav:set:hitl")],
        [_btn("Send caps", "nav:set:cap"), _btn("Follow-ups", "nav:set:fu")],
        [_btn("Languages", "nav:set:lang"), _btn("Grounding", "nav:set:gnd")],
        [_btn("Reports", "nav:set:rep"), _btn("Channels", "nav:set:ord")],
        _back_row("nav:back", "⬅ Menu"),
    ]
    return "\n".join(lines), rows


def _render_offers(settings: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    offer = settings.get("offer_range") or {}
    currency = str(offer.get("currency") or "")
    rows = [
        _stepper_row(
            "of", "dmin", "Min",
            int(offer.get("discount_min_pct") or 0), STEPPERS[("of", "dmin")],
        ),
        _stepper_row(
            "of", "dmax", "Max",
            int(offer.get("discount_max_pct") or 0), STEPPERS[("of", "dmax")],
        ),
    ]
    rows.append(
        [
            _btn((f"{c} ✓" if c == currency else c), f"set:of:cur:{c}")
            for c in CURRENCIES
        ]
        + [_btn("Other…", "set:of:cur:other")]
    )
    rows.append(_back_row())
    text = (
        "<b>Offers</b> — the band an agent may propose inside\n\n"
        f"Currency: <b>{_esc(currency)}</b>\n"
        f"Discount: <b>{offer.get('discount_min_pct', 0)}-{offer.get('discount_max_pct', 0)}%</b>"
    )
    return text, rows


def _render_hitl(settings: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    hitl = settings.get("hitl_levels") or {}
    rows: list[list[dict[str, str]]] = []
    for alias, field in LEVEL_FIELDS.items():
        current = int(hitl.get(field) or 0)
        rows.append(
            [_btn((label if value != current else f"{label} ✓"), f"set:hitl:{alias}:{value}")
             for value, label in LEVEL_CHOICES]
        )
    rows.append(_back_row())
    lines = ["<b>Approval levels</b> — when the agents must ask you", ""]
    for alias, field in LEVEL_FIELDS.items():
        current = int(hitl.get(field) or 0)
        label = next((name for value, name in LEVEL_CHOICES if value == current), "?")
        lines.append(f"{LEVEL_LABELS[alias]}: <b>{label}</b>")
    return "\n".join(lines), rows


def _render_caps(settings: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    caps = settings.get("send_caps") or {}
    days = [int(d) for d in caps.get("days") or []]
    rows = [
        _stepper_row(
            "cap", "daily", "Cap",
            int(caps.get("daily_cap") or 0), STEPPERS[("cap", "daily")],
        ),
        _stepper_row(
            "cap", "warm", "Warmup",
            int(caps.get("warmup_day") or 0), STEPPERS[("cap", "warm")],
        ),
        _stepper_row(
            "cap", "from", "From",
            int(caps.get("start_hour") or 0), STEPPERS[("cap", "from")],
        ),
        _stepper_row(
            "cap", "until", "Until",
            int(caps.get("end_hour") or 0), STEPPERS[("cap", "until")],
        ),
        [_btn((name if index not in days else f"{name} ✓"), f"set:cap:days:{index}")
         for name, index in WEEKDAYS],
        [_btn(f"TZ {_esc(caps.get('timezone'))}", "set:cap:tz:set")],
        _back_row(),
    ]
    active = ", ".join(name for name, index in WEEKDAYS if index in days) or "none"
    text = (
        "<b>Send caps</b> — volume and timing\n\n"
        f"{caps.get('daily_cap', 50)} messages/day (warmup day {caps.get('warmup_day', 0)})\n"
        f"Window {caps.get('start_hour', 9):02d}:00-{caps.get('end_hour', 18):02d}:00, "
        f"{_esc(caps.get('timezone'))}\n"
        f"Working days: <b>{_esc(active)}</b>"
    )
    return text, rows


def _render_follow_up(settings: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    follow = settings.get("follow_up") or {}
    days = [int(d) for d in follow.get("days") or []]
    rows = [
        [_btn((f"+{d}" if d not in days else f"+{d} ✓"), f"set:fu:days:{d}") for d in FU_OFFSETS],
        _stepper_row(
            "fu", "max", "Max",
            int(follow.get("max_follow_ups") or 0), STEPPERS[("fu", "max")],
        ),
        _back_row(),
    ]
    offsets = ", ".join(f"+{d}d" for d in days) or "none"
    text = (
        "<b>Follow-ups</b>\n\n"
        f"Days after the first message: <b>{_esc(offsets)}</b>\n"
        f"Maximum follow-ups: <b>{follow.get('max_follow_ups', 3)}</b> (0 turns them off)"
    )
    return text, rows


def _render_languages(settings: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    languages = settings.get("languages") or {}
    allowed = {str(x) for x in languages.get("allowed") or []}
    rows = [
        [_btn("en ✓", "noop")]
        + [
            _btn((c if c not in allowed else f"{c} ✓"), f"set:lang:allow:{c}")
            for c in LANGUAGE_CHOICES
        ]
        + [_btn("Add…", "set:lang:allow:add")],
        _back_row(),
    ]
    text = (
        "<b>Languages</b> — what a reply may be written in\n\n"
        f"Allowed: <b>{_esc(', '.join(sorted(allowed)) or 'en')}</b>\n"
        "English is always allowed and cannot be removed."
    )
    return text, rows


def _render_grounding(settings: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    grounding = settings.get("grounding") or {}
    mode = str(grounding.get("mode") or "advisory")
    agents = dict(grounding.get("agents") or {})
    rows = [
        [
            _btn(("Advisory" if mode != "advisory" else "Advisory ✓"), "set:gnd:claims:advisory"),
            _btn(("Block" if mode != "block" else "Block ✓"), "set:gnd:claims:block"),
        ]
    ]
    for agent in GROUNDING_AGENTS:
        on = bool(agents.get(agent, True))
        rows.append([_btn((AGENT_SHORT[agent] if not on else f"{AGENT_SHORT[agent]} ✓"),
                          f"set:gnd:ag:{agent}")])
    rows.append(_back_row())
    text = (
        "<b>Groundedness</b> — what happens to an ungrounded claim\n\n"
        f"Mode: <b>{_esc(mode)}</b>\n"
        "Checked agents are grounded; unchecking one skips its check."
    )
    return text, rows


def _render_report_prefs(settings: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    prefs = settings.get("report_prefs") or {}
    rows = [
        [_btn((label if not prefs.get(key, key != "daily") else f"{label} ✓"), f"set:rep:{key}")]
        for key, label in REPORT_PREFS
    ]
    rows.append(_back_row())
    text = "<b>Reports</b> — which ones get built\n\nTap to turn a report on or off."
    return text, rows


def _render_channel_order(settings: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    order = [str(x) for x in (settings.get("channel_order") or {}).get("order") or []]
    rows = [
        [_btn(f"↑ {channel}", f"set:ord:up:{index}")]
        if index
        # the first channel is already first: moving it up is a no-op, so it
        # renders as a state label (the bot's ✓ convention) rather than a
        # callback that would ack and change nothing.
        else [_btn(f"{channel} ✓", "noop")]
        for index, channel in enumerate(order)
    ]
    rows.append(_back_row())
    text = (
        "<b>Channel order</b> — which channel is tried first\n\n"
        + " → ".join(_esc(c) for c in order)
    )
    return text, rows


BLOCK_RENDERERS = {
    "of": _render_offers,
    "hitl": _render_hitl,
    "cap": _render_caps,
    "fu": _render_follow_up,
    "lang": _render_languages,
    "gnd": _render_grounding,
    "rep": _render_report_prefs,
    "ord": _render_channel_order,
}


def render_block(alias: str, settings: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    """One block's screen. Unknown aliases render the home screen — a stale
    keyboard after a redeploy must not dead-end the owner."""
    renderer = BLOCK_RENDERERS.get(alias)
    if renderer is None:
        return render_settings_home(settings)
    return renderer(settings)


def decode_set(code: str) -> tuple[str, Any]:
    """`set:<block>:<field>[:<value>]` → ("write", (block, partial)) or
    ("ask", question) for the force-reply specials, or ("bad", reason).

    The only decoder: renderers emit with the same aliases, and the tests
    round-trip the pair, so a code that decodes to nothing is a bug rather
    than a mystery.
    """
    parts = code.split(":")
    if len(parts) < 3 or parts[0] != "set":
        return "bad", "malformed"
    _, block_alias, field_alias = parts[0], parts[1], parts[2]
    value = parts[3] if len(parts) > 3 else ""
    block = BLOCK_ALIASES.get(block_alias)
    if block is None:
        return "bad", f"unknown block {block_alias}"

    if block_alias == "of":
        if field_alias == "cur":
            if value == "other":
                return "ask", CURRENCY_QUESTION
            if value.isalpha() and len(value) <= 8:
                return "write", (block, {"currency": value.upper()})
            return "bad", "bad currency"
        spec = STEPPERS.get(("of", field_alias))
        if spec is None:
            return "bad", f"unknown field {field_alias}"
        return "write", (block, {spec["field"]: _delta_value(value, spec)})

    if block_alias == "hitl":
        field = LEVEL_FIELDS.get(field_alias)
        if field is None:
            return "bad", f"unknown field {field_alias}"
        if value not in ("0", "1", "2"):
            return "bad", "bad level"
        return "write", (block, {field: int(value)})

    if block_alias == "cap":
        if field_alias == "days":
            if not value.isdigit() or not 0 <= int(value) <= 6:
                return "bad", "bad day"
            return "toggle_set", (block, "days", int(value))
        if field_alias == "tz":
            return "ask", TIMEZONE_QUESTION
        spec = STEPPERS.get(("cap", field_alias))
        if spec is None:
            return "bad", f"unknown field {field_alias}"
        return "write", (block, {spec["field"]: _delta_value(value, spec)})

    if block_alias == "fu":
        if field_alias == "days":
            if not value.isdigit():
                return "bad", "bad offset"
            return "toggle_set", (block, "days", int(value))
        spec = STEPPERS.get(("fu", field_alias))
        if spec is None:
            return "bad", f"unknown field {field_alias}"
        return "write", (block, {spec["field"]: _delta_value(value, spec)})

    if block_alias == "lang":
        if field_alias != "allow":
            return "bad", f"unknown field {field_alias}"
        if value == "add":
            return "ask", LANGUAGE_QUESTION
        if value and value.replace("-", "").replace("_", "").isalnum():
            return "toggle_set", (block, "allowed", value.lower())
        return "bad", "bad language"

    if block_alias == "gnd":
        if field_alias == "claims":
            if value not in ("advisory", "block"):
                return "bad", "bad mode"
            return "write", (block, {"mode": value})
        if field_alias == "ag":
            if value not in GROUNDING_AGENTS:
                return "bad", "unknown agent"
            return "toggle_agent", (block, value)
        return "bad", f"unknown field {field_alias}"

    if block_alias == "rep":
        if field_alias not in {key for key, _ in REPORT_PREFS}:
            return "bad", f"unknown field {field_alias}"
        return "toggle_bool", (block, field_alias)

    if block_alias == "ord":
        if field_alias != "up" or not value.isdigit():
            return "bad", "bad reorder"
        return "order_up", (block, int(value))

    return "bad", "unhandled"


def _delta_value(value: str, spec: dict[str, Any]) -> Any:
    """A stepper's payload is a signed delta, resolved against current state
    by the caller (it owns the settings); here it is only parsed and bound."""
    delta = int(value) if value and value[0] in "+-" and value[1:].isdigit() else None
    if delta is None:
        raise ValueError(f"bad stepper value {value!r}")
    return ("__delta__", delta, spec)


# ------------------------------------------------------------------- service


# ------------------------------------------------------------ provider mode
#
# One bot owned by the platform (TELEGRAM_BOT_TOKEN in env) that every tenant
# connects to by messaging it — no token pasted, no chat id typed. Everything
# above is the per-tenant mode and is reached only through a tenant's ingress
# key; everything below is reached only through the platform key, and only
# when that setting is set. The two modes coexist: a tenant with their own
# token keeps their own webhook (Telegram delivers to the one registered
# webhook anyway), and their key opens the per-tenant door unchanged.

# The one reply an unbound chat can get. The same text for every unbound
# reason — no /start yet, an unknown code, a typo — so the reply is the same
# information for everyone and leaks nothing about which codes exist.
BINDING_PROMPT = (
    "This chat isn't connected to a workspace yet.\n\n"
    "Open Connections → Telegram in your dashboard and send:\n"
    "/start <your-code>"
)

# A binding code is short enough to type on a phone and unambiguous when read
# back: no 0/O, no 1/I/L. Eight characters over the remaining 31 symbols is
# about 40 bits, and a code only ever connects one chat, once, within a day.
BINDING_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
BINDING_CODE_LENGTH = 8
BINDING_TTL_SECONDS = 24 * 3600

# The platform bot's @username, fetched with getMe and cached: it is the only
# way to learn it, and it is not worth an outbound call per setup-screen
# render. A failure is cached briefly rather than forever — the screen degrades
# to plain instructions without the link, and a short retry stops an outage
# from turning every render into an outbound call.
USERNAME_CACHE_KEY = "telegram:platform:username"
USERNAME_TTL_SECONDS = 7 * 24 * 3600
USERNAME_RETRY_SECONDS = 300

# The chat→tenant resolution is cached for this long. The uncached walk costs
# one database session per active tenant on *every* update, which on a
# high-latency link (server far from the database) is seconds per message.
# The cost is staleness: a disconnect or rebind takes up to this long to be
# seen by the platform bot. `_bind` deletes the entries it knows about, so a
# rebind is picked up on the very next message; only a disconnect made from
# the dashboard (no chat id to delete) waits out the TTL.
CHAT_CACHE_PREFIX = "telegram:chat:"
CHAT_CACHE_TTL_SECONDS = 30


def _binding_tenant_key(tenant_id: UUID) -> str:
    return f"telegram:binding:{tenant_id}"


def _binding_code_key(code: str) -> str:
    return f"telegram:binding-code:{code.strip().upper()}"


async def current_binding_code(
    container: Any, tenant_id: UUID, *, refresh: bool = False
) -> str:
    """The tenant's one live binding code, minted on demand.

    Stored in the cache rather than the database: it is short-lived,
    single-purpose state, and the key is tenant-scoped. A refresh retires the
    old code before minting the new one, so a tenant never has two live codes
    and a code already shown to somebody is dead the moment its replacement is
    issued.
    """
    cache = container.cache
    existing = await cache.get(_binding_tenant_key(tenant_id))
    if existing and not refresh:
        return existing
    code = "".join(
        secrets.choice(BINDING_ALPHABET) for _ in range(BINDING_CODE_LENGTH)
    )
    if existing:
        await cache.delete(_binding_code_key(existing))
    await cache.set(_binding_tenant_key(tenant_id), code, BINDING_TTL_SECONDS)
    await cache.set(_binding_code_key(code), str(tenant_id), BINDING_TTL_SECONDS)
    return code


async def tenant_for_binding_code(container: Any, code: str) -> UUID | None:
    """The tenant a /start code names, or None. Case-insensitive on purpose:
    the code is typed by a human on a phone keyboard."""
    if not code or not code.strip():
        return None
    raw = await container.cache.get(_binding_code_key(code))
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


async def consume_binding_code(container: Any, tenant_id: UUID) -> None:
    """Retire the tenant's code after a successful bind — single use, so a
    code glimpsed by anyone else has already been spent."""
    cache = container.cache
    code = await cache.get(_binding_tenant_key(tenant_id))
    if not code:
        return
    await cache.delete(_binding_code_key(code))
    await cache.delete(_binding_tenant_key(tenant_id))


async def platform_bot_username(container: Any) -> str | None:
    """The platform bot's @username (without the @), or None when unknown."""
    cached = await container.cache.get(USERNAME_CACHE_KEY)
    if cached is not None:
        # an empty string is a cached failure, not a miss
        return cached or None
    username = await _fetch_bot_username(container)
    ttl = USERNAME_TTL_SECONDS if username else USERNAME_RETRY_SECONDS
    await container.cache.set(USERNAME_CACHE_KEY, username or "", ttl)
    return username


async def _fetch_bot_username(container: Any) -> str | None:
    """One getMe against the platform token. Never raises and never puts the
    token in a log line — an exception's own string is not trusted to carry
    one, so only the exception's type is recorded."""
    import httpx

    from app.adapters.channels.telegram import (
        TELEGRAM_API_BASE,
        assert_telegram_bot_token,
    )
    from app.core.urlguard import assert_peer_public

    token = str(container.settings.TELEGRAM_BOT_TOKEN or "")
    try:
        assert_telegram_bot_token(token)
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(f"{TELEGRAM_API_BASE}/bot{token}/getMe")
            assert_peer_public(response)
        if response.status_code >= 400:
            return None
        username = str((response.json().get("result") or {}).get("username") or "")
        return username.lstrip("@") or None
    except Exception as exc:
        logger.warning("telegram_platform_getme_failed", error=type(exc).__name__)
        return None


def _update_chat_id(update: dict[str, Any]) -> str:
    """The chat an update belongs to, as text — the form external_ref stores.

    Callbacks are read off the message they were pressed on, the same rule as
    the sender gate.
    """
    message = update.get("message") or update.get("edited_message") or {}
    callback = update.get("callback_query") or {}
    source: Any = {}
    if isinstance(callback, dict) and callback:
        source = callback.get("message") or {}
    elif isinstance(message, dict) and message:
        source = message
    chat = source.get("chat") if isinstance(source, dict) else None
    if not isinstance(chat, dict):
        return ""
    return str(chat.get("id") or "")


def _start_code(update: dict[str, Any]) -> str:
    """The argument of a /start, when there is one.

    A bare /start returns "" and is not special: it flows through the normal
    routing like any other message, so a bound chat still gets its menu and an
    unbound chat still gets the prompt.
    """
    message = update.get("message") or {}
    if not isinstance(message, dict):
        return ""
    parts = str(message.get("text") or "").strip().split()
    if not parts or parts[0].split("@")[0].lower() != "/start" or len(parts) < 2:
        return ""
    return parts[1].strip()



class TelegramBotService:
    """Dispatches Telegram updates for one tenant, in the ingress request.

    The notifier is built per update and never cached — the token is a tenant
    secret, the same rule the approvals and reports services state — and the
    whole handler runs inline behind the ingress's existing rate limits, the
    same precedent as the on-demand report.
    """

    def __init__(self, container: Any) -> None:
        self._container = container

    async def handle(
        self, tenant_id: UUID, integration: dict[str, Any], update: dict[str, Any]
    ) -> dict[str, Any]:
        """One update, already key-verified and rate-limited by the ingress."""
        from app.services.growth.reports.service import telegram_command

        chat_id = str(integration.get("external_ref") or "")
        if not sender_allowed(update, chat_id):
            return {"status": "ignored"}

        if not self._container.settings.TELEGRAM_ENABLED:
            return {"status": "ignored"}

        callback = update.get("callback_query")
        if isinstance(callback, dict) and callback:
            return await self._callback(tenant_id, integration, callback)

        command = telegram_command(update)
        if command is not None:
            return await self._command(tenant_id, integration, command)

        message = update.get("message") or {}
        if isinstance(message, dict):
            if message.get("reply_to_message"):
                return await self._free_text(tenant_id, integration, message)
            if str(message.get("text") or "").strip():
                return await self._orchestrate(tenant_id, integration, message)

        return {"status": "ignored"}

    # --------------------------------------------------------- provider mode

    async def handle_platform(self, update: dict[str, Any]) -> dict[str, Any]:
        """One update for the platform bot: bind a chat, or route it.

        The platform ingress key has already been verified and rate-limited by
        the ingress; here the *chat* is what resolves the tenant, by the row
        its /start wrote. Everything after resolution is the per-tenant
        handler above unchanged — sender gate, commands, callbacks — because
        the binding writes exactly the row per-tenant mode creates.
        """
        if not self._container.settings.TELEGRAM_ENABLED:
            return {"status": "ignored"}

        chat_id = _update_chat_id(update)
        if not chat_id:
            return {"status": "ignored"}

        code = _start_code(update)
        if code:
            return await self._bind(chat_id, code)

        resolved = await self._resolve_chat(chat_id)
        if resolved is None:
            return await self._prompt(chat_id)
        tenant_id, integration = resolved
        return await self.handle(tenant_id, integration, update)

    async def _resolve_chat(
        self, chat_id: str
    ) -> tuple[UUID, dict[str, Any]] | None:
        """The tenant whose active telegram row names this chat, if any.

        The per-tenant ingress never needs this — its key names the tenant —
        but the platform bot has one key for everybody, so the chat is the only
        routing fact in the update. ``growth_integrations`` carries FORCE RLS,
        so there is no single cross-tenant query: walk the active tenants (from
        ``tenants``, the one table without RLS — the scheduler's pattern) and
        ask each inside its own scope.

        The result is cached per chat for ``CHAT_CACHE_TTL_SECONDS`` — the walk
        is a database session per tenant per update, which costs seconds on a
        high-latency link. First match wins, deterministically by tenant id.
        """
        import json as _json

        from app.adapters.postgres.growth_repo import active_tenant_ids

        cache = self._container.cache
        cached = await cache.get(CHAT_CACHE_PREFIX + chat_id)
        if cached:
            try:
                entry = _json.loads(cached)
                return UUID(entry["tenant_id"]), entry["integration"]
            except (ValueError, KeyError, TypeError):
                # a malformed entry is a miss, not an error
                await cache.delete(CHAT_CACHE_PREFIX + chat_id)

        after: UUID | None = None
        while True:
            tenant_ids = await active_tenant_ids(limit=200, after=after)
            if not tenant_ids:
                return None
            for tenant_id in tenant_ids:
                row = await self._container.growth.integrations.get(
                    tenant_id, "telegram", chat_id
                )
                if row is not None and row.get("status") == "active":
                    await cache.set(
                        CHAT_CACHE_PREFIX + chat_id,
                        _json.dumps(
                            {"tenant_id": str(tenant_id), "integration": row},
                            default=str,
                        ),
                        CHAT_CACHE_TTL_SECONDS,
                    )
                    return tenant_id, row
            if len(tenant_ids) < 200:
                return None
            after = tenant_ids[-1]

    async def _bind(self, chat_id: str, code: str) -> dict[str, Any]:
        """/start <code>: bind this chat to the code's tenant, then the menu."""
        tenant_id = await tenant_for_binding_code(self._container, code)
        if tenant_id is None:
            # an unknown or expired code gets the same reply as no code at all
            return await self._prompt(chat_id)

        # the row per-tenant mode creates: provider telegram, the chat id as
        # external_ref, active. Every downstream reader — the sender gate, the
        # approvals and report notifiers, the setup screen — works unchanged.
        await self._container.growth.integrations.upsert(
            tenant_id, "telegram", status="active", external_ref=chat_id
        )
        # one bound chat per tenant: retiring any earlier binding is what keeps
        # the resolution single-answer after a tenant switches chats
        retired: list[str] = []
        for other in await self._container.growth.integrations.list(tenant_id):
            ref = str(other.get("external_ref") or "")
            if (
                str(other.get("provider")) == "telegram"
                and ref not in ("", chat_id)
                and other.get("status") == "active"
            ):
                await self._container.growth.integrations.upsert(
                    tenant_id, "telegram", status="not_connected", external_ref=ref
                )
                retired.append(ref)
        # the resolution cache must not outlive the rows it caches: this chat
        # resolves fresh (its row just changed), and so does every retired one
        cache = self._container.cache
        await cache.delete(CHAT_CACHE_PREFIX + chat_id)
        for ref in retired:
            await cache.delete(CHAT_CACHE_PREFIX + ref)
        await consume_binding_code(self._container, tenant_id)
        await self._container.growth.audit.record(
            tenant_id,
            agent="telegram",
            action="integration_updated",
            input={"provider": "telegram", "status": "active"},
            reason=f"chat {chat_id} bound via /start code",
        )

        notifier = self._platform_notifier(chat_id)
        if notifier is None:
            return {"status": "bound", "reason": "telegram not deliverable"}
        receipt = await notifier.send_message("What should I do?", MENU_KEYBOARD)
        return {"status": "bound" if receipt.delivered else "bind send failed"}

    async def _prompt(self, chat_id: str) -> dict[str, Any]:
        """The one reply an unbound chat can get. See ``BINDING_PROMPT``."""
        notifier = self._platform_notifier(chat_id)
        if notifier is None:
            return {"status": "ignored", "reason": "telegram not deliverable"}
        receipt = await notifier.send_message(BINDING_PROMPT)
        return {"status": "prompted" if receipt.delivered else "send failed"}

    def _platform_notifier(self, chat_id: str) -> Any | None:
        """A notifier on the platform bot's token, addressed to one chat.

        Same construction rule as ``_notifier`` — built per use, never cached —
        minus the tenant secret: the platform token is a deployment setting,
        not a per-tenant credential, so there is nothing to resolve.
        """
        from app.adapters.channels.telegram import TelegramApprovalNotifier

        token = str(self._container.settings.TELEGRAM_BOT_TOKEN or "")
        if not token:
            return None
        try:
            return TelegramApprovalNotifier(
                token,
                chat_id,
                lambda _request: {},
                timeout=self._container.settings.ESCALATION_NOTIFY_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning("telegram_platform_notifier_rejected")
            return None

    # -------------------------------------------------------------- commands

    async def _command(
        self, tenant_id: UUID, integration: dict[str, Any], command: str
    ) -> dict[str, Any]:
        notifier = await self._notifier(tenant_id, integration)
        if notifier is None:
            return {"status": "ignored", "reason": "telegram not deliverable"}

        if command == "report":
            return await self._report(tenant_id)
        if command == "menu":
            receipt = await notifier.send_message("What should I do?", MENU_KEYBOARD)
            return {"status": "sent" if receipt.delivered else "send failed"}
        if command == "insights":
            return await self._insights_list(tenant_id, notifier)
        if command == "approvals":
            return await self._approvals_list(tenant_id, notifier)
        if command == "profile":
            return await self._profile(tenant_id, notifier)
        if command == "settings":
            settings = await self._container.growth_profile.get(tenant_id)
            text, rows = render_settings_home(settings)
            receipt = await notifier.send_message(text, {"inline_keyboard": rows})
            return {"status": "sent" if receipt.delivered else "send failed"}
        if command == "help":
            receipt = await notifier.send_message(HELP_TEXT)
            return {"status": "sent" if receipt.delivered else "send failed"}
        return {"status": "ignored"}

    async def _report(self, tenant_id: UUID) -> dict[str, Any]:
        """The same deduped on-demand path the panel's Run now uses."""
        try:
            return await self._container.growth_reports.request_on_demand(
                tenant_id, via="telegram"
            )
        except ValidationError:
            return {"status": "ignored", "reason": "on-demand reports are off"}

    async def _insights_list(self, tenant_id: UUID, notifier: Any) -> dict[str, Any]:
        listed = await self._container.growth_insights.list(
            tenant_id, limit=5, open_only=True
        )
        items = listed.get("items") or []
        if not items:
            receipt = await notifier.send_message("No open insights. The watchers are quiet.")
            return {"status": "sent" if receipt.delivered else "send failed"}
        lines = ["<b>Open insights</b>", ""]
        rows: list[list[dict[str, str]]] = []
        for item in items:
            lines.append(f"• {_esc(item.get('summary'))}")
            rows.append(
                [
                    _btn("Act ▶", f"ins:{item['id']}:act"),
                    _btn("Snooze 💤", f"ins:{item['id']}:snooze"),
                    _btn("Skip ⏭", f"ins:{item['id']}:skip"),
                ]
            )
        receipt = await notifier.send_message(
            "\n".join(lines), {"inline_keyboard": rows}
        )
        return {"status": "sent" if receipt.delivered else "send failed"}

    async def _approvals_list(self, tenant_id: UUID, notifier: Any) -> dict[str, Any]:
        from app.services.approvals.render import feature_label

        rows = await self._container.approvals.pending_for(tenant_id, limit=5)
        if not rows:
            receipt = await notifier.send_message("Nothing is waiting on you.")
            return {"status": "sent" if receipt.delivered else "send failed"}
        lines = ["<b>Waiting on you</b>", ""]
        keyboard: list[list[dict[str, str]]] = []
        for row in rows:
            summary = str((row.get("payload") or {}).get("summary") or "")
            lines.append(
                f"• <b>{_esc(feature_label(str(row.get('feature'))))}</b> — "
                f"{_esc(' '.join(summary.split())[:140])}"
            )
            keyboard.append(
                [
                    _btn("✅ Approve", f"ap:{row['id']}:y"),
                    _btn("✖️ Reject", f"ap:{row['id']}:n"),
                ]
            )
        receipt = await notifier.send_message(
            "\n".join(lines), {"inline_keyboard": keyboard}
        )
        return {"status": "sent" if receipt.delivered else "send failed"}

    async def _profile(self, tenant_id: UUID, notifier: Any) -> dict[str, Any]:
        """The filled profile summary. Re-discovery is P4; until it exists the
        honest screen is what the agents currently read."""
        settings = await self._container.growth_profile.get(tenant_id)
        profile = settings.get("profile") or {}
        pairs = [
            ("Sells", profile.get("sells")),
            ("Audience", profile.get("audience")),
            ("Website", profile.get("website")),
            ("Price range", profile.get("price_range")),
            ("Sender", profile.get("sender_name")),
        ]
        lines = ["<b>Profile</b> — what the agents read", ""]
        lines.extend(
            f"{label}: {_esc(value) if value else '<i>not set</i>'}"
            for label, value in pairs
        )
        receipt = await notifier.send_message("\n".join(lines))
        return {"status": "sent" if receipt.delivered else "send failed"}

    # ------------------------------------------------------------- callbacks

    async def _callback(
        self, tenant_id: UUID, integration: dict[str, Any], callback: dict[str, Any]
    ) -> dict[str, Any]:
        """Ack first (Telegram's window is seconds), act, then edit the source
        message to the outcome — the message becomes its own receipt."""
        data = str(callback.get("data") or "")
        query_id = str(callback.get("id") or "")
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        message_id = message.get("message_id")
        if not data or not chat_id or message_id is None:
            return {"status": "ignored"}

        notifier = await self._notifier(tenant_id, integration)
        if notifier is None:
            return {"status": "ignored", "reason": "telegram not deliverable"}
        if query_id:
            await notifier.answer_callback(query_id)

        prefix, _, rest = data.partition(":")
        try:
            if prefix == "ap":
                return await self._approval_callback(
                    tenant_id, notifier, chat_id, message_id, rest
                )
            if prefix == "ins":
                return await self._insight_callback(tenant_id, notifier, chat_id, message_id, rest)
            if prefix == "nav":
                return await self._nav_callback(tenant_id, notifier, chat_id, message_id, rest)
            if prefix == "set":
                return await self._set_callback(
                    tenant_id, notifier, chat_id, message_id, rest
                )
        except Exception:
            logger.error("telegram_bot_callback_failed", exc_info=True, prefix=prefix)
            await self._edit(notifier, chat_id, message_id, "⚠️ Something broke. Try again.")
            return {"status": "error"}
        return {"status": "ignored"}

    async def _approval_callback(
        self, tenant_id: UUID, notifier: Any, chat_id: str, message_id: Any, rest: str
    ) -> dict[str, Any]:
        """`ap:<id>:y|n` → the panel's own decide path, in-process."""
        approval_id, _, flag = rest.partition(":")
        if flag not in ("y", "n"):
            return {"status": "ignored"}
        decision = "approved" if flag == "y" else "rejected"
        try:
            row = await self._container.approvals.decide(
                tenant_id,
                UUID(approval_id),
                decision=decision,
                decided_by=f"telegram:{chat_id}",
            )
        except ValidationError as exc:
            # already decided from the panel, or expired: the row does not
            # change, and the message should stop offering the choice
            await self._edit(
                notifier, chat_id, message_id, f"Already decided. {_esc(str(exc))}"
            )
            return {"status": "already"}
        outcome = (
            "✅ Approved — batch released to sending"
            if decision == "approved"
            else "✖️ Rejected — batch dropped, nothing sent"
        )
        await self._edit(notifier, chat_id, message_id, outcome)
        logger.info(
            "telegram_approval_decided",
            tenant_id=str(tenant_id),
            approval_id=approval_id,
            decision=decision,
        )
        return {"status": decision, "feature": row.get("feature")}

    async def _insight_callback(
        self, tenant_id: UUID, notifier: Any, chat_id: str, message_id: Any, rest: str
    ) -> dict[str, Any]:
        """`ins:<id>:act|go|snooze|skip|list` — the staged act flow."""
        insight_id, _, action = rest.partition(":")
        if action not in ("act", "go", "snooze", "skip", "list"):
            return {"status": "ignored"}

        if action == "list":
            return await self._insights_list(tenant_id, notifier)

        if action == "act":
            # stage 2: the confirm keyboard on the edited message. The insight
            # text is re-read from the open list so the stage-2 screen and the
            # list can never disagree.
            listed = await self._container.growth_insights.list(
                tenant_id, limit=10, open_only=True
            )
            item = next(
                (i for i in listed.get("items") or [] if i.get("id") == insight_id), None
            )
            if item is None:
                await self._edit(notifier, chat_id, message_id, "Already handled.")
                return {"status": "already"}
            text = (
                f"<b>{_esc(item.get('summary'))}</b>\n\n"
                f"Why: {_esc(item.get('why'))}\n\n"
                f"Suggested: {_esc(item.get('suggested_action'))}\n\nAct on it?"
            )
            await self._edit(
                notifier,
                chat_id,
                message_id,
                text,
                {
                    "inline_keyboard": [
                        [
                            _btn("✅ Confirm campaign", f"ins:{insight_id}:go"),
                            _btn("💤 Snooze 3d", f"ins:{insight_id}:snooze"),
                        ],
                        [_btn("⏭ Skip", f"ins:{insight_id}:skip"),
                         _btn("⬅ Back to list", f"ins:{insight_id}:list")],
                    ]
                },
            )
            return {"status": "staged"}

        act_action = {"go": "campaign", "snooze": "snooze", "skip": "ignore"}[action]
        updated = await self._container.growth_insights.act(
            tenant_id, UUID(insight_id), act_action
        )
        if updated is None:
            await self._edit(notifier, chat_id, message_id, "Already handled.")
            return {"status": "already"}
        outcomes = {
            "campaign": "✅ Marked for campaign — draft it from Insights in the panel.",
            "snooze": "💤 Snoozed — the watchers will not re-surface it for a while.",
            "ignore": "⏭ Ignored — it leaves the open list.",
        }
        await self._edit(notifier, chat_id, message_id, outcomes[act_action])
        return {"status": act_action}

    async def _nav_callback(
        self, tenant_id: UUID, notifier: Any, chat_id: str, message_id: Any, rest: str
    ) -> dict[str, Any]:
        """`nav:settings` / `nav:set:<block>` / `nav:back` — settings
        navigation, always re-rendered from current state."""
        if rest == "back":
            receipt = await notifier.send_message("What should I do?", MENU_KEYBOARD)
            return {"status": "sent" if receipt.delivered else "send failed"}

        settings = await self._container.growth_profile.get(tenant_id)
        if rest == "settings":
            text, rows = render_settings_home(settings)
        elif rest.startswith("set:"):
            text, rows = render_block(rest.split(":")[1], settings)
        else:
            return {"status": "ignored"}
        await self._edit(notifier, chat_id, message_id, text, {"inline_keyboard": rows})
        return {"status": "rendered"}

    async def _set_callback(
        self,
        tenant_id: UUID,
        notifier: Any,
        chat_id: str,
        message_id: Any,
        rest: str,
    ) -> dict[str, Any]:
        """`set:<block>:<field>:<value>` — a settings write, or a force-reply
        question for the genuinely-free values."""
        kind, payload = decode_set(f"set:{rest}")
        if kind == "bad":
            return {"status": "ignored"}
        if kind == "ask":
            receipt = await notifier.send_message(
                payload, {"force_reply": True, "selective": False}
            )
            return {"status": "asked" if receipt.delivered else "send failed"}

        settings = await self._container.growth_profile.get(tenant_id)
        partial = self._resolve_partial(settings, kind, payload)
        if partial is None:
            return {"status": "ignored"}
        block, fields = partial
        alias = _alias_for(block)
        try:
            result = await self._container.growth_profile.put(
                tenant_id, {block: fields}, actor=f"telegram:{chat_id}"
            )
        except ValidationError as exc:
            # the write was refused (a validator, e.g. a 6th follow-up
            # offset): re-render the block with the reason, values unchanged
            text, rows = render_block(alias, settings)
            await self._edit(
                notifier, chat_id, message_id, f"⚠️ {_esc(str(exc))}\n\n{text}",
                {"inline_keyboard": rows},
            )
            return {"status": "rejected"}
        text, rows = render_block(alias, result)
        await self._edit(notifier, chat_id, message_id, text, {"inline_keyboard": rows})
        return {"status": "written", "block": block}

    def _resolve_partial(
        self, settings: dict[str, Any], kind: str, payload: Any
    ) -> tuple[str, dict[str, Any]] | None:
        """Turn a decoded callback into the partial block `growth_profile.put`
        takes, resolving steppers and toggles against current state."""
        if kind == "write":
            block, fields = payload
            resolved: dict[str, Any] = {}
            for field, value in fields.items():
                if isinstance(value, tuple) and value and value[0] == "__delta__":
                    _, delta, spec = value
                    resolved[field] = self._step(settings, block, field, delta, spec)
                else:
                    resolved[field] = value
            return block, resolved
        if kind == "toggle_set":
            block, field, member = payload
            current = list((settings.get(block) or {}).get(field) or [])
            if member in current:
                members = [m for m in current if m != member]
            else:
                members = [*current, member]
            return block, {field: sorted(members)}
        if kind == "toggle_bool":
            block, field = payload
            current = bool((settings.get(block) or {}).get(field))
            return block, {field: not current}
        if kind == "toggle_agent":
            block, agent = payload
            agents = dict((settings.get(block) or {}).get("agents") or {})
            agents[agent] = not bool(agents.get(agent, True))
            return block, {"agents": agents}
        if kind == "order_up":
            block, index = payload
            order = [str(x) for x in (settings.get(block) or {}).get("order") or []]
            if index <= 0 or index >= len(order):
                return None
            order[index - 1], order[index] = order[index], order[index - 1]
            return block, {"order": order}
        return None

    def _step(
        self, settings: dict[str, Any], block: str, field: str, delta: int, spec: dict[str, Any]
    ) -> int:
        """current ± delta, clamped to the field's bounds. The offer band's
        min ≤ max rule is enforced here too, so a stepper press never produces
        a write the validator would bounce."""
        current = int((settings.get(block) or {}).get(field) or 0)
        value = max(int(spec["lo"]), min(int(spec["hi"]), current + delta))
        if field == "discount_min_pct":
            value = min(value, int((settings.get(block) or {}).get("discount_max_pct") or 0))
        if field == "discount_max_pct":
            value = max(value, int((settings.get(block) or {}).get("discount_min_pct") or 0))
        return value

    # ------------------------------------------------------------- free text

    async def _free_text(
        self, tenant_id: UUID, integration: dict[str, Any], message: dict[str, Any]
    ) -> dict[str, Any]:
        """A reply to one of the force-reply questions. The replied-to message
        is the state: each question has one exact text, and a reply that does
        not answer a question we asked is not ours to interpret."""
        replied = message.get("reply_to_message") or {}
        question = str(replied.get("text") or "")
        text = str(message.get("text") or "").strip()
        if not text:
            return {"status": "ignored"}

        if question == CURRENCY_QUESTION:
            code = text.upper()
            if not code.isalpha() or not 2 <= len(code) <= 8:
                return await self._reask(tenant_id, integration, CURRENCY_QUESTION)
            block, fields = "offer_range", {"currency": code}
        elif question == TIMEZONE_QUESTION:
            block, fields = "send_caps", {"timezone": text}
        elif question == LANGUAGE_QUESTION:
            codes = [c.strip().lower() for c in text.split(",") if c.strip()]
            if not codes:
                return await self._reask(tenant_id, integration, LANGUAGE_QUESTION)
            block, fields = "languages", {"allowed": codes}
        else:
            # a reply that does not answer a question we asked is a plain
            # question — the owner talking to the orchestrator
            return await self._orchestrate(tenant_id, integration, message)

        notifier = await self._notifier(tenant_id, integration)
        if notifier is None:
            return {"status": "ignored"}
        chat = message.get("chat") or {}
        try:
            result = await self._container.growth_profile.put(
                tenant_id, {block: fields}, actor=f"telegram:{chat.get('id', '')}"
            )
        except ValidationError as exc:
            receipt = await notifier.send_message(f"⚠️ {_esc(str(exc))}")
            return {"status": "rejected" if receipt.delivered else "send failed"}
        # the block screen comes back as a fresh message: the question message
        # is spent, and the new screen carries the state from here on
        alias = _alias_for(block)
        screen, rows = render_block(alias, result)
        receipt = await notifier.send_message(screen, {"inline_keyboard": rows})
        return {"status": "written" if receipt.delivered else "send failed"}

    async def _orchestrate(
        self, tenant_id: UUID, integration: dict[str, Any], message: dict[str, Any]
    ) -> dict[str, Any]:
        """A plain question from the owner: the orchestrator answers it.

        This chat is the owner's, so any text that reaches here is the owner
        asking — the sender gate has already said so. The orchestrator is
        optional wiring: absent, the old silent-ignore stands, and the menus,
        commands and buttons above are unaffected either way.
        """
        agent = getattr(self._container, "orchestrator_agent", None)
        if agent is None:
            return {"status": "ignored"}
        text = str(message.get("text") or "").strip()
        if not text:
            return {"status": "ignored"}
        chat = message.get("chat") or {}
        reply = await agent.answer(tenant_id, str(chat.get("id") or ""), text)
        notifier = await self._notifier(tenant_id, integration)
        if notifier is None:
            return {"status": "ignored"}
        delivered = False
        for chunk in chunk_reply(reply):
            receipt = await notifier.send_message(chunk)
            delivered = delivered or receipt.delivered
        return {"status": "answered" if delivered else "send failed"}

    async def _reask(
        self, tenant_id: UUID, integration: dict[str, Any], question: str
    ) -> dict[str, Any]:
        notifier = await self._notifier(tenant_id, integration)
        if notifier is None:
            return {"status": "ignored"}
        receipt = await notifier.send_message(
            f"That did not look right.\n{question}", {"force_reply": True}
        )
        return {"status": "asked" if receipt.delivered else "send failed"}

    # -------------------------------------------------------------- plumbing

    async def _notifier(self, tenant_id: UUID, integration: dict[str, Any]) -> Any | None:
        """One notifier for this update, on the integration's stored chat id.

        Same construction rule as the approvals and reports services: the
        token is read from ``tenant_secrets`` per use and never cached, and a
        missing leg is a skip, not an exception.
        """
        from app.adapters.channels.telegram import TelegramApprovalNotifier
        from app.services.approvals.service import TELEGRAM_SECRET_PROVIDER

        token = await read_tenant_secret(
            self._container, tenant_id, integration.get("secret_ref"),
            TELEGRAM_SECRET_PROVIDER,
        )
        if not token:
            # provider mode: a tenant with no bot of their own is served by the
            # platform bot — the binding wrote a chat id, never a secret
            token = str(self._container.settings.TELEGRAM_BOT_TOKEN or "")
        chat_id = str(integration.get("external_ref") or "")
        if not token or not chat_id:
            return None
        try:
            return TelegramApprovalNotifier(
                token,
                chat_id,
                lambda _request: {},
                timeout=self._container.settings.ESCALATION_NOTIFY_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning("telegram_bot_notifier_rejected", tenant_id=str(tenant_id))
            return None

    async def _edit(
        self,
        notifier: Any,
        chat_id: str,
        message_id: Any,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Edit the source message to its outcome. An omitted reply_markup
        would leave the old keyboard alive, so it becomes an empty one; and a
        failed edit (a source message older than 48h cannot be edited) falls
        back to a fresh message, so an outcome is never silently lost."""
        if reply_markup is None:
            reply_markup = {"inline_keyboard": []}
        receipt = await notifier.edit_message(chat_id, message_id, text, reply_markup)
        if not receipt.delivered:
            await notifier.send_message(text, reply_markup)


def _alias_for(block: str) -> str:
    for alias, name in SETTINGS_MENU:
        if name == block:
            return alias
    return "of"


async def read_tenant_secret(
    container: Any, tenant_id: UUID, ref: Any, provider: str
) -> str | None:
    """Decrypt one tenant secret by reference — the same read the approvals
    and reports services make, stated once here rather than a fourth time."""
    if not ref:
        return None
    from sqlalchemy import text

    from app.core.db import tenant_scope
    from app.core.security import decrypt_secret

    async with tenant_scope(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT ciphertext FROM tenant_secrets "
                    "WHERE tenant_id = :tid AND ref = :ref AND provider = :provider"
                ),
                {"tid": tenant_id, "ref": str(ref), "provider": provider},
            )
        ).mappings().first()
    if row is None:
        return None
    try:
        return decrypt_secret(str(row["ciphertext"]), container.settings.ENCRYPTION_KEY)
    except Exception:
        logger.warning("telegram_bot_secret_undecryptable", provider=provider)
        return None
