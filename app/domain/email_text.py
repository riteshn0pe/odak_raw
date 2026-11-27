"""Pure email text handling: quote stripping, subject normalisation, loop detection.

Regex and BeautifulSoup only, no infrastructure imports, in the same spirit as
``pii.py`` — every rule in here is a decision about untrusted text and has to be
readable on its own.

Order matters and is load-bearing elsewhere: **strip before masking**. A forty
message thread run through ``mask_pii`` first produces a token map with hundreds
of entries and a ``restore()`` that rewrites the reply unpredictably.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Injected into every outbound body. A reply to *us* is the common case and this
# marker is the only deterministic rule in the ladder — every other rule is a
# heuristic against somebody else's mail client. Front, Intercom and Zendesk all
# do the same thing, which is why their threading looks clean.
OUTBOUND_TEXT_MARKER = "--- Please reply above this line ---"
OUTBOUND_HTML_BOUNDARY_ID = "ca-quote-boundary"
OUTBOUND_HTML_BOUNDARY = f'<div id="{OUTBOUND_HTML_BOUNDARY_ID}"></div>'

# Loop header we set on every send and reject on every receive.
LOOP_HEADER = "x-ca-loop"

# Reasons a message is dropped without an answer. Mirrors the comment on
# email_inbound_receipts.ignored_reason in migrations/008_email.sql.
IGNORED_AUTO_SUBMITTED = "auto_submitted"
IGNORED_BULK = "bulk"
IGNORED_LIST_MAIL = "list_mail"
IGNORED_BOUNCE_SENDER = "bounce_sender"
IGNORED_SELF_ADDRESSED = "self_addressed"
IGNORED_LOOP_HEADER = "loop_header"
IGNORED_SPAM = "spam"
IGNORED_SENDER_NOT_ALLOWED = "sender_not_allowed"

# Below either bound the stripping is treated as a mistake and discarded. A noisy
# question is recoverable; an empty one is not. Same posture as is_legible() in
# the OCR path — recovered garbage loses to what we already had.
_MIN_STRIPPED_CHARS = 20
_MIN_STRIPPED_RATIO = 0.05

_SUBJECT_PREFIX = re.compile(
    r"^\s*(?:(?:re|res|aw|antw|sv|vs|vb|fw|fwd|tr|enc|encaminhada)\s*(?:\[\d+\])?\s*:\s*)+",
    re.IGNORECASE,
)

# "On Tue, 3 Jun 2025 at 14:02, Riya <riya@example.com> wrote:" and the
# localised forms. The line wraps constantly, so the pattern is deliberately
# tolerant about what sits between the date and the colon.
_ATTRIBUTION = re.compile(
    r"^[ \t>]*(?:"
    r"On\s.{0,200}?\swrote\s*:"
    r"|Le\s.{0,200}?\sa\s+(?:é|e)crit\s*:"
    r"|Am\s.{0,200}?\sschrieb\s.{0,120}?:"
    r"|El\s.{0,200}?\sescribi(?:ó|o)\s*:"
    r"|Il\s.{0,200}?\sha\s+scritto\s*:"
    r"|Op\s.{0,200}?\sschreef\s.{0,120}?:"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_ORIGINAL_MESSAGE = re.compile(
    r"^[ \t>]*(?:-{2,}\s*(?:Original\s+Message|Forwarded\s+message|"
    r"Urspr(?:ü|u)ngliche\s+Nachricht|Mensaje\s+original)\s*-{2,}"
    r"|_{20,})\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_HEADER_LINE = re.compile(
    r"^[ \t>]*(?:From|Sent|To|Cc|Bcc|Subject|Date|Reply-To|Von|Gesendet|An|Betreff|"
    r"De|Enviado|Para|Asunto|Objet)\s*:\s?\S",
    re.IGNORECASE,
)
_QUOTE_LINE = re.compile(r"^[ \t]*>")
_SIGNATURE_DELIMITER = re.compile(r"^-- ?$")
_MOBILE_SIGNATURE = re.compile(
    r"^[ \t]*(?:Sent\s+from\s+my\s+\w+|Sent\s+from\s+Outlook(?:\s+for\s+\w+)?|"
    r"Get\s+Outlook\s+for\s+(?:iOS|Android)|Von\s+meinem\s+iPhone\s+gesendet)\b.*$",
    re.IGNORECASE,
)
_AUTO_REPLY_SUBJECT = re.compile(
    r"^\s*(?:auto(?:matic)?[ \-]?reply|autoreply|out\s+of\s+(?:the\s+)?office|"
    r"abwesenheit(?:snotiz)?|r(?:é|e)ponse\s+automatique|ausencia|"
    r"respuesta\s+autom(?:á|a)tica|undelivered\s+mail|delivery\s+status\s+notification|"
    r"mail\s+delivery\s+fail)",
    re.IGNORECASE,
)
_MESSAGE_ID = re.compile(r"<([^<>\s]+)>")
_OWN_MESSAGE_ID = re.compile(r"^<?ca-([0-9a-f]{8,64})-(\d+)@", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")

# HTML containers every major client wraps the quoted history in.
_HTML_QUOTE_SELECTORS = (
    "div.gmail_quote",
    "div.gmail_quote_container",
    "blockquote[type=cite]",
    "div#divRplyFwdMsg",
    "div#appendonsend",
    "div.yahoo_quoted",
    "div.moz-cite-prefix",
    "hr#stopSpelling",
    "div.OutlookMessageHeader",
    "div.protonmail_quote",
)


@dataclass(frozen=True, slots=True)
class StrippedBody:
    """The new content of a reply, plus which rule produced it.

    ``method`` is logged so a bad heuristic is findable in production rather than
    a mystery about "why did it answer last Tuesday's question".
    """

    text: str
    stripped_chars: int
    method: str
    confident: bool


def html_to_text(html: str) -> str:
    """Flatten HTML to text, dropping quote containers, scripts and styles.

    BeautifulSoup is already a dependency (``pyproject.toml`` beautifulsoup4), so
    this costs no new package. An unparseable body degrades to a tag strip rather
    than raising: some question is better than none.
    """
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "head", "title"]):
            tag.decompose()
        for selector in _HTML_QUOTE_SELECTORS:
            for node in soup.select(selector):
                node.decompose()
        for node in soup.find_all("br"):
            node.replace_with("\n")
        for name in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4", "blockquote"):
            for node in soup.find_all(name):
                node.append("\n")
        text = soup.get_text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    collapsed: list[str] = []
    blanks = 0
    for line in lines:
        if line.strip():
            blanks = 0
            collapsed.append(line)
        else:
            blanks += 1
            if blanks <= 1:
                collapsed.append("")
    return "\n".join(collapsed).strip()


def _html_before_boundary(html: str) -> str | None:
    """Cut an HTML body at our own injected boundary, when it is present."""
    lowered = html.lower()
    needle = f'id="{OUTBOUND_HTML_BOUNDARY_ID}"'
    index = lowered.find(needle)
    if index < 0:
        index = lowered.find(f"id='{OUTBOUND_HTML_BOUNDARY_ID}'")
        if index < 0:
            return None
    opening = html.rfind("<", 0, index)
    return html[: opening if opening >= 0 else index]


def _cut_at_marker(text: str) -> str | None:
    index = text.find(OUTBOUND_TEXT_MARKER)
    if index < 0:
        # a client may have re-wrapped or quoted the marker line
        loose = re.search(r"^[ \t>]*-{2,}\s*Please reply above this line\s*-{2,}\s*$",
                          text, re.IGNORECASE | re.MULTILINE)
        if loose is None:
            return None
        index = loose.start()
    return text[:index]


def _cut_at_header_block(text: str) -> str | None:
    """Three or more consecutive quoted-header lines means the history starts here."""
    lines = text.split("\n")
    run_start = -1
    run = 0
    for position, line in enumerate(lines):
        if _HEADER_LINE.match(line):
            if run == 0:
                run_start = position
            run += 1
            if run >= 3:
                return "\n".join(lines[:run_start])
        elif line.strip():
            run = 0
    return None


def _cut_trailing_quote_block(text: str) -> str | None:
    """Walk back from the end over blank and ``>`` lines only.

    Only from the end, so an inline ``> quoted question`` mid-body survives — that
    is somebody quoting our own sentence to ask about it, which is the question.
    """
    lines = text.split("\n")
    index = len(lines)
    saw_quote = False
    while index > 0:
        line = lines[index - 1]
        if _QUOTE_LINE.match(line):
            saw_quote = True
            index -= 1
        elif not line.strip():
            index -= 1
        else:
            break
    if not saw_quote or index == len(lines):
        return None
    return "\n".join(lines[:index])


def _cut_at_signature(text: str) -> str | None:
    lines = text.split("\n")
    for position, line in enumerate(lines):
        if _SIGNATURE_DELIMITER.match(line) or _MOBILE_SIGNATURE.match(line):
            return "\n".join(lines[:position])
    return None


def strip_quoted(text: str, html: str | None = None) -> StrippedBody:
    """Extract just the new content from a reply.

    An email thread accumulates its entire history in every message. Feeding that
    to retrieval means embedding the same paragraph twenty times and answering a
    question the customer asked last Tuesday.

    The ladder runs in order and the first rule that fires wins. If the result
    looks implausible the stripping is discarded entirely and the full body is
    returned with ``confident=False``.
    """
    source = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not source.strip() and html:
        source = html_to_text(html)
        original = source
        method = "html_only"
    else:
        original = source
        method = ""

    candidate: str | None = None

    # 1. our own marker, in text then in HTML. the only deterministic rule here
    candidate = _cut_at_marker(source)
    if candidate is not None:
        method = "own_marker_text"
    if candidate is None and html:
        cut = _html_before_boundary(html)
        if cut is not None:
            candidate = html_to_text(cut)
            method = "own_marker_html"

    # 2. HTML quote containers
    if candidate is None and html:
        flattened = html_to_text(html)
        if flattened and len(flattened) < len(_WHITESPACE.sub(" ", source)):
            candidate = flattened
            method = "html_quote_container"

    working = candidate if candidate is not None else source

    # 3. attribution line
    if candidate is None:
        match = _ATTRIBUTION.search(working)
        if match is not None:
            candidate = working[: match.start()]
            method = "attribution_line"

    # 4. -----Original Message----- and Outlook's horizontal rule
    if candidate is None:
        match = _ORIGINAL_MESSAGE.search(working)
        if match is not None:
            candidate = working[: match.start()]
            method = "original_message"

    # 5. header block
    if candidate is None:
        cut = _cut_at_header_block(working)
        if cut is not None:
            candidate = cut
            method = "header_block"

    # 6. trailing > quote block
    if candidate is None:
        cut = _cut_trailing_quote_block(working)
        if cut is not None:
            candidate = cut
            method = "trailing_quote"

    result = candidate if candidate is not None else working

    # 7. signature delimiter, applied on top of whatever fired above
    signature_cut = _cut_at_signature(result)
    if signature_cut is not None and signature_cut.strip():
        result = signature_cut
        method = f"{method}+signature" if method else "signature"

    result = result.strip()
    if not method:
        method = "no_quote_found"

    # 8. safety net
    stripped_chars = max(len(original) - len(result), 0)
    if original.strip() and (
        len(result) < _MIN_STRIPPED_CHARS
        or len(result) < _MIN_STRIPPED_RATIO * len(original.strip())
    ):
        return StrippedBody(
            text=original.strip(),
            stripped_chars=0,
            method=f"{method}+discarded_by_safety_net",
            confident=False,
        )

    return StrippedBody(
        text=result,
        stripped_chars=stripped_chars,
        method=method,
        confident=method not in {"no_quote_found", "html_only"},
    )


def normalise_subject(subject: str) -> str:
    """Strip a leading run of Re:/Fwd:/AW:/SV: prefixes and collapse whitespace.

    Used as the fallback threading key, so it has to be stable across clients:
    "RE: Re: Fwd: invoice" and "invoice" must produce the same string.
    """
    if not subject:
        return ""
    value = _SUBJECT_PREFIX.sub("", subject)
    return _WHITESPACE.sub(" ", value).strip()


def reply_subject(subject: str) -> str:
    """``Re: <normalised>`` — the prefix is what actually makes Gmail group them."""
    base = normalise_subject(subject)
    if not base:
        return "Re: your message"
    return f"Re: {base}"


def normalise_address(address: str) -> str:
    """Lower-case and unwrap ``Name <addr>``. Dots are left alone deliberately.

    Dot-insensitivity is a Gmail behaviour, not an SMTP one; folding dots for
    every domain would route mail for two genuinely different mailboxes to the
    same tenant.
    """
    if not address:
        return ""
    value = address.strip()
    if "<" in value and ">" in value:
        value = value[value.rfind("<") + 1 : value.rfind(">")]
    return value.strip().strip("'\"").lower()


def strip_plus_tag(address: str) -> str:
    """``support-ab12cd34+tag@x.com`` -> ``support-ab12cd34@x.com`` for lookup only.

    The tagged form is still what gets written to the row: it is what the sender
    actually addressed and a tenant debugging a forwarding rule needs to see it.
    """
    local, separator, domain = normalise_address(address).partition("@")
    if not separator or "+" not in local:
        return normalise_address(address)
    return f"{local.split('+', 1)[0]}@{domain}"


def address_domain(address: str) -> str:
    _, separator, domain = normalise_address(address).partition("@")
    return domain if separator else ""


def address_local_part(address: str) -> str:
    local, separator, _ = strip_plus_tag(address).partition("@")
    return local if separator else ""


def mask_address(address: str) -> str:
    """``riya@gmail.com`` -> ``ri***@gmail.com``. The display form, everywhere."""
    value = normalise_address(address)
    local, separator, domain = value.partition("@")
    if not separator or not local:
        return "***"
    keep = local[:2] if len(local) > 2 else local[:1]
    return f"{keep}***@{domain}"


def parse_message_ids(raw: str | None) -> tuple[str, ...]:
    """Every ``<id>`` in an In-Reply-To or References header, in order.

    Angle brackets are kept: they are part of the RFC 5322 msg-id and keeping
    them makes the stored array directly comparable to a header value.
    """
    if not raw:
        return ()
    found = [f"<{match}>" for match in _MESSAGE_ID.findall(raw)]
    if not found:
        bare = raw.strip()
        if bare and "@" in bare and " " not in bare:
            found = [f"<{bare.strip('<>')}>"]
    seen: set[str] = set()
    ordered: list[str] = []
    for item in found:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return tuple(ordered)


def format_message_id(thread_key: str, seq: int, domain: str) -> str:
    """``<ca-{thread_key}-{seq}@{EMAIL_INBOUND_DOMAIN}>``.

    Set explicitly rather than left to the provider so inbound resolution can
    parse our own id directly and survive a mangled References header.
    """
    return f"<ca-{thread_key}-{seq}@{domain}>"


def parse_own_message_id(message_id: str) -> tuple[str, int] | None:
    """Read ``thread_key`` and sequence back out of one of our own ids."""
    if not message_id:
        return None
    match = _OWN_MESSAGE_ID.match(message_id.strip())
    if match is None:
        return None
    try:
        return match.group(1).lower(), int(match.group(2))
    except ValueError:
        return None


def build_references(inbound_references: tuple[str, ...], inbound_message_id: str) -> str:
    """References for our reply: the inbound chain plus the inbound id, newest 20.

    Trimmed from the left because some clients choke on a very long header and
    keeping the newest entries is what preserves threading.
    """
    chain = [item for item in inbound_references if item]
    if inbound_message_id and inbound_message_id not in chain:
        chain.append(inbound_message_id)
    return " ".join(chain[-20:])


def is_auto_submitted(
    headers: dict[str, str],
    subject: str,
    from_address: str,
    *,
    inbound_domain: str = "",
    channel_address: str = "",
    spam_score: float | None = None,
    spam_threshold: float = 5.0,
) -> str | None:
    """Return an ``ignored_reason``, or None when the mail deserves an answer.

    Every rule here exists because without it our bot and the customer's bot will
    happily converse until someone notices the bill. This is the single most
    important function in the inbound path.
    """
    lowered = {key.lower(): (value or "") for key, value in (headers or {}).items()}

    auto_submitted = lowered.get("auto-submitted", "").strip().lower()
    if auto_submitted and auto_submitted.split(";")[0].strip() != "no":
        return IGNORED_AUTO_SUBMITTED

    precedence = lowered.get("precedence", "").strip().lower()
    if precedence in {"bulk", "list", "junk", "auto_reply"}:
        return IGNORED_BULK

    for header in ("x-auto-response-suppress", "x-autoreply", "x-autorespond",
                   "x-autogenerated", "x-auto-generated", "x-autoresponse"):
        if header in lowered and lowered[header].strip():
            return IGNORED_AUTO_SUBMITTED

    if lowered.get("list-id") or lowered.get("list-unsubscribe") or lowered.get("list-post"):
        return IGNORED_LIST_MAIL

    return_path = lowered.get("return-path", "").strip()
    if return_path in {"<>", ""} and "return-path" in lowered:
        return IGNORED_BOUNCE_SENDER

    if LOOP_HEADER in lowered and lowered[LOOP_HEADER].strip():
        return IGNORED_LOOP_HEADER

    sender = normalise_address(from_address)
    if sender:
        if channel_address and sender == normalise_address(channel_address):
            return IGNORED_SELF_ADDRESSED
        if inbound_domain and address_domain(sender) == inbound_domain.strip().lower():
            return IGNORED_SELF_ADDRESSED
        local = sender.split("@", 1)[0]
        if local in {"mailer-daemon", "postmaster", "no-reply", "noreply", "donotreply",
                     "do-not-reply", "bounce", "bounces"}:
            return IGNORED_BOUNCE_SENDER

    if subject and _AUTO_REPLY_SUBJECT.match(subject):
        return IGNORED_AUTO_SUBMITTED

    if spam_score is not None and spam_score >= spam_threshold:
        return IGNORED_SPAM

    return None


def is_sender_allowed(
    from_address: str, allowed_forwarders: list[str] | tuple[str, ...]
) -> bool:
    """Empty allowlist accepts any sender; non-empty accepts only listed addresses."""
    if not allowed_forwarders:
        return True
    sender = normalise_address(from_address)
    if not sender:
        return False
    allowed = {normalise_address(entry) for entry in allowed_forwarders}
    allowed.discard("")
    return sender in allowed
