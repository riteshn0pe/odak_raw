"""Pure growth-platform rules: contact normalisation, lead identity, dedup,
suppression checks and language resolution.

No I/O, no imports outside the standard library — the same posture as ``pii.py``
and ``email_text.py``. Every function here is a decision the agents make dozens
of times a day against text a stranger typed, so each one has to be readable and
testable on its own, without a database.

The two rules worth stating up front, because everything else follows from them:

* **Identity is derived, never assigned.** ``lead_uid`` hashes the normalised
  contact triple, so the same company found twice through two different sources
  lands on one row. That is why normalisation is in this file and not in a
  service — if two call sites normalised differently, dedup would quietly stop
  working and nobody would notice until a prospect got two cold mails.
* **Language follows the customer.** ``resolve_language`` never guesses over
  evidence: the moment a human writes to us in their own language, that beats
  every stored preference and every region hint, forever.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# Lead status is a forward-only ladder. dedupe_lead uses the position, not the
# name: a later sighting of a lead we have already mailed must not drag it back
# to `new` because the second source knew less than the first.
LEAD_STATUS_ORDER: tuple[str, ...] = (
    "new",
    "researched",
    "queued",
    "mailed",
    "replied",
    "positive",
    "meeting",
    "won",
)

# Terminal states sit outside the ladder. They are decisions a human or a hard
# signal made, so nothing merges out of them — a `suppressed` lead that turns up
# again in a scrape stays suppressed.
LEAD_STATUS_TERMINAL: frozenset[str] = frozenset({"lost", "later", "suppressed"})

# India is the default market, so a bare 10-digit mobile is assumed +91. This is
# the one country-specific rule in the file and it is deliberate: guessing wrong
# on an Indian number is the difference between reaching the customer and not,
# and every other origin in the corpus arrives already carrying its own +code.
DEFAULT_COUNTRY_CODE = "91"

# Indian mobile numbers are ten digits starting 6-9. Landlines start with an STD
# code and are not reachable by WhatsApp, so they are kept but never assumed
# mobile — the caller decides what to do with a number this does not match.
_IN_MOBILE = re.compile(r"^[6-9]\d{9}$")

# The shortest national subscriber number in use anywhere. Below this the input
# is an extension, an order reference, or noise — never a phone number.
_MIN_NATIONAL_DIGITS = 7

_NON_DIGITS = re.compile(r"[^\d+]")
_SCHEME_AND_PATH = re.compile(r"^[a-z][a-z0-9+.-]*://|[/?#].*$")
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Devanagari, Bengali, Gurmukhi, Gujarati, Tamil, Telugu, Kannada, Malayalam,
# Arabic. Script is a far more reliable signal than a word list, so it is
# checked first and a hit ends the question.
_SCRIPT_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x0900, 0x097F, "hi"),
    (0x0980, 0x09FF, "bn"),
    (0x0A00, 0x0A7F, "pa"),
    (0x0A80, 0x0AFF, "gu"),
    (0x0B80, 0x0BFF, "ta"),
    (0x0C00, 0x0C7F, "te"),
    (0x0C80, 0x0CFF, "kn"),
    (0x0D00, 0x0D7F, "ml"),
    (0x0600, 0x06FF, "ur"),
)

# Romanised Hindi written in Latin script — "hinglish". A word list is a weak
# signal on its own, which is why two independent hits are required below: one
# stray "kya" in an English mail is a quotation, two is a language choice.
_HINGLISH_MARKERS: frozenset[str] = frozenset(
    {
        "aap", "aapka", "aapko", "hai", "hain", "nahi", "nahin", "kya", "kaise",
        "kitna", "kitne", "karo", "karna", "kar", "chahiye", "bhej", "bhejo",
        "kripya", "dhanyavad", "shukriya", "theek", "thik", "acha", "accha",
        "bataye", "bataiye", "mujhe", "hum", "hume", "humein", "mera", "meri",
        "yeh", "woh", "abhi", "jaldi", "paisa", "rupaye", "sahi", "bhai",
    }
)

# Phone country code to language guess. Only used for a first contact, and only
# when nothing better exists — see resolve_language.
_REGION_LANGUAGE: Mapping[str, str] = {
    "91": "hi",
    "880": "bn",
    "92": "ur",
}

DEFAULT_LANGUAGE = "en"


def normalise_phone(raw: str | None, default_country: str = DEFAULT_COUNTRY_CODE) -> str:
    """Reduce a hand-typed phone number to a stable E.164-ish key.

    "+91 98765-43210", "098765 43210", "9876543210" and "0091 9876543210" are
    one customer, and the dedup key has to see them that way. The result carries
    a leading ``+`` and digits only.

    Returns ``""`` for anything that cannot be a phone number, which is the
    signal callers use to fall back to email as the identity.
    """
    if not raw:
        return ""
    # keep '+' only where it leads: a '+' in the middle is punctuation someone
    # pasted, not a country prefix
    cleaned = _NON_DIGITS.sub("", raw.strip())
    if cleaned.startswith("+"):
        digits = "+" + cleaned[1:].replace("+", "")
    else:
        digits = cleaned.replace("+", "")

    if digits.startswith("+"):
        body = digits[1:]
    elif digits.startswith("00"):
        # the international access prefix, spelled the way most of the world
        # dials it. Strip it and what is left is a country code
        body = digits[2:]
    elif digits.startswith("0"):
        # a national trunk prefix: drop it and apply the default country
        body = default_country + digits.lstrip("0")
    elif _IN_MOBILE.match(digits) and default_country == DEFAULT_COUNTRY_CODE:
        body = default_country + digits
    elif len(digits) > 10:
        # long enough to already carry its own country code
        body = digits
    elif len(digits) >= _MIN_NATIONAL_DIGITS:
        body = default_country + digits
    else:
        # too short to be a subscriber number anywhere. Prefixing a country code
        # onto "12345" would manufacture a plausible-looking number out of an
        # order id, and that number would then dedup against a real person
        return ""

    body = body.lstrip("0")
    # 7 digits is the shortest real subscriber number anywhere; 15 is the E.164
    # ceiling. Outside that this is an extension, an order id, or noise
    if not body.isdigit() or not 7 <= len(body) <= 15:
        return ""
    return "+" + body


def normalise_email(raw: str | None) -> str:
    """Lower-case and trim an address.

    Plus-tags are deliberately kept. ``riya+leads@example.com`` is a real,
    deliverable address that its owner chose in order to filter us — stripping
    the tag would merge two people who asked to be kept apart, and would send
    mail to an address the recipient never gave us.
    """
    if not raw:
        return ""
    value = raw.strip().lower()
    # a display name pasted along with the address: "Riya <riya@example.com>".
    # Unwrapped before any stripping, because stripping the brackets first
    # destroys the very delimiters this needs to find the address between
    if "<" in value and ">" in value:
        value = value[value.rfind("<") + 1 : value.rfind(">")].strip()
    value = value.strip("<>")
    if not _EMAIL_SHAPE.match(value):
        return ""
    return value


def normalise_domain(raw: str | None) -> str:
    """Reduce a URL or a host to the bare registrable-looking host.

    Scheme, path, query, port, trailing dot and a leading ``www.`` all go. What
    is left is what two sources will agree on when they both found the same
    company.
    """
    if not raw:
        return ""
    value = raw.strip().lower()
    value = _SCHEME_AND_PATH.sub("", value)
    value = value.split("@")[-1]  # someone pasted an email instead of a site
    value = value.split(":")[0].rstrip(".")
    if value.startswith("www."):
        value = value[4:]
    if "." not in value or " " in value:
        return ""
    return value


def lead_uid(
    tenant_id: str,
    phone: str | None = None,
    email: str | None = None,
    domain: str | None = None,
) -> str:
    """Deterministic identity for a lead, unique within one tenant.

    The tenant id is inside the hash, not merely beside it in the unique index,
    so the same company researched by two tenants never produces one shared key
    that could leak across the boundary if a query ever forgot its scope.

    The strongest available identifier wins, in the order phone, email, domain —
    a phone number is a person, a domain is a building full of them. Two sources
    that found the same company by different identifiers therefore will *not*
    merge automatically; that is the safe direction to fail, and the merge is
    made explicitly by ``dedupe_lead`` once a query has matched them.

    Raises ``ValueError`` when nothing identifying is present: a lead we cannot
    name is a bug in the caller, not a row to write.
    """
    parts = (
        normalise_phone(phone),
        normalise_email(email),
        normalise_domain(domain),
    )
    for kind, value in zip(("phone", "email", "domain"), parts, strict=False):
        if value:
            raw = f"growth_lead:{tenant_id}:{kind}:{value}"
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    raise ValueError("lead_uid needs at least one of phone, email or domain")


def _status_rank(status: str) -> int:
    try:
        return LEAD_STATUS_ORDER.index(status)
    except ValueError:
        return -1


def dedupe_lead(
    existing: Mapping[str, Any], incoming: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge a fresh sighting of a lead into the row we already hold.

    Three rules, and they exist because the alternative is losing work:

    * a non-empty field is never overwritten with an empty one — a directory
      listing that knows only the company name must not erase the contact a
      human found;
    * ``source`` is first-seen: a lead the website form brought in that a later
      research run or a JustDial enquiry touches again keeps the label of where
      it actually came from. The by-source report answers "where did this lead
      originate", and last-writer-wins would let any second touch relabel it;
    * ``research_summary`` and ``research_links`` are the exception: the newest
      research wins outright, because a summary is a snapshot and a half-merged
      one describes no real company;
    * status only moves forward along ``LEAD_STATUS_ORDER``, and never moves out
      of a terminal state at all.

    Returns a new dict; neither argument is mutated.
    """
    merged = dict(existing)

    for key, value in incoming.items():
        if key in {"status", "research_summary", "research_links"}:
            continue
        if value in (None, "", [], {}):
            continue
        if key == "source" and str(existing.get("source") or ""):
            continue
        merged[key] = value

    summary = incoming.get("research_summary")
    if summary:
        merged["research_summary"] = summary
        # the links are the evidence for that summary. Carrying old links beside
        # a new summary would cite pages the new text never read
        merged["research_links"] = incoming.get("research_links") or []

    current = str(existing.get("status") or "new")
    proposed = str(incoming.get("status") or current)
    if current in LEAD_STATUS_TERMINAL:
        merged["status"] = current
    elif proposed in LEAD_STATUS_TERMINAL:
        # a terminal signal is a decision, and decisions win over progress
        merged["status"] = proposed
    else:
        merged["status"] = (
            proposed if _status_rank(proposed) > _status_rank(current) else current
        )

    return merged


