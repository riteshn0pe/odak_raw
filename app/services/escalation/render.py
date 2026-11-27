"""Rendering for escalation notifications: one email pair, one Slack payload.

Kept out of the adapters and injected into them. The adapters know how to talk to
Resend and to ``hooks.slack.com``; what an escalation *says* is a product
decision, and it changes far more often than either wire format.

Everything here assumes the event was already re-masked by
``EscalationService.build_event``. Nothing in this module calls ``mask_pii``,
deliberately: if masking lived at the render step then every new destination kind
would be one forgotten call away from leaking a card number into a support inbox.
"""

from __future__ import annotations

import html
from typing import Any

from app.domain.escalation import (
    SLACK_QUESTION_CHARS,
    EscalationEvent,
    EscalationTrigger,
)

# What a human reads instead of the enum value.
TRIGGER_LABELS: dict[str, str] = {
    EscalationTrigger.LOW_CONFIDENCE: "Low confidence",
    EscalationTrigger.UNGROUNDED: "Answer not grounded in sources",
    EscalationTrigger.USER_REQUESTED: "Visitor asked for a human",
    EscalationTrigger.NEGATIVE_FEEDBACK: "Visitor rejected the answer",
    EscalationTrigger.CRISIS: "Crisis language detected",
    EscalationTrigger.BLOCKLIST: "Blocked phrase",
    EscalationTrigger.MODEL_REQUESTED: "Agent asked for a human",
    EscalationTrigger.EMAIL_UNDELIVERABLE: "Email could not be delivered",
    EscalationTrigger.EMAIL_THREAD_LOOP: "Email thread reply limit reached",
    EscalationTrigger.DRAFT: "Drafted for a human to send",
}

# A crisis escalation is not a support ticket. It is first in the subject line
# because a subject that buries it behind a tenant name gets read too late.
_URGENT_TRIGGERS = frozenset({EscalationTrigger.CRISIS, EscalationTrigger.USER_REQUESTED})

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_WRAP = f"font-family:{_FONT};font-size:15px;line-height:1.55;color:#1f2328;"
_LABEL = "color:#57606a;font-size:12px;text-transform:uppercase;letter-spacing:.04em;"
_QUOTE = "margin:4px 0 16px;padding:10px 12px;background:#f6f8fa;border-radius:6px;"
_META = "margin:0 0 4px;font-size:13px;color:#57606a;"


def trigger_label(trigger: str) -> str:
    return TRIGGER_LABELS.get(trigger, trigger.replace("_", " ").capitalize())


