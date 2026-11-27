"""Verified identity and contact resolution.

Three jobs, in the order a request meets them:

**Establish.** `IdentityService.establish` runs the whole handshake once, at
session creation: the deployment switch, the identity-verify rate limit, the
verifier lookup, `app.core.identity`'s four mandatory checks, and the fail-closed
replay claim. It never raises for a bad token — an invalid, expired or replayed
token returns an unverified outcome carrying a reason, and the caller mints an
anonymous session. A tenant whose token minting breaks must not get a session
that will not load.

**Resolve.** `resolve_for_action` is the only path by which a `{{contact.*}}` or
`{{user.*}}` hole is ever filled. It refuses on two distinct grounds and returns
which one: `identity_required` when this session is not verified, and
`identity_incomplete` when it is verified but the stored row lacks the field the
template names. Neither is ever satisfied with `""` — interpolating an empty
string into `GET /orders?customer={{contact.external_id}}` does not fail, it
succeeds against a *different* resource, which is why this is a hard error and
not a best-effort substitution.

**Attribute hygiene.** `contacts.attributes` is flat and string-valued because
its values are interpolated into URL paths and query strings; a nested object has
nothing sensible to render and an unbounded value produces a malformed request.

Contact attributes never enter the model's context. They populate outbound
requests only. The single exception is `identity_preamble`, which an agent may
opt into: it is written by the server, from stored values, never by the model.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from time import time
from typing import Any
from uuid import UUID

from app.core.constants import (
    CONTACT_ATTRIBUTE_KEY_PATTERN,
    CONTACT_COLUMN_FIELDS,
    CONTACT_EVENT_IDENTITY_VERIFIED,
    CONTACT_SOURCE_EMAIL,
    CONTACT_SOURCE_IDENTITY_JWT,
    CONTACT_SOURCE_WHATSAPP,
    IDENTITY_CONTACT_NAMESPACES,
    IDENTITY_OUTCOME_INCOMPLETE,
    IDENTITY_OUTCOME_REQUIRED,
    IDENTITY_REJECT_LOG_SAMPLE,
    IDENTITY_VERIFY_RATE_PER_MINUTE,
    RL_SCOPE_IDENTITY_VERIFY,
)
from app.core.errors import RateLimitError, ValidationError
from app.core.identity import (
    REJECT_NO_VERIFIER,
    REJECT_NOT_ENABLED,
    REJECT_RATE_LIMITED,
    IdentityRejected,
    VerifiedIdentity,
    jti_ledger_ttl,
    verify_identity_token,
)
from app.core.logging import get_logger
from app.core.security import hash_identifier
from app.domain.actions import TEMPLATE_RE

logger = get_logger(__name__)

_ATTRIBUTE_KEY_RE = re.compile(CONTACT_ATTRIBUTE_KEY_PATTERN)


@dataclass(frozen=True, slots=True)
class IdentityOutcome:
    """What the session endpoint learned, and what it puts in the token.

    `verified` false with `reason` None means no token was presented at all,
    which is the ordinary anonymous case rather than a failure worth surfacing.
    """

    verified: bool
    reason: str | None = None
    contact_id: UUID | None = None
    name: str | None = None
    email: str | None = None
    # hash_identifier(sub, tenant_salt): stable across the signed-in person's
    # devices, which is what lets them resume one thread on a laptop and a phone.
    # The raw `sub` is never used as a continuity key; it lives once, in
    # contacts.external_id.
    sub_hash: str | None = None
    # min(session expiry, token expiry) as a unix timestamp, or None
    identity_expires_at: int | None = None
    claims: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContactResolution:
    """The result of filling a `{{contact.*}}` / `{{user.*}}` namespace.

    `ok` false always carries an `outcome` of `identity_required` or
    `identity_incomplete`, and `values` is then never used. There is no third
    state in which a hole renders empty.
    """

    ok: bool
    outcome: str | None = None
    values: dict[str, str] = field(default_factory=dict)
    missing: tuple[str, ...] = ()


def contact_holes(*templates: str | None) -> tuple[str, ...]:
    """The `contact.` / `user.` field names a set of templates depends on.

    Reads the same `TEMPLATE_RE` the executor renders with, so a hole this misses
    is a hole that does not render either.
    """
    found: list[str] = []
    for template in templates:
        if not template:
            continue
        for match in TEMPLATE_RE.finditer(template):
            namespace, _, rest = match.group(1).partition(".")
            if namespace in IDENTITY_CONTACT_NAMESPACES and rest and rest not in found:
                found.append(rest)
    return tuple(found)


def build_namespace(row: dict[str, Any]) -> dict[str, str]:
    """A contacts row as the `{{contact.*}}` namespace.

    Columns are tried first and `attributes` is the fallback, so
    `{{contact.plan}}` finds `attributes->>'plan'` while `{{contact.email}}`
    cannot be shadowed by a tenant-supplied attribute of the same name.
    """
    values: dict[str, str] = {}
    for column in CONTACT_COLUMN_FIELDS:
        raw = row.get(column)
        if raw is not None and str(raw) != "":
            values[column] = str(raw)
    attributes = row.get("attributes") or {}
    if isinstance(attributes, dict):
        for key, value in attributes.items():
            if value is None or str(value) == "":
                continue
            values.setdefault(str(key), str(value))
    return values


def validate_attributes(
    raw: Any, *, max_keys: int, max_value_chars: int
) -> dict[str, str]:
    """Flat, string-valued, bounded, and key-shaped. Raises `ValidationError`.

    Enforced here rather than only in the database because the message a tenant
    reads has to name the offending key, and a CHECK constraint cannot.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError("attributes must be a flat object of string values")
    if len(raw) > max_keys:
        raise ValidationError(f"at most {max_keys} attributes are allowed")

    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key)
        if not _ATTRIBUTE_KEY_RE.match(name):
            raise ValidationError(
                f"attribute key '{name}' must match {CONTACT_ATTRIBUTE_KEY_PATTERN}"
            )
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise ValidationError(f"attribute '{name}' must be a string value")
        text_value = str(value)
        if len(text_value) > max_value_chars:
            raise ValidationError(
                f"attribute '{name}' exceeds {max_value_chars} characters"
            )
        cleaned[name] = text_value
    return cleaned


