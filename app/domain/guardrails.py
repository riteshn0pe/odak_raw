"""Input guardrails: prompt injection and abuse screening.

Design rule that the old code got wrong: never block on a bare topic keyword.
Words like hack, attack, exploit and suicide appear constantly in legitimate
questions about cyber law, security policy and criminal statutes. Blocking them
outright made whole domains unusable.

So screening looks for instruction shaped patterns, meaning an attempt to
redirect the system, not for subject matter. Topic policy is a per tenant concern
expressed in the system prompt, not a hardcoded blocklist.
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum


class Verdict(StrEnum):
    ALLOW = "allow"
    FLAG = "flag"
    BLOCK = "block"


@dataclass(slots=True)
class ScreenResult:
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def blocked(self) -> bool:
        return self.verdict == Verdict.BLOCK


# each pattern targets an attempt to change the system's instructions or to make
# it reveal them. weights are additive, the threshold decides the verdict
INJECTION_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    (
        "override_instructions",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b"
            r"(previous|prior|above|earlier|all)\b[^.\n]{0,20}"
            r"\b(instruction|prompt|rule|direction|context)s?\b",
            re.IGNORECASE,
        ),
        1.0,
    ),
    (
        "reveal_system_prompt",
        re.compile(
            r"\b(reveal|show|print|repeat|output|display|give)\b[^.\n]{0,30}\b"
            r"(your |the )?(system|initial|original|hidden|secret)\b[^.\n]{0,20}"
            r"\b(prompt|instruction|message|rule)s?\b",
            re.IGNORECASE,
        ),
        1.0,
    ),
    (
        "role_reassignment",
        re.compile(
            r"\b(you are now|from now on you|act as|pretend to be|роль)\b[^.\n]{0,40}"
            r"\b(dan|jailbroken|unrestricted|developer mode|no restrictions|admin)\b",
            re.IGNORECASE,
        ),
        1.0,
    ),
    (
        "guardrail_removal",
        re.compile(
            r"\b(without|bypass|disable|remove|turn off|skip)\b[^.\n]{0,25}\b"
            r"(restriction|guardrail|filter|safety|censor|limitation)s?\b",
            re.IGNORECASE,
        ),
        0.8,
    ),
    (
        "fake_authority",
        re.compile(
            r"^\s*(system|developer|admin|root)\s*[:>\]]",
            re.IGNORECASE | re.MULTILINE,
        ),
        0.7,
    ),
    (
        "delimiter_injection",
        re.compile(
            r"(<\|(im_start|im_end|system|endoftext)\|>|\[/?INST\]|###\s*(system|instruction))",
            re.IGNORECASE,
        ),
        1.0,
    ),
    (
        "exfiltrate_config",
        re.compile(
            r"\b(what|list|show|dump)\b[^.\n]{0,30}\b"
            r"(api[_\s\-]?key|secret|token|credential|password|env(ironment)? var)",
            re.IGNORECASE,
        ),
        1.0,
    ),
    (
        "encoded_payload",
        re.compile(
            r"\b(base64|rot13|hex)\b[^.\n]{0,30}\b(decode|decrypt|then (run|execute|follow))",
            re.IGNORECASE,
        ),
        0.5,
    ),
]

# only slurs and direct threats against a person. subject matter is never here
ABUSE_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    (
        "direct_threat",
        re.compile(
            r"\b(i (will|am going to|wanna|want to)|we will)\b[^.\n]{0,20}\b"
            r"(kill|murder|rape|shoot|stab|bomb)\b[^.\n]{0,20}\b(you|him|her|them|u)\b",
            re.IGNORECASE,
        ),
        1.0,
    ),
    (
        "sexual_content_minor",
        re.compile(
            r"\b(child|minor|kid|underage|teen)\b[^.\n]{0,30}\b"
            r"(porn|sexual|nude|naked|explicit)\b",
            re.IGNORECASE,
        ),
        1.0,
    ),
]

BLOCK_THRESHOLD = 1.0
FLAG_THRESHOLD = 0.5

MAX_QUERY_CHARS = 8000
# a wall of repeated characters is a context flooding attempt, not a question
MAX_REPEAT_RUN = 200


def screen_input(text: str, max_chars: int = MAX_QUERY_CHARS) -> ScreenResult:
    reasons: list[str] = []
    score = 0.0

    stripped = text.strip()
    if not stripped:
        return ScreenResult(Verdict.BLOCK, ["empty_query"], 1.0)

    if len(text) > max_chars:
        return ScreenResult(Verdict.BLOCK, ["query_too_long"], 1.0)

    if re.search(r"(.)\1{%d,}" % MAX_REPEAT_RUN, text):
        reasons.append("repetition_flood")
        score += 1.0

    for name, pattern, weight in INJECTION_PATTERNS:
        if pattern.search(text):
            reasons.append(f"injection:{name}")
            score += weight

    for name, pattern, weight in ABUSE_PATTERNS:
        if pattern.search(text):
            reasons.append(f"abuse:{name}")
            score += weight

    if score >= BLOCK_THRESHOLD:
        return ScreenResult(Verdict.BLOCK, reasons, score)
    if score >= FLAG_THRESHOLD:
        return ScreenResult(Verdict.FLAG, reasons, score)
    return ScreenResult(Verdict.ALLOW, reasons, score)


def screen_retrieved_text(text: str) -> ScreenResult:
    """Indirect injection check on document content.

    An uploaded file can carry instructions aimed at the model. Retrieved text is
    never trusted, so a hit here downgrades the chunk rather than the request.
    """
    reasons: list[str] = []
    score = 0.0
    for name, pattern, weight in INJECTION_PATTERNS:
        if pattern.search(text):
            reasons.append(f"document_injection:{name}")
            score += weight
    verdict = Verdict.FLAG if score >= FLAG_THRESHOLD else Verdict.ALLOW
    return ScreenResult(verdict, reasons, score)


SELF_HARM_PATTERN = re.compile(
    r"\b(i (want|wanna|am going|plan) to (kill myself|end (my|it) (life|all))"
    r"|(kill|hurt|harm) myself"
    r"|thinking about (suicide|ending my life))\b",
    re.IGNORECASE,
)


def needs_crisis_response(text: str) -> bool:
    """First person distress, not the topic of suicide.

    A question about section 309 or about a prevention policy is a normal query.
    Only a first person statement of intent triggers a crisis reply.
    """
    return bool(SELF_HARM_PATTERN.search(text))
