"""PII detection and masking. Regex only, no ML model, so it costs no memory.

Masking runs on the inbound query before it reaches a provider, and on retrieved
text before it enters a prompt. A tenant can extend the rule set with its own
patterns through agent config.

Reversible masking is supported: mask_pii returns a token map so a downstream
answer can have real values restored for the end user without those values ever
crossing a provider boundary.
"""

import re
from dataclasses import dataclass, field
from typing import Pattern

MASK_TEMPLATE = "[{label}_{index}]"


@dataclass(slots=True)
class PIIRule:
    label: str
    pattern: Pattern[str]
    group: int = 1
    validator: str | None = None


@dataclass(slots=True)
class PIIDetection:
    label: str
    placeholder: str
    start: int
    end: int


@dataclass(slots=True)
class MaskResult:
    text: str
    detections: list[PIIDetection] = field(default_factory=list)
    # placeholder to original value, kept server side only
    token_map: dict[str, str] = field(default_factory=dict)

    @property
    def labels(self) -> list[str]:
        return sorted({detection.label for detection in self.detections})

    @property
    def found(self) -> bool:
        return bool(self.detections)

    def restore(self, text: str) -> str:
        for placeholder, original in self.token_map.items():
            text = text.replace(placeholder, original)
        return text


def _luhn_valid(digits: str) -> bool:
    numbers = [int(char) for char in digits if char.isdigit()]
    if len(numbers) < 12:
        return False
    checksum = 0
    parity = len(numbers) % 2
    for index, number in enumerate(numbers):
        if index % 2 == parity:
            number *= 2
            if number > 9:
                number -= 9
        checksum += number
    return checksum % 10 == 0


def _verhoeff_valid(digits: str) -> bool:
    """Aadhaar uses a Verhoeff check digit. Validating it removes most false
    positives from ordinary 12 digit numbers such as amounts or invoice ids."""
    d_table = [
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
        [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
        [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
        [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
        [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
        [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
        [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
        [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
        [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
    ]
    p_table = [
        [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
        [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
        [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
        [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
        [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
        [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
        [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
    ]
    inverse = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]

    cleaned = [int(char) for char in digits if char.isdigit()]
    if len(cleaned) != 12:
        return False
    checksum = 0
    for index, number in enumerate(reversed(cleaned)):
        checksum = d_table[checksum][p_table[index % 8][number]]
    return inverse[checksum] == 0


VALIDATORS = {"luhn": _luhn_valid, "verhoeff": _verhoeff_valid}


DEFAULT_RULES: list[PIIRule] = [
    PIIRule("EMAIL", re.compile(r"\b([\w.%+\-]+@[\w.\-]+\.[A-Za-z]{2,})\b")),
    PIIRule("PAN", re.compile(r"\b([A-Z]{5}\d{4}[A-Z])\b")),
    PIIRule("IFSC", re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")),
    PIIRule("GSTIN", re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9])\b")),
    PIIRule(
        "AADHAAR",
        re.compile(r"\b([2-9]\d{3}[\s\-]?\d{4}[\s\-]?\d{4})\b"),
        validator="verhoeff",
    ),
    PIIRule("CARD", re.compile(r"\b((?:\d[ \-]?){13,19})\b"), validator="luhn"),
    PIIRule("PHONE_IN", re.compile(r"(?:\+91[\s\-]?)?\b([6-9]\d{9})\b")),
    PIIRule("PASSPORT_IN", re.compile(r"\b([A-PR-WY][1-9]\d{6})\b")),
    PIIRule(
        "ACCOUNT",
        re.compile(
            r"(?:a/c|account)\s*(?:no\.?|number|num)?\s*[:\-]?\s*(\d{9,18})\b", re.IGNORECASE
        ),
    ),
    PIIRule("IP", re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3})\b")),
]


def mask_pii(
    text: str,
    extra_rules: list[PIIRule] | None = None,
    *,
    only_labels: frozenset[str] | None = None,
) -> MaskResult:
    """Replace detected values with stable placeholders.

    Matches are applied right to left so earlier offsets stay valid while the
    string is rewritten. Identical values reuse one placeholder, which keeps a
    prompt coherent when the same email appears twice.

    `only_labels` narrows the default rule set to those labels. It exists for
    content that leaves our control — an escalation email or a Slack message —
    where card and government-identifier labels must be masked but the visitor's
    own email and phone must not, or the human cannot reply. Tenant-supplied
    `extra_rules` are always applied: a tenant that declared its own
    account-number pattern meant it.
    """
    defaults = (
        DEFAULT_RULES
        if only_labels is None
        else [rule for rule in DEFAULT_RULES if rule.label in only_labels]
    )
    rules = defaults + (extra_rules or [])
    masked = text
    detections: list[PIIDetection] = []
    token_map: dict[str, str] = {}
    value_to_placeholder: dict[str, str] = {}
    counters: dict[str, int] = {}

    for rule in rules:
        matches = list(rule.pattern.finditer(masked))
        for match in reversed(matches):
            group = rule.group if rule.group <= (match.lastindex or 0) else 0
            original = match.group(group)
            if not original:
                continue

            if rule.validator:
                validator = VALIDATORS.get(rule.validator)
                if validator and not validator(original):
                    continue

            placeholder = value_to_placeholder.get(original)
            if placeholder is None:
                counters[rule.label] = counters.get(rule.label, 0) + 1
                placeholder = MASK_TEMPLATE.format(
                    label=rule.label, index=counters[rule.label]
                )
                value_to_placeholder[original] = placeholder
                token_map[placeholder] = original

            start, end = match.start(group), match.end(group)
            masked = masked[:start] + placeholder + masked[end:]
            detections.append(PIIDetection(rule.label, placeholder, start, end))

    detections.reverse()
    return MaskResult(text=masked, detections=detections, token_map=token_map)


def compile_extra_rules(patterns: list[dict[str, str]]) -> list[PIIRule]:
    """Build rules from tenant supplied config.

    A bad pattern is skipped rather than raised, so one malformed tenant regex
    cannot take down that tenant's chat endpoint. Entries that are not dicts
    (a legacy or hand-edited config carrying bare strings) are skipped the
    same way — `entry.get` on a str would raise and take the endpoint down.
    """
    rules: list[PIIRule] = []
    for entry in patterns:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or "").strip().upper()
        raw = str(entry.get("pattern") or "")
        if not label or not raw:
            continue
        try:
            compiled = re.compile(raw)
        except re.error:
            continue
        rules.append(PIIRule(label, compiled, group=0 if compiled.groups == 0 else 1))
    return rules