def is_suppressed(
    suppressions: Iterable[Mapping[str, Any]], channel: str, address: str
) -> bool:
    """Is this address blocked on this channel?

    Opting out is cross-channel by design: a person who replied STOP on WhatsApp
    has not consented to be emailed instead. So a suppression recorded on *any*
    channel for the same normalised address blocks all of them, and the
    ``channel`` argument only decides how the address is normalised.

    The rows are passed in rather than fetched, because this is the gate every
    sender calls and a pure predicate is the only version of it that can be
    tested exhaustively.
    """
    target = normalise_phone(address) if channel == "whatsapp" else normalise_email(address)
    if not target:
        # an address we cannot normalise is one we cannot prove is unsuppressed.
        # Refusing to send is the only safe reading
        return True

    for row in suppressions:
        row_channel = str(row.get("channel") or "")
        raw = str(row.get("address") or "")
        stored = normalise_phone(raw) if row_channel == "whatsapp" else normalise_email(raw)
        if stored and stored == target:
            return True
        # a row stored under the other channel's normalisation still counts:
        # compare the raw forms too rather than let a formatting difference
        # reopen a closed door
        if raw.strip().lower() == address.strip().lower():
            return True
    return False


def detect_language(text: str | None) -> str | None:
    """Best-effort language of a piece of customer writing, or None.

    Script first: a single Devanagari character settles it, and no word list can
    be as reliable. Only when the text is entirely Latin does the hinglish word
    check run, and it needs two independent markers — one "kya" inside an
    English sentence is somebody quoting, two is somebody choosing.

    Returns None rather than "en" when there is no evidence either way, so the
    caller can tell "they wrote English" apart from "they wrote nothing".
    """
    if not text or not text.strip():
        return None

    for char in text:
        code = ord(char)
        for start, end, language in _SCRIPT_RANGES:
            if start <= code <= end:
                return language

    words = {
        w
        for w in re.split(r"[^a-z]+", unicodedata.normalize("NFKD", text).lower())
        if w
    }
    if len(words & _HINGLISH_MARKERS) >= 2:
        return "hinglish"
    if any(c.isalpha() for c in text):
        return DEFAULT_LANGUAGE
    return None


