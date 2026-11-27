"""What an approval request says, on each destination.

Kept out of the adapters and injected into them, exactly as escalation rendering
is. The adapters know how to talk to the Telegram Bot API, to
``hooks.slack.com`` and to Meta; what an approval *asks* is a product decision
and it changes far more often than any of those wire formats.

One rule runs through all three renderers: **the owner sees the same text
everywhere**. The preview lines are built once by the service and handed to every
renderer, so approving from Telegram and approving from the panel are decisions
about the same thing. A notifier that summarised the payload itself would
eventually summarise it differently, and the owner would be approving two
different batches depending on which screen they happened to be looking at.
"""

from __future__ import annotations

import html
from typing import Any

from app.domain.ports import ApprovalRequest

# What a human reads instead of the feature key.
FEATURE_LABELS: dict[str, str] = {
    "outbound_batch": "Outbound batch",
    "campaign": "Campaign",
    "review_ask": "Review request",
}

# Preview lines shown on a chat destination. The full batch is one tap away in
# the panel; a chat message that scrolls for two screens gets ignored, and an
# ignored approval expires, which is the failure this whole component exists to
# avoid.
CHAT_PREVIEW_ITEMS = 5
CHAT_ITEM_CHARS = 140


def feature_label(feature: str) -> str:
    return FEATURE_LABELS.get(feature, feature.replace("_", " ").capitalize())


def _one_line(text: str, limit: int) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def _preview(approval: ApprovalRequest) -> list[str]:
    return [
        _one_line(item, CHAT_ITEM_CHARS)
        for item in approval.items[:CHAT_PREVIEW_ITEMS]
        if item and item.strip()
    ]


def _remainder(approval: ApprovalRequest) -> int:
    return max(0, len(approval.items) - CHAT_PREVIEW_ITEMS)


def _deadline(approval: ApprovalRequest) -> str:
    if approval.expires_at is None:
        return ""
    return approval.expires_at.strftime("%d %b, %H:%M UTC")


def render_telegram_message(approval: ApprovalRequest) -> dict[str, Any]:
    """sendMessage body for one approval, with approve/reject callback buttons.

    HTML parse mode rather than Markdown: a lead's company name containing an
    underscore is common, and Telegram's Markdown would either mangle it or
    reject the whole message. Every interpolated value is escaped.

    The buttons are Telegram callbacks (``ap:<id>:y``), not panel URLs: the
    press is answered in chat and the message edits to the outcome, which is
    P6's whole interaction model. The presser is authenticated by the sender
    gate before the callback is acted on, and the panel's own decide endpoint
    remains the fallback surface.
    """
    label = html.escape(feature_label(approval.feature))
    summary = html.escape(_one_line(approval.summary, 300))
    lines = [f"<b>{label}</b> needs your approval", "", summary]

    preview = _preview(approval)
    if preview:
        lines.append("")
        lines.extend(f"• {html.escape(item)}" for item in preview)
        extra = _remainder(approval)
        if extra:
            lines.append(f"…and {extra} more")

    deadline = _deadline(approval)
    if deadline:
        lines.extend(["", f"Expires {html.escape(deadline)} — after that it is held, not sent."])

    approval_id = str(approval.approval_id)
    buttons = [
        [
            {"text": "✅ Approve", "callback_data": f"ap:{approval_id}:y"},
            {"text": "✖️ Reject", "callback_data": f"ap:{approval_id}:n"},
        ]
    ]

    return {
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        # a link preview of our own dashboard adds nothing and pushes the buttons
        # below the fold on a phone
        "link_preview_options": {"is_disabled": True},
        "reply_markup": {"inline_keyboard": buttons},
    }


def render_slack_blocks(approval: ApprovalRequest) -> dict[str, Any]:
    """Block Kit payload for one approval.

    URL buttons, not interactive components: an interactive Slack action needs a
    request URL, a signing secret and a new ingress, and the dashboard already
    authenticates the person pressing the button.
    """
    label = feature_label(approval.feature)
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{label} needs approval"[:150]},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _one_line(approval.summary, 2000) or "(no summary)"},
        },
    ]

    preview = _preview(approval)
    if preview:
        body = "\n".join(f"• {item}" for item in preview)
        extra = _remainder(approval)
        if extra:
            body += f"\n_…and {extra} more_"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})

    deadline = _deadline(approval)
    if deadline:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Expires {deadline}. After that the batch is held, not sent.",
                    }
                ],
            }
        )

    if approval.approve_url and approval.reject_url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "url": approval.approve_url,
                    },
                    {
                        "type": "button",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "url": approval.reject_url,
                    },
                ],
            }
        )

    return {"text": f"{label} needs approval", "blocks": blocks}


def render_whatsapp_text(approval: ApprovalRequest) -> str:
    """Plain text for the owner's own WhatsApp number.

    No buttons: this goes out as a utility template or a free-form message inside
    the owner's own service window, and neither carries a link button reliably
    across clients. The URL is in the body, which every client makes tappable.
    """
    label = feature_label(approval.feature)
    lines = [f"{label} needs your approval.", _one_line(approval.summary, 400)]

    preview = _preview(approval)
    if preview:
        lines.append("")
        lines.extend(f"- {item}" for item in preview)
        extra = _remainder(approval)
        if extra:
            lines.append(f"...and {extra} more")

    deadline = _deadline(approval)
    if deadline:
        lines.append("")
        lines.append(f"Expires {deadline}. After that it is held, not sent.")
    if approval.approve_url:
        lines.append("")
        lines.append(f"Review: {approval.approve_url}")
    return "\n".join(line for line in lines if line is not None)