def identity_preamble(row: dict[str, Any]) -> str:
    """The opt-in identity preamble, written by the server from stored values.

    This is the one place a contact field reaches the model's context, and it
    carries name and email only — never `external_id` and never an attribute. It
    is what lets a booking action stop asking for details we already hold, which
    is a deliberate tension with "attributes never enter the context" and is
    documented in the UI next to the toggle.
    """
    name = str(row.get("name") or "").strip()
    email = str(row.get("email") or "").strip()
    if name and email:
        return f"You are speaking with {name} ({email}), signed in."
    if name:
        return f"You are speaking with {name}, signed in."
    if email:
        return f"You are speaking with the signed-in holder of {email}."
    return "You are speaking with a signed-in visitor."


class IdentityService:
    """The identity handshake and contact resolution, over the container.

    Constructed with the container rather than its parts because it needs four of
    them — settings, the contacts repo, the cache and the rate limiter — and the
    cache in particular is the same object `RateLimiter` wraps.
    """

    def __init__(self, container: Any) -> None:
        self._container = container
        self._rejections = 0

    @property
    def _settings(self) -> Any:
        return self._container.settings

    @property
    def _repo(self) -> Any:
        return self._container.contacts_repo

    async def enabled(self) -> bool:
        """Off unless this deployment's switch is on.

        ``IDENTITY_VERIFICATION_ENABLED`` is an operator decision about the
        deployment, not a commercial one about a tenant.
        """
        return bool(self._settings.IDENTITY_VERIFICATION_ENABLED)

    async def establish(
        self,
        *,
        tenant_id: UUID,
        agent_id: UUID | None,
        token: str | None,
        audiences: Sequence[str] = (),
        session_ttl_seconds: int = 900,
        previous_visitor_id: str | None = None,
    ) -> IdentityOutcome:
        """Verify once, at session creation, and upsert the person behind the token.

        Never raises for a bad token. An invalid, expired or replayed token yields
        an unverified outcome with a reason and the caller mints an anonymous
        session; the cost is that "look up my order" then fails, and that is
        mitigated by the reason travelling back to the host page and by
        `reject_count` on the verifier row. The raw token is never stored and never
        logged.
        """
        if not token or not token.strip():
            return IdentityOutcome(verified=False)

        now = int(time())
        try:
            identity = await self._verify(
                tenant_id=tenant_id,
                agent_id=agent_id,
                token=token,
                audiences=audiences,
                now=now,
            )
        except IdentityRejected as rejected:
            await self._note_rejection(tenant_id, agent_id, rejected.reason)
            return IdentityOutcome(verified=False, reason=rejected.reason)

        row = await self._repo.upsert_by_external_id(
            tenant_id,
            external_id=identity.subject,
            source=CONTACT_SOURCE_IDENTITY_JWT,
            verified=True,
            email=identity.email,
            phone=identity.phone,
            name=identity.name,
            attributes=identity.attributes,
        )
        contact_id = row.get("id")
        sub_hash = hash_identifier(identity.subject, self._tenant_salt(tenant_id))

        if contact_id is not None:
            if previous_visitor_id:
                # The anonymous thread this visitor abandoned on signing in. Hashed
                # with the same tenant salt so what we keep is a pseudonym, not the
                # browser's raw identifier.
                previous = hash_identifier(
                    previous_visitor_id, self._tenant_salt(tenant_id)
                )
                if previous != sub_hash:
                    await self._repo.append_external_user_ref(
                        tenant_id, contact_id, previous
                    )
            await self._repo.append_external_user_ref(tenant_id, contact_id, sub_hash)
            await self._repo.record_event(
                tenant_id,
                contact_id,
                kind=CONTACT_EVENT_IDENTITY_VERIFIED,
                payload={
                    "verifier_id": str(identity.verifier_id),
                    "source": CONTACT_SOURCE_IDENTITY_JWT,
                },
            )
        await self._repo.record_verification(tenant_id, identity.verifier_id)

        return IdentityOutcome(
            verified=True,
            contact_id=contact_id,
            name=identity.name,
            email=identity.email,
            sub_hash=sub_hash,
            # the session must never outlive the assertion behind it
            identity_expires_at=min(now + session_ttl_seconds, identity.expires_at),
            claims=identity.claims(),
        )

    async def _verify(
        self,
        *,
        tenant_id: UUID,
        agent_id: UUID | None,
        token: str,
        audiences: Sequence[str],
        now: int,
    ) -> VerifiedIdentity:
        """The gate, the limiter, the four checks, and the replay claim."""
        if not await self.enabled():
            raise IdentityRejected(REJECT_NOT_ENABLED, "verification is off")

        # Its own scope, separate from the session quota, so a broken integration
        # minting bad tokens in a loop cannot exhaust session creation for everyone.
        try:
            await self._container.rate_limiter.check(
                RL_SCOPE_IDENTITY_VERIFY,
                str(tenant_id),
                IDENTITY_VERIFY_RATE_PER_MINUTE,
                60,
            )
        except RateLimitError as exc:
            raise IdentityRejected(REJECT_RATE_LIMITED, "too many attempts") from exc

        candidates = await self._repo.load_verifier_candidates(
            tenant_id, agent_id, self._settings.ENCRYPTION_KEY
        )
        if not candidates:
            raise IdentityRejected(REJECT_NO_VERIFIER, "no verifier registered")

        skew = int(self._settings.IDENTITY_CLOCK_SKEW_SECONDS)
        identity = verify_identity_token(
            token,
            candidates=candidates,
            audiences=audiences,
            now=now,
            clock_skew_seconds=skew,
            max_ttl_seconds=int(self._settings.IDENTITY_MAX_TTL_SECONDS),
            max_attributes=int(self._settings.IDENTITY_MAX_ATTRIBUTES),
        )

        await self._repo.claim_jti(
            tenant_id,
            identity.jti,
            ttl_seconds=jti_ledger_ttl(identity, now=now, clock_skew_seconds=skew),
            ledger_seconds=int(self._settings.IDENTITY_JTI_LEDGER_RETENTION_SECONDS),
            cache=self._container.cache,
        )
        return identity

    async def _note_rejection(
        self, tenant_id: UUID, agent_id: UUID | None, reason: str
    ) -> None:
        """Count the rejection on the candidate rows and log it, sampled.

        Sampled because a tenant whose minting is broken produces one rejection per
        page load, and an unsampled warning would bury every other line in the log
        while telling an operator nothing the first one did not.
        """
        self._rejections += 1
        if self._rejections % IDENTITY_REJECT_LOG_SAMPLE == 1:
            logger.warning(
                "identity_token_rejected", tenant_id=str(tenant_id), reason=reason
            )
        try:
            await self._repo.record_rejection(tenant_id, agent_id)
        except Exception:
            # Bookkeeping for a dashboard warning. It must not turn a graceful
            # anonymous session into a failed request.
            logger.warning("identity_reject_count_failed", tenant_id=str(tenant_id))

    def _tenant_salt(self, tenant_id: UUID) -> str:
        """The same per-tenant salt WhatsApp hashes senders with."""
        return f"{self._settings.SECRET_KEY}:{tenant_id}"

    # ------------------------------------------------------------- resolution

    async def resolve_for_action(
        self,
        tenant_id: UUID,
        *,
        contact_id: UUID | None,
        verified: bool,
        holes: Sequence[str] = (),
    ) -> ContactResolution:
        """Fill the `{{contact.*}}` namespace, or refuse and say which way.

        **A `{{contact.*}}` hole resolves only from a verified identity.** An
        unverified session is refused here with `identity_required` before any
        request is built, which is the second of §4.6's three redundant layers
        reaching the one place that owns contact data.

        A verified session whose stored row lacks a named field is refused with
        `identity_incomplete`. There is no path that substitutes an empty string:
        `?customer=` does not fail against the tenant's API, it succeeds against a
        different resource, and that is the whole reason this is a hard error.
        """
        if not verified or contact_id is None:
            return ContactResolution(ok=False, outcome=IDENTITY_OUTCOME_REQUIRED)

        row = await self._repo.get(tenant_id, contact_id)
        if row is None:
            # RLS turns a cross-tenant or deleted contact_id into no row at all,
            # which is the same refusal as never having been verified.
            return ContactResolution(ok=False, outcome=IDENTITY_OUTCOME_REQUIRED)

        values = build_namespace(row)
        missing = tuple(name for name in holes if not values.get(name))
        if missing:
            return ContactResolution(
                ok=False, outcome=IDENTITY_OUTCOME_INCOMPLETE, missing=missing
            )
        return ContactResolution(ok=True, values=values)

    async def preamble_for(self, tenant_id: UUID, contact_id: UUID | None) -> str:
        """The opt-in identity preamble for a verified conversation, or "".

        Server-written from stored name and email only, which is what lets an
        action that needs `{{contact.*}}` work without the model having been told
        any attribute.
        """
        if contact_id is None:
            return ""
        row = await self._repo.get(tenant_id, contact_id)
        if row is None or row.get("verified_at") is None:
            return ""
        return identity_preamble(row)

    # ---------------------------------------------------------- other channels

    async def establish_from_whatsapp(
        self, tenant_id: UUID, *, sender: str, name: str | None = None
    ) -> IdentityOutcome:
        """WhatsApp: verified, because Meta's webhook signature says so.

        A signed webhook is a *stronger* assertion than a tenant JWT, so
        `identity_verified` is true. The caveat the UI must state is that it proves
        control of a phone number, not that the number matches an account in the
        tenant's system: resolution therefore exposes `phone` and collected data
        only, and an action needing the tenant's own customer id must still look it
        up by phone.
        """
        salt = self._tenant_salt(tenant_id)
        sender_ref = hash_identifier(sender, salt)
        row = await self._repo.upsert_by_external_id(
            tenant_id,
            external_id=f"whatsapp:{sender_ref}",
            source=CONTACT_SOURCE_WHATSAPP,
            verified=True,
            phone=sender,
            name=name,
        )
        contact_id = row.get("id")
        if contact_id is not None:
            await self._repo.append_external_user_ref(tenant_id, contact_id, sender_ref)
        return IdentityOutcome(
            verified=True,
            contact_id=contact_id,
            name=row.get("name"),
            sub_hash=sender_ref,
        )

    async def establish_from_email(
        self,
        tenant_id: UUID,
        *,
        address: str,
        name: str | None = None,
        dkim_passed: bool = False,
    ) -> IdentityOutcome:
        """Email: unverified unless DKIM passed for the sending domain.

        The `From` header is not an assertion of anything on its own, so the
        contact is still created — the tenant wants the record — but
        `identity_verified` stays false and no `{{contact.*}}` hole will resolve
        for it.
        """
        normalised = address.strip().lower()
        row = await self._repo.upsert_by_external_id(
            tenant_id,
            external_id=f"email:{normalised}",
            source=CONTACT_SOURCE_EMAIL,
            verified=dkim_passed,
            email=normalised,
            name=name,
        )
        contact_id = row.get("id")
        return IdentityOutcome(
            verified=dkim_passed,
            reason=None if dkim_passed else "dkim_unverified",
            contact_id=contact_id,
            name=row.get("name"),
            email=normalised,
        )