def _one_line(value: str, limit: int) -> str:
    collapsed = " ".join((value or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def escalation_subject(event: EscalationEvent) -> str:
    """``[Escalation] Low confidence — "how do I close my account"``.

    The question is in the subject because a support inbox is triaged from the
    subject list, and "Escalation from Acme" tells the reader nothing.
    """
    prefix = "[Urgent]" if event.trigger in _URGENT_TRIGGERS else "[Escalation]"
    question = _one_line(event.question, 70)
    label = trigger_label(str(event.trigger))
    if question:
        return f'{prefix} {label} — "{question}"'
    return f"{prefix} {label} — {event.agent_name}"


def _text_sections(event: EscalationEvent) -> list[str]:
    sections: list[str] = [
        f"{trigger_label(str(event.trigger))} on {event.agent_name} ({event.tenant_name}).",
        "",
        "QUESTION",
        event.question.strip() or "(empty)",
    ]

    if event.answer and event.answer.strip():
        sections += ["", "WHAT THE AGENT SAID", event.answer.strip()]

    contact = event.contact
    if contact is not None and not contact.is_empty:
        parts = [part for part in (contact.name, contact.email, contact.phone) if part]
        sections += ["", "CONTACT", ", ".join(parts) + f" (from {contact.source})"]
    else:
        sections += ["", "CONTACT", "None captured. Reply in the dashboard thread instead."]

    if event.considered:
        sections += ["", "SOURCES CONSIDERED"]
        sections += [
            f"- {ref.filename} p{ref.page_from} (score {ref.score:.2f})"
            for ref in event.considered
        ]

    if event.transcript:
        sections += ["", "TRANSCRIPT"]
        sections += [
            f"{turn.role}: {_one_line(turn.content, 400)}" for turn in event.transcript
        ]

    meta = [f"Channel: {event.channel}", f"Confidence: {event.confidence:.2f}"]
    if event.external_user_ref:
        meta.append(f"Visitor: {event.external_user_ref[:16]}")
    if event.page_url:
        meta.append(f"Page: {event.page_url}")
    if event.occurred_at is not None:
        meta.append(f"When: {event.occurred_at.isoformat(timespec='seconds')}")
    sections += ["", "DETAILS", *meta]

    if event.note:
        sections += ["", "NOTE", event.note]
    if event.dashboard_url:
        sections += ["", f"Open in dashboard: {event.dashboard_url}"]
    return sections


def _html_block(label: str, body: str) -> str:
    return (
        f'<p style="{_LABEL}">{html.escape(label)}</p>'
        f'<div style="{_QUOTE}">{body}</div>'
    )


def _escape_multiline(value: str) -> str:
    return html.escape(value.strip(), quote=False).replace("\n", "<br />")


def render_escalation_email(event: EscalationEvent) -> tuple[str, str, str]:
    """Subject, plain text body, HTML body."""
    text = "\n".join(_text_sections(event))

    parts: list[str] = [
        f'<p style="margin:0 0 16px;"><strong>{html.escape(trigger_label(str(event.trigger)))}'
        f"</strong> on {html.escape(event.agent_name)} "
        f"({html.escape(event.tenant_name)})</p>",
        _html_block("Question", _escape_multiline(event.question) or "(empty)"),
    ]

    if event.answer and event.answer.strip():
        parts.append(_html_block("What the agent said", _escape_multiline(event.answer)))

    contact = event.contact
    if contact is not None and not contact.is_empty:
        joined = ", ".join(part for part in (contact.name, contact.email, contact.phone) if part)
        parts.append(
            _html_block(
                "Contact",
                f"{html.escape(joined)} "
                f'<span style="color:#8b949e;">(from {html.escape(contact.source)})</span>',
            )
        )
    else:
        parts.append(
            _html_block("Contact", "None captured. Reply in the dashboard thread instead.")
        )

    if event.considered:
        items = "".join(
            f"<li>{html.escape(ref.filename)} p{ref.page_from} "
            f'<span style="color:#8b949e;">score {ref.score:.2f}</span></li>'
            for ref in event.considered
        )
        parts.append(
            f'<p style="{_LABEL}">Sources considered</p>'
            f'<ul style="margin:4px 0 16px;padding-left:20px;">{items}</ul>'
        )

    if event.transcript:
        turns = "".join(
            f'<p style="margin:0 0 6px;"><strong>{html.escape(turn.role)}:</strong> '
            f"{_escape_multiline(_one_line(turn.content, 400))}</p>"
            for turn in event.transcript
        )
        parts.append(f'<p style="{_LABEL}">Transcript</p><div style="{_QUOTE}">{turns}</div>')

    meta_lines = [f"Channel: {event.channel}", f"Confidence: {event.confidence:.2f}"]
    if event.external_user_ref:
        meta_lines.append(f"Visitor: {event.external_user_ref[:16]}")
    if event.occurred_at is not None:
        meta_lines.append(f"When: {event.occurred_at.isoformat(timespec='seconds')}")
    parts.append(
        "".join(f'<p style="{_META}">{html.escape(line)}</p>' for line in meta_lines)
    )

    if event.note:
        parts.append(_html_block("Note", _escape_multiline(event.note)))

    if event.dashboard_url:
        # The one link in the message. Rendered as a button because the reply is
        # written in the dashboard, not in this mail.
        parts.append(
            f'<p style="margin:20px 0 0;"><a href="{html.escape(event.dashboard_url, quote=True)}"'
            ' style="background:#0969da;color:#ffffff;padding:9px 16px;border-radius:6px;'
            'text-decoration:none;display:inline-block;">Open in dashboard</a></p>'
        )

    return escalation_subject(event), text, f'<div style="{_WRAP}">{"".join(parts)}</div>'


def render_slack_blocks(event: EscalationEvent) -> dict[str, Any]:
    """Block Kit payload for one escalation.

    Slack sees deliberately less than the email: the question truncated to
    ``SLACK_QUESTION_CHARS``, no transcript and no answer body. A Slack channel is
    usually wider than the support team, and the full text lives one click away in
    the dashboard, behind the tenant's own access control.
    """
    label = trigger_label(str(event.trigger))
    question = _one_line(event.question, SLACK_QUESTION_CHARS) or "(empty)"
    contact = event.contact
    contact_text = "not captured"
    if contact is not None and not contact.is_empty:
        contact_text = ", ".join(
            part for part in (contact.name, contact.email, contact.phone) if part
        )

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{label} · {event.agent_name}"[:150]},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f">{question}"}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Channel*\n{event.channel}"},
                {"type": "mrkdwn", "text": f"*Confidence*\n{event.confidence:.2f}"},
                {"type": "mrkdwn", "text": f"*Contact*\n{contact_text[:120]}"},
                {"type": "mrkdwn", "text": f"*Tenant*\n{event.tenant_name[:80]}"},
            ],
        },
    ]

    if event.considered:
        top = event.considered[0]
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Closest source: {top.filename} p{top.page_from} "
                        f"(score {top.score:.2f})",
                    }
                ],
            }
        )

    if event.dashboard_url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open in dashboard"},
                        "url": event.dashboard_url,
                    }
                ],
            }
        )

    # `text` is the notification fallback: without it the mobile push is blank.
    return {"text": f"{label}: {question}"[:300], "blocks": blocks}
