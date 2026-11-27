"""JustDial: the lead source that arrives as mail.

JustDial has no push API a tenant can point at us. What it does have is a
setting in the seller panel that mails every enquiry to an address the tenant
chooses, so the onboarding story is: tenant sets that address to their inbound
address, and the enquiries start arriving as mail. This module is the other
half — turning one of those mails into a lead.

**The parser is deterministic, and that is a hard rule.** Regex and labelled
templates only, never an LLM. A format JustDial changes is fixed once by
adding a pattern here; an LLM rescue call would cost money on every failure
forever and hide the broken pattern (it is on the never-build list in
``implement.md`` §0.5). A mail none of the patterns can read stays in
``growth_lead_raw`` with ``parse_error`` set — visible, with the body attached,
rather than silently rescued into a guessed lead.

Everything after the parse is not this module's business: the lead goes
through the same ``InboundService.ingest`` tail as a website form — same
dedupe by ``lead_uid``, same qualification pass, same owner ping, same
acknowledgement. A JustDial enquiry is not a different kind of lead; only the
envelope is different.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any
from uuid import UUID

from app.core.errors import ProviderUnavailableError
from app.core.logging import get_logger
from app.domain.growth import normalise_email, normalise_phone

logger = get_logger(__name__)

AGENT_KEY = "growth_justdial"
PROVIDER = "justdial"

# JustDial's own notification senders. The match is on the From domain, because
# a forwarding rule preserves the original sender — the mail the tenant's
# mailbox forwards to us still says From: something@justdial.com, and that is
# precisely what distinguishes it from the reply traffic this channel also
# carries.
_JUSTDIAL_DOMAIN = re.compile(r"@([\w.-]*\.)?justdial\.com$", re.IGNORECASE)

# A labelled field, "Label: value" or "Label - value", on its own line. The
# colon form covers every JustDial template seen; the dash form is admitted
# because their HTML mails sometimes render labels without the colon. The
# value runs to end of line — JustDial puts one field per row, and a value
# that greedily crossed rows would swallow the next label. The en dash is
# escaped because it is deliberate (templates use it) and ruff would otherwise
# read it as an ambiguity to fix.
_LABELLED = re.compile(
    # the en dash is deliberate: JustDial templates use it
    r"^\s*(?P<label>[A-Za-z][A-Za-z /]{1,30}?)\s*[:–-]\s*(?P<value>.+?)\s*$",  # noqa: RUF001
    re.MULTILINE,
)

# Which label each JustDial row name maps to, and the order they are tried.
# Multiple labels name the same field in real templates ("Mobile", "Contact
# Number", "Phone"); first match wins and later rows do not overwrite it.
_LABEL_ALIASES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("name", "contact person", "sender name", "customer name"), "name"),
    (
        ("mobile", "mobile number", "contact number", "phone", "phone number", "contact"),
        "phone",
    ),
    (("email", "email id", "e mail", "mail id"), "email"),
    (("company", "company name", "firm", "business name"), "company"),
    (
        ("requirement", "requirement details", "enquiry", "enquiry details", "query",
         "message", "details", "product"),
        "message",
    ),
    (("city", "location", "address"), "location"),
)

# The subject line of a JustDial enquiry names the product, and some templates
# put the requirement only there. "New Enquiry for X", "Business Enquiry: X".
_SUBJECT_PRODUCT = re.compile(
    r"(?:enquir\w+|requirement|lead)\s*(?:for|:|-)\s*(?P<product>.+)$",
    re.IGNORECASE,
)

# What makes the mail garbled rather than merely sparse: a lead we cannot
# answer is worse than no lead, it is a row on the board nobody can action —
# the same rule the website-form parser applies in `_validate`.
PARSE_ERROR_NO_CONTACT = (
    "the JustDial mail carries no usable email address or phone number"
)
PARSE_ERROR_UNRECOGNISED = (
    "the JustDial mail matches no known template; none of its labelled fields "
    "could be read"
)

_MAX_FIELD_CHARS = 2000
_MAX_BODY_CHARS = 20000


def is_justdial_sender(from_address: str) -> bool:
    """True when the mail's From domain is JustDial's own.

    The cheap gate the email webhook runs before anything else: one regex on
    one header, no database, no parse. A forwarded JustDial mail keeps the
    original sender, so this is stable under every forwarding setup that
    preserves From (all of them that a tenant can actually configure).
    """
    return bool(_JUSTDIAL_DOMAIN.search(from_address.strip()))


def parse_justdial_mail(
    subject: str, text: str | None, html: str | None
) -> tuple[dict[str, str], str]:
    """Read one JustDial mail into lead fields. Deterministic, always.

    Returns ``(fields, "")`` on success and ``({}, reason)`` on failure — a
    reason rather than an exception because "this mail is not one we can read"
    is an ordinary outcome the caller records on the raw row, not an error
    condition that should park a job.

    Text body first: JustDial's multipart mails carry a legible text/plain
    part, and reading the HTML only when there is no text keeps the parser off
    tag-stripping edge cases it does not need to face. Pure and exported so
    real mail shapes can be tested exhaustively without a database.

    Both bodies are capped at the door, before any regex sees them: the
    tag-stripper is quadratic in the worst case (a body of ``<`` characters
    makes every ``[^>]+`` walk to end-of-input), and a mail is attacker-sized
    input on this path — the From domain is the only gate ahead of this
    function, and From is spoofable. ``re.sub`` is synchronous, so an
    uncapped multi-megabyte body would not just burn this job, it would stall
    the worker's event loop under every tenant's jobs.
    """
    body = (text or "")[:_MAX_BODY_CHARS].strip()
    if not body:
        body = _html_to_text((html or "")[:_MAX_BODY_CHARS])

    fields: dict[str, str] = {}
    for match in _LABELLED.finditer(body[:_MAX_BODY_CHARS]):
        label = _slug_label(match.group("label"))
        value = match.group("value").strip()
        # trailing colon is the HTML fallback's doing: the close of the last
        # cell in a row becomes one, and the value does not want it
        value = value.rstrip(":").strip()
        if not value or len(value) > _MAX_FIELD_CHARS:
            continue
        for aliases, field in _LABEL_ALIASES:
            if label in aliases:
                # first row wins: a template that repeats a label puts the
                # readable one first and the masked duplicate after
                fields.setdefault(field, value)
                break

    # the subject often carries the only mention of what they asked for
    if "message" not in fields:
        product = _SUBJECT_PRODUCT.search(subject or "")
        if product:
            fields["message"] = product.group("product").strip()[:_MAX_FIELD_CHARS]

    if not fields:
        return {}, PARSE_ERROR_UNRECOGNISED

    # the values are normalised here rather than in InboundService._upsert's
    # caller, because the decision "is this mail actionable" belongs with the
    # parser: the same no-contact rule as the website form, stated once per
    # source rather than once per pipeline hop
    phone = normalise_phone(fields.get("phone", ""))
    email = normalise_email(fields.get("email", ""))
    if not phone and not email:
        return {}, PARSE_ERROR_NO_CONTACT
    if phone:
        fields["phone"] = phone
    if email:
        fields["email"] = email
    return fields, ""


def _html_to_text(html: str) -> str:
    """Tag-stripped HTML, rows rebuilt as "Label:value" lines.

    Labels and values live in separate table cells in JustDial's HTML, and a
    cell boundary is not punctuation — strip the tags naively and "Name" and
    "Priya Patel" end up as unrelated fragments of one line. So the close of a
    cell becomes the colon the line parser keys on, rows and ``<br>`` become
    newlines, and only then do the remaining tags go.
    """
    cleaned = re.sub(r"<style[^>]*>.*?</style>|<script[^>]*>.*?</script>", "", html,
                     flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"</td>", ":", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"<(tr|/tr|br|/p|/div|/table)[^>]*>",
        "\n",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"<td[^>]*>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = _html.unescape(cleaned)
    return re.sub(r"[ \t]+", " ", cleaned)


def _slug_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip().lower())


class JustDialService:
    """One JustDial mail: fetch, parse, archive, hand to the shared pipeline."""

    def __init__(self, container: Any) -> None:
        self._c = container

    async def handle(self, tenant_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """The ``growth_inbound_lead`` job, ``source == "justdial"`` branch.

        The queue payload carries no mail content — the same rule as the
        ``email_reply`` job: nothing sensitive traverses the queue, the worker
        re-fetches the message from Resend by id.
        """
        provider_id = str(payload.get("provider_id") or "")
        message_id = str(payload.get("message_id") or provider_id)
        if not provider_id:
            return {"status": "skipped", "reason": "no provider id"}

        integration = await self._c.growth.integrations.get(tenant_id, PROVIDER)
        if integration is None or integration.get("status") != "active":
            # connected, then disconnected between the webhook and now. The
            # same refusal the ingress makes, for the same reason: a tenant
            # who turned a source off is entitled to have it be off
            return {"status": "skipped", "reason": "justdial is not connected"}

        provider = getattr(self._c, "email", None)
        if provider is None:
            logger.warning("justdial_backend_unconfigured", tenant_id=str(tenant_id))
            return {"status": "skipped", "reason": "email_backend_unconfigured"}

        try:
            # the provider raises on failure rather than returning None. The
            # taxonomy matters here: a transient outage (ProviderUnavailableError
            # — timeout, network) re-raises so the queue retries this job, which
            # re-fetches by id; a permanent one (ProviderError — the mail is
            # gone, the payload unreadable) must NOT burn the retry ladder to
            # dead-letter, so it is recorded and the job ends
            received = await provider.fetch_received(provider_id)
        except ProviderUnavailableError:
            raise
        except Exception:
            logger.warning(
                "justdial_fetch_failed",
                tenant_id=str(tenant_id),
                provider_id=provider_id,
                exc_info=True,
            )
            return {"status": "failed", "reason": "mail could not be fetched"}

        # the archive is written before anything believes the body, exactly as
        # for a form post: a template change is a visible row with the mail
        # attached, not a log line about a body nobody kept. The HTML goes in
        # too, because for an HTML-only mail it is the only body there is
        raw_row = await self._c.growth.raw.store(
            tenant_id,
            source=PROVIDER,
            payload={
                "subject": received.subject,
                "from": received.from_address,
                "text": (received.text or "")[:_MAX_BODY_CHARS],
                "html": (received.html or "")[:_MAX_BODY_CHARS],
            },
            dedupe_key=message_id,
        )
        raw_id: UUID
        if raw_row is None:
            # the Message-ID collided. Usually a webhook redelivery of a lead
            # already made, but a job that died between archive and resolve
            # leaves the row unclaimed — re-reading it is the difference
            # between a free no-op and a lead lost forever
            existing = await self._c.growth.raw.get_by_dedupe_key(
                tenant_id, PROVIDER, message_id
            )
            if existing is None or existing.get("lead_id") is not None:
                return {"status": "duplicate"}
            if existing.get("parse_error"):
                # already recorded as unreadable; re-parsing the same mail
                # would just record it again
                return {"status": "failed", "reason": str(existing["parse_error"])}
            raw_id = UUID(str(existing["id"]))
        else:
            raw_id = UUID(str(raw_row["id"]))

        fields, problem = parse_justdial_mail(
            received.subject, received.text, received.html
        )
        if problem:
            await self._fail(tenant_id, raw_id, problem)
            return {"status": "failed", "reason": problem}

        await self._note_health(tenant_id, error=None)
        result: dict[str, Any] = await self._c.growth_inbound.ingest(
            tenant_id,
            source=PROVIDER,
            fields=fields,
            raw_id=raw_id,
        )
        return result

    # ---------------------------------------------------------------- private

    async def _fail(self, tenant_id: UUID, raw_id: Any, problem: str) -> None:
        """Record a parse failure on the raw row and on the card, never silently.

        The raw row is the evidence (mail body attached, ``parse_error`` set).
        The card note is the visibility: the Integrations screen renders
        ``health.last_error`` for this provider whenever it is present, so a
        stream of unreadable templates surfaces on the card instead of only in
        a table nobody opens.
        """
        await self._c.growth.raw.resolve(
            tenant_id, UUID(str(raw_id)), parse_error=problem
        )
        logger.warning(
            "justdial_parse_failed",
            tenant_id=str(tenant_id),
            reason=problem,
        )
        await self._note_health(tenant_id, error=problem)

    async def _note_health(
        self, tenant_id: UUID, error: str | None
    ) -> None:
        """Read-merge-write the integration row's ``health`` column.

        ``health`` is a whole-column replace at the SQL layer, so a writer that
        passes a fresh dict silently erases whatever else the column carried —
        the Stage 5 hazard recorded in ``implement.md`` §0.7. Read, merge, then
        write, and never let bookkeeping kill the lead: a failure here is
        logged and swallowed.

        The row is re-read rather than trusting the snapshot the job started
        with, because ``upsert`` overwrites ``status`` unconditionally: a
        tenant who disconnected the source while this job ran must not have it
        written back to ``active`` by the very job they disconnected from.
        """
        current = await self._c.growth.integrations.get(tenant_id, PROVIDER)
        if current is None or current.get("status") != "active":
            # disconnected mid-job: leave it disconnected
            return
        health = dict(current.get("health") or {})
        if error is None:
            health.pop("last_error", None)
            health["last_lead_at"] = _now_iso()
        else:
            health["last_error"] = error[:500]
            health["last_error_at"] = _now_iso()
        try:
            await self._c.growth.integrations.upsert(
                tenant_id,
                provider=PROVIDER,
                status="active",
                external_ref=str(current.get("external_ref") or ""),
                secret_ref=current.get("secret_ref"),
                health=health,
            )
        except Exception:
            logger.warning(
                "justdial_health_write_failed", tenant_id=str(tenant_id), exc_info=True
            )


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