def resolve_language(
    last_message: str | None,
    stored_pref: str | None,
    region_hint: str | None,
    allowed: Sequence[str] | None = None,
) -> str:
    """Which language do we write back in?

    Strict precedence, and the order is the whole rule:

    1. **What they just wrote.** Hinglish in, hinglish out. This is evidence, and
       evidence beats every guess we could make about them.
    2. **A stored preference** on the contact — something they told us once.
    3. **A region guess** from a phone country code or an address. Only reachable
       on a first contact, and it is dropped permanently the moment rule 1 or 2
       has anything to say.
    4. **English**, the floor.

    ``allowed`` is the tenant's language list. A resolution outside it falls
    through to the next rule rather than to English directly, so a tenant who
    supports Hindi but not Bengali still answers a Bengali speaker in the
    language they stored for that contact if there is one.

    Owner-facing text does not come through here at all — the owner reads their
    own language regardless of what a customer wrote.
    """
    permitted = [lang for lang in (allowed or []) if lang] or [DEFAULT_LANGUAGE]

    detected = detect_language(last_message)
    if detected and detected in permitted:
        return detected
    if stored_pref and stored_pref in permitted:
        return stored_pref
    if detected is None and region_hint:
        guess = _region_language(region_hint)
        if guess and guess in permitted:
            return guess
    return DEFAULT_LANGUAGE if DEFAULT_LANGUAGE in permitted else permitted[0]


def _region_language(hint: str) -> str | None:
    """Map a region hint — a phone number or a country code — to a language.

    Longest prefix first, so "+91" is not shadowed by a shorter entry if one is
    ever added.
    """
    phone = normalise_phone(hint)
    digits = phone[1:] if phone else re.sub(r"\D", "", hint)
    if not digits:
        return None
    for code in sorted(_REGION_LANGUAGE, key=len, reverse=True):
        if digits.startswith(code):
            return _REGION_LANGUAGE[code]
    return None
