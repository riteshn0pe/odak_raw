"""Human escalation vocabulary: the moment the agent gives up and a person is told.

Pure. No infrastructure imports, no database, no HTTP — the same shape as
``guardrails.py`` and ``pii.py``, and for the same reason: every rule in here has
to be readable and testable on its own.

The detector follows the ``guardrails.py`` doctrine of intent-shaped patterns
rather than bare topic keywords. "I want to speak to a human" is a request for a
person; "our human resources policy" is a question about a document, and a
detector that cannot tell them apart pages a support team for the second one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.core.constants import Channel


class EscalationTrigger(StrEnum):
    """Why a human is being told. Mirrors the escalations_trigger_valid CHECK."""

    LOW_CONFIDENCE = "low_confidence"
    UNGROUNDED = "ungrounded"
    USER_REQUESTED = "user_requested"
    NEGATIVE_FEEDBACK = "negative_feedback"
    CRISIS = "crisis"
    BLOCKLIST = "blocklist"
    MODEL_REQUESTED = "model_requested"
    EMAIL_UNDELIVERABLE = "email_undeliverable"
    EMAIL_THREAD_LOOP = "email_thread_loop"
    # Draft mode is not a failure. The agent produced a healthy answer and the
    # channel is configured to have a human send it, so the reason this row
    # exists is the configuration, not anything wrong with the answer. It is
    # deliberately absent from DEFAULT_ESCALATION_TRIGGERS: a tenant does not
    # opt into it, the channel's draft setting implies it. See migration
    # 011_escalation_draft_trigger.sql.
    DRAFT = "draft"


class EscalationState(StrEnum):
    """Mirrors the escalations_state_valid CHECK."""

    PENDING = "pending"
    NOTIFIED = "notified"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


# Why an escalation was never sent. Recorded in the audit detail, not the row.
SUPPRESSED_DISABLED = "disabled"
SUPPRESSED_TRIGGER_OFF = "trigger_off"
SUPPRESSED_COOLDOWN = "cooldown"
SUPPRESSED_RATE_LIMITED = "rate_limited"
SUPPRESSED_NO_DESTINATION = "no_destination"
SUPPRESSED_NO_CONTACT = "no_contact"

CONTACT_SOURCE_EMAIL_FROM = "email_from"
CONTACT_SOURCE_WHATSAPP_SENDER = "whatsapp_sender"
CONTACT_SOURCE_MODEL = "model"
CONTACT_SOURCE_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class VisitorContact:
    """How the human can reply. Deliberately never masked — see ESCALATION_MASK_LABELS."""

    email: str | None = None
    name: str | None = None
    phone: str | None = None
    source: str = CONTACT_SOURCE_UNKNOWN

    @property
    def is_empty(self) -> bool:
        return not (self.email or self.phone)


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A chunk that was retrieved and judged insufficient.

    The single most useful field in an escalation: it names the document that was
    almost right, which is the document the tenant should fix.
    """

    filename: str
    page_from: int = 0
    score: float = 0.0
    # optional because a citation that survived validate_citations still may not
    # carry a document id, and a placeholder id would render as a broken link
    document_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class TranscriptTurn:
    role: str
    content: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EscalationEvent:
    """Everything a notifier may render. Assembled once, per escalation."""

    id: UUID
    tenant_id: UUID
    tenant_name: str
    agent_id: UUID | None
    agent_name: str
    trigger: EscalationTrigger
    channel: Channel
    conversation_id: UUID | None
    message_id: UUID | None
    question: str
    answer: str | None
    confidence: float
    considered: tuple[SourceRef, ...] = ()
    transcript: tuple[TranscriptTurn, ...] = ()
    contact: VisitorContact | None = None
    external_user_ref: str | None = None
    page_url: str | None = None
    occurred_at: datetime | None = None
    dashboard_url: str = ""
    note: str | None = None


# Re-masking label set for content that leaves our control.
#
# Present: the identifiers that are never needed to reply and are a breach in an
# audit if they turn up in a support inbox or a Slack channel.
# Deliberately absent: EMAIL, PHONE_IN, IP. A masked contact makes the escalation
# undeliverable and the feature pointless, and an address in the *body* is
# usually load-bearing ("my colleague bob@x.com cannot log in") — masking it
# destroys the question. Accepted risk, documented here rather than discovered.
ESCALATION_MASK_LABELS = frozenset(
    {"CARD", "AADHAAR", "PAN", "ACCOUNT", "GSTIN", "IFSC", "PASSPORT_IN"}
)

# What Slack is allowed to see, in characters of the question.
SLACK_QUESTION_CHARS = 200


_PERSON = r"(?:human|person|someone|agent|representative|rep|advisor|adviser|operator)"

HUMAN_REQUEST_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "talk to a human", "speak with a real person", "chat to an agent"
    re.compile(rf"\b(?:talk|speak|chat)\s+(?:to|with)\s+(?:an?\s+)?(?:real\s+|live\s+)?{_PERSON}\b",
               re.IGNORECASE),
    # "speak to your manager", "talk to a supervisor"
    re.compile(r"\b(?:talk|speak)\s+(?:to|with)\s+(?:your\s+|a\s+|the\s+)?"
               r"(?:manager|supervisor|team\s+lead)\b", re.IGNORECASE),
    # "connect me to support", "transfer me to an agent", "put me through to someone"
    re.compile(rf"\b(?:connect|transfer|put|hand)\s+(?:me|this|us)\s+"
               rf"(?:over\s+|through\s+)?(?:to|with)\s+(?:an?\s+)?(?:{_PERSON}|team|support|"
               r"support\s+team|customer\s+service)\b", re.IGNORECASE),
    # "I need to speak to somebody", "I would like to talk to a person"
    re.compile(rf"\b(?:want|need|would\s+like|wanna)\s+(?:to\s+)?"
               rf"(?:talk|speak|deal)\s+(?:to|with)\s+(?:an?\s+)?(?:real\s+)?{_PERSON}\b",
               re.IGNORECASE),
    # "human agent", "human support", "human being please"
    re.compile(r"\bhuman\s+(?:agent|support|assistance|help|being|please)\b", re.IGNORECASE),
    # "a real person", "an actual human"
    re.compile(r"\b(?:real|actual|live)\s+(?:person|human|agent)\b", re.IGNORECASE),
    # "is there a human", "can I get someone"
    re.compile(rf"\b(?:is|are)\s+there\s+(?:an?y?\s+)?(?:real\s+)?{_PERSON}\b", re.IGNORECASE),
    re.compile(rf"\b(?:can|could|may)\s+i\s+(?:please\s+)?(?:get|have|reach)\s+(?:an?\s+)?"
               rf"(?:hold\s+of\s+)?{_PERSON}\b", re.IGNORECASE),
    # explicit escalation language
    re.compile(r"\b(?:please\s+)?escalate\s+(?:this|it|my|the)\b", re.IGNORECASE),
    re.compile(r"\bescalate\s+to\s+(?:a\s+)?(?:human|person|manager|supervisor|team)\b",
               re.IGNORECASE),
    # "customer service representative", "support representative"
    re.compile(r"\b(?:customer\s+(?:service|support)|support)\s+"
               r"(?:agent|rep|representative|person|human)\b", re.IGNORECASE),
    # "I don't want to talk to a bot", "stop talking to me like a bot"
    re.compile(r"\b(?:not|don'?t|do\s+not|no)\s+(?:want\s+(?:to\s+)?)?"
               r"(?:talk|speak|deal)?\s*(?:to\s+)?(?:a\s+)?(?:bot|robot|chatbot|ai)\b",
               re.IGNORECASE),
)


def wants_human(text: str) -> bool:
    """True when the message is a request for a person, not a question about people.

    Deterministic and cheap on purpose: it runs before retrieval and before any
    model spend, so a visitor who asks for a human never pays for an answer they
    did not want.
    """
    if not text:
        return False
    candidate = " ".join(text.split())
    if len(candidate) > 2000:
        # a request for a human is short. a very long body is a pasted document,
        # and scanning it with a dozen alternating patterns is wasted work
        candidate = candidate[:2000]
    return any(pattern.search(candidate) for pattern in HUMAN_REQUEST_PATTERNS)


@dataclass(slots=True)
class EscalationDecision:
    """Outcome of maybe_escalate, for a caller that wants to tell the user."""

    escalation_id: UUID | None = None
    suppressed_reason: str | None = None
    user_message: str = ""
    collect_contact: bool = False
    contact_required: bool = False
    receipts: list[dict[str, object]] = field(default_factory=list)

    @property
    def escalated(self) -> bool:
        return self.escalation_id is not None
