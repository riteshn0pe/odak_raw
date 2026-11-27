"""Verified visitor identity: a hand-rolled HS256 JWT verifier.

Hand-rolled over the standard library's `hmac`, `hashlib` and `json` plus the
base64url helpers already in `app.core.security` — deliberately not PyJWT. The
reason is not dependency minimalism: a library's `decode()` is one keyword
argument away from accepting an algorithm we did not intend, and the whole point
of this module is that the algorithm check is explicit, first, and unmissable.

The order of operations is the security property:

1. exactly three dot-separated segments, or reject;
2. the header decodes to an object whose `alg` is exactly ``"HS256"`` — not "one
   of a set of HMAC algorithms", exactly that string — with no `crit` header and
   a `typ` of `JWT` if it is present at all;
3. the signature is verified with `hmac.compare_digest` **before the payload is
   decoded**, so no claim is ever read out of an unverified token;
4. only then are the claims checked: `sub`, `iat`, `exp`, `jti` required, TTL
   bounded, `iss` and `aud` matched.

Rejecting every algorithm but HS256 closes both halves of the confusion attack:
an attacker submitting `RS256` so the verifier treats the shared secret as a
public key, and an attacker signing `HS256` using the tenant's published public
key as the secret. `alg: "none"` is not a special case here — it simply is not
the string `"HS256"`.

Replay protection and secret decryption are not in this module. This module is
pure: token bytes and a candidate list in, a `VerifiedIdentity` or an
`IdentityRejected` out. The `jti` ledger and the `tenant_secrets` read live in
`app.adapters.db.contacts_repo`, because they touch the database and the cache.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence
from dataclasses import dataclass
from time import time
from typing import Any
from uuid import UUID

from app.core.security import _b64url_decode, _b64url_encode

# The only algorithm this platform will ever accept. Compared by exact string.
IDENTITY_ALG = "HS256"

# A signed identity token is a browser-supplied string; cap it before any parse.
MAX_TOKEN_CHARS = 8192

# Claim bounds. Signed values are trusted, not unbounded: they are interpolated
# into outbound URLs and rendered in a dashboard.
MAX_SUB_CHARS = 128
MAX_JTI_CHARS = 128
MAX_EMAIL_CHARS = 320
MAX_NAME_CHARS = 200
MAX_PHONE_CHARS = 40
MAX_ISSUER_CHARS = 200
MAX_ATTRIBUTE_VALUE_CHARS = 200

# Rejection reasons. Returned to the host page in the session response's
# `identity` block, so they name what the tenant's minting code did wrong and
# never which of the tenant's own verifier rows was tried.
REJECT_MALFORMED = "malformed"
REJECT_ALGORITHM = "bad_algorithm"
REJECT_SIGNATURE = "bad_signature"
REJECT_CLAIMS = "bad_claims"
REJECT_EXPIRED = "expired"
REJECT_NOT_YET_VALID = "not_yet_valid"
REJECT_TTL_TOO_LONG = "ttl_too_long"
REJECT_ISSUER = "issuer_mismatch"
REJECT_AUDIENCE = "audience_mismatch"
REJECT_REPLAYED = "replayed"
REJECT_NO_VERIFIER = "no_verifier"
REJECT_REPLAY_UNAVAILABLE = "replay_check_unavailable"
REJECT_NOT_ENABLED = "not_enabled"
REJECT_RATE_LIMITED = "rate_limited"

IDENTITY_REJECT_REASONS = frozenset(
    {
        REJECT_MALFORMED,
        REJECT_ALGORITHM,
        REJECT_SIGNATURE,
        REJECT_CLAIMS,
        REJECT_EXPIRED,
        REJECT_NOT_YET_VALID,
        REJECT_TTL_TOO_LONG,
        REJECT_ISSUER,
        REJECT_AUDIENCE,
        REJECT_REPLAYED,
        REJECT_NO_VERIFIER,
        REJECT_REPLAY_UNAVAILABLE,
        REJECT_NOT_ENABLED,
        REJECT_RATE_LIMITED,
    }
)


class IdentityRejected(Exception):  # noqa: N818 - a refusal, not an error condition
    """A token was not accepted. Carries a reason, never the token.

    Not an `AppError`: an unverified visitor is not a failed request. The caller
    mints an anonymous session and reports the reason in the response, which is
    what stops a tenant's broken token minting from producing a widget that will
    not load.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class VerifierSpec:
    """One `identity_verifiers` row, as the verifier reads it.

    `secret_ref` is carried for logging and rotation bookkeeping only; the
    plaintext secret is passed alongside this object and never stored on it.
    """

    id: UUID
    tenant_id: UUID
    agent_id: UUID | None = None
    alg: str = IDENTITY_ALG
    secret_ref: str = ""
    issuer: str | None = None
    audience: str | None = None
    max_ttl_seconds: int = 300
    attribute_allowlist: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """The result of a successful verification. Every field is signed.

    `attributes` is already filtered through the verifier's allowlist, so a
    tenant that widened its token cannot widen what we store without also
    widening the row.
    """

    verifier_id: UUID
    subject: str
    jti: str
    issued_at: int
    expires_at: int
    email: str | None = None
    name: str | None = None
    phone: str | None = None
    attributes: dict[str, str] | None = None

    def claims(self) -> dict[str, str]:
        """The `{{user.*}}` namespace: what the tenant asserted this session."""
        asserted: dict[str, str] = {"external_id": self.subject}
        if self.email:
            asserted["email"] = self.email
        if self.name:
            asserted["name"] = self.name
        if self.phone:
            asserted["phone"] = self.phone
        for key, value in (self.attributes or {}).items():
            asserted.setdefault(key, value)
        return asserted


def _split_segments(token: str) -> tuple[str, str, str]:
    """Three non-empty dot-separated segments, or reject. Nothing is parsed here."""
    if not token or len(token) > MAX_TOKEN_CHARS:
        raise IdentityRejected(REJECT_MALFORMED, "token length")
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise IdentityRejected(REJECT_MALFORMED, "segment count")
    return parts[0], parts[1], parts[2]


def _decode_object(segment: str, reason: str) -> dict[str, Any]:
    try:
        decoded = json.loads(_b64url_decode(segment))
    except (ValueError, TypeError) as exc:
        raise IdentityRejected(reason, "not base64url json") from exc
    if not isinstance(decoded, dict):
        raise IdentityRejected(reason, "not an object")
    return decoded


def check_header(segment: str) -> dict[str, Any]:
    """The algorithm allowlist, checked before anything else is parsed.

    `alg` must be exactly ``"HS256"``. An absent `alg`, `alg: "none"`, and every
    asymmetric algorithm all fail this one comparison, which is why there is no
    separate branch for any of them. A `crit` header is rejected outright: it
    asserts an extension we do not implement, and honouring a token that says
    "you must understand this" without understanding it is the bug.
    """
    header = _decode_object(segment, REJECT_MALFORMED)
    if header.get("alg") != IDENTITY_ALG:
        raise IdentityRejected(REJECT_ALGORITHM, "alg is not HS256")
    typ = header.get("typ")
    if typ is not None and str(typ).upper() != "JWT":
        raise IdentityRejected(REJECT_MALFORMED, "typ is not JWT")
    if "crit" in header:
        raise IdentityRejected(REJECT_MALFORMED, "crit header")
    return header


def _signature_matches(signing_input: str, provided: str, secret: str) -> bool:
    """Constant-time signature check. Never `==`."""
    expected = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return hmac.compare_digest(_b64url_encode(expected), provided)


def _require_int(payload: dict[str, Any], claim: str) -> int:
    value = payload.get(claim)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise IdentityRejected(REJECT_CLAIMS, f"{claim} missing or not numeric")
    return int(value)


def _bounded_string(payload: dict[str, Any], claim: str, limit: int) -> str | None:
    value = payload.get(claim)
    if value is None:
        return None
    if not isinstance(value, str):
        raise IdentityRejected(REJECT_CLAIMS, f"{claim} is not a string")
    trimmed = value.strip()
    if not trimmed:
        return None
    if len(trimmed) > limit:
        raise IdentityRejected(REJECT_CLAIMS, f"{claim} is too long")
    return trimmed


def _check_audience(
    payload: dict[str, Any], verifier: VerifierSpec, fallbacks: Sequence[str]
) -> None:
    """`aud` must match the verifier row, or the tenant slug.

    A token minted for one property must not verify on another, which is the
    reason there is no "audience unset means anything goes" path: when the row
    names no audience the tenant slug stands in.
    """
    raw = payload.get("aud")
    if isinstance(raw, str):
        presented = [raw]
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        presented = [str(item) for item in raw]
    elif raw is None:
        presented = []
    else:
        raise IdentityRejected(REJECT_CLAIMS, "aud is not a string or array")

    expected = [verifier.audience] if verifier.audience else [f for f in fallbacks if f]
    if not expected:
        # Nothing to match against would make `aud` decorative. A verifier with
        # no audience, used by a caller that cannot name the tenant slug, is a
        # misconfiguration rather than an open door.
        raise IdentityRejected(REJECT_AUDIENCE, "no expected audience")
    if not any(candidate in presented for candidate in expected):
        raise IdentityRejected(REJECT_AUDIENCE, "aud does not match")


def _check_attributes(
    payload: dict[str, Any], verifier: VerifierSpec, max_attributes: int
) -> dict[str, str] | None:
    """The `attrs` claim: flat, string-valued, bounded, allowlist-filtered.

    Flat and string-only because these values are interpolated into URL paths and
    query strings; a nested object has nothing sensible to render and a long
    value produces a malformed request.
    """
    raw = payload.get("attrs")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise IdentityRejected(REJECT_CLAIMS, "attrs is not an object")
    if len(raw) > max_attributes:
        raise IdentityRejected(REJECT_CLAIMS, "attrs has too many keys")

    allowlist = set(verifier.attribute_allowlist or ())
    filtered: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key)
        if not isinstance(value, str):
            raise IdentityRejected(REJECT_CLAIMS, "attrs values must be strings")
        if len(value) > MAX_ATTRIBUTE_VALUE_CHARS:
            raise IdentityRejected(REJECT_CLAIMS, "attrs value is too long")
        if allowlist and name not in allowlist:
            # Filtered, not rejected: the tenant widening its token must not break
            # a working integration, it must simply not widen what we store.
            continue
        filtered[name] = value
    return filtered or None


def check_claims(
    payload: dict[str, Any],
    verifier: VerifierSpec,
    *,
    now: int,
    clock_skew_seconds: int,
    max_ttl_seconds: int,
    audiences: Sequence[str],
    max_attributes: int,
) -> VerifiedIdentity:
    """Every claim rule, on an already signature-verified payload.

    `exp - iat` is capped because a long-lived bearer token sitting in browser
    JavaScript is the thing that cap exists to prevent: the signature proves the
    tenant minted it, not that the person holding it is still the person it was
    minted for.
    """
    skew = max(0, clock_skew_seconds)

    subject = _bounded_string(payload, "sub", MAX_SUB_CHARS)
    if not subject:
        raise IdentityRejected(REJECT_CLAIMS, "sub missing")
    jti = _bounded_string(payload, "jti", MAX_JTI_CHARS)
    if not jti:
        raise IdentityRejected(REJECT_CLAIMS, "jti missing")

    issued_at = _require_int(payload, "iat")
    expires_at = _require_int(payload, "exp")

    if issued_at > now + skew:
        raise IdentityRejected(REJECT_CLAIMS, "iat is in the future")
    if expires_at <= now - skew:
        raise IdentityRejected(REJECT_EXPIRED, "exp has passed")

    ttl_ceiling = min(max_ttl_seconds, verifier.max_ttl_seconds)
    if expires_at - issued_at > ttl_ceiling:
        raise IdentityRejected(REJECT_TTL_TOO_LONG, "exp - iat exceeds the ceiling")

    not_before = payload.get("nbf")
    if not_before is not None:
        if isinstance(not_before, bool) or not isinstance(not_before, int | float):
            raise IdentityRejected(REJECT_CLAIMS, "nbf is not numeric")
        if int(not_before) > now + skew:
            raise IdentityRejected(REJECT_NOT_YET_VALID, "nbf is in the future")

    # The issuer check. A token must always name an issuer — a missing `iss` is a
    # rejection even when the verifier row leaves the expected value unset, so no
    # configuration can turn this check off. When the row does name one, the match
    # is exact.
    claimed_issuer = _bounded_string(payload, "iss", MAX_ISSUER_CHARS)
    if not claimed_issuer:
        raise IdentityRejected(REJECT_ISSUER, "iss missing")
    if verifier.issuer and claimed_issuer != verifier.issuer:
        raise IdentityRejected(REJECT_ISSUER, "iss does not match")

    _check_audience(payload, verifier, audiences)

    return VerifiedIdentity(
        verifier_id=verifier.id,
        subject=subject,
        jti=jti,
        issued_at=issued_at,
        expires_at=expires_at,
        email=_bounded_string(payload, "email", MAX_EMAIL_CHARS),
        name=_bounded_string(payload, "name", MAX_NAME_CHARS),
        phone=_bounded_string(payload, "phone", MAX_PHONE_CHARS),
        attributes=_check_attributes(payload, verifier, max_attributes),
    )


def select_candidates(
    header: dict[str, Any], candidates: Sequence[tuple[VerifierSpec, str]]
) -> list[tuple[VerifierSpec, str]]:
    """Narrow the rotation set by `kid`, if the token carries one.

    `kid` is only ever used to pick between the tenant's own rows. It never
    causes a fetch of anything, remote or local, so a forged `kid` can at worst
    select a row the same tenant already owns.
    """
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        return list(candidates)
    matched = [pair for pair in candidates if str(pair[0].id) == kid]
    return matched or list(candidates)


def verify_identity_token(
    token: str,
    *,
    candidates: Sequence[tuple[VerifierSpec, str]],
    audiences: Sequence[str] = (),
    now: int | None = None,
    clock_skew_seconds: int = 60,
    max_ttl_seconds: int = 300,
    max_attributes: int = 20,
) -> VerifiedIdentity:
    """Verify one identity token against a tenant's active verifier rows.

    `candidates` is `(row, plaintext secret)` in `created_at` order, at most two
    of them, which is what lets a secret rotate without a cutover. The signature
    of every candidate is checked before any payload is decoded, so a token that
    matches none of them never has a claim read out of it.

    Raises `IdentityRejected`. Replay is not checked here — see
    `ContactsRepo.claim_jti`, which needs the cache and the database.
    """
    if not candidates:
        raise IdentityRejected(REJECT_NO_VERIFIER, "no active verifier")

    header_segment, payload_segment, signature = _split_segments(token)
    header = check_header(header_segment)
    signing_input = f"{header_segment}.{payload_segment}"
    moment = int(time()) if now is None else now

    for verifier, secret in select_candidates(header, candidates):
        if not secret or not _signature_matches(signing_input, signature, secret):
            continue
        payload = _decode_object(payload_segment, REJECT_MALFORMED)
        return check_claims(
            payload,
            verifier,
            now=moment,
            clock_skew_seconds=clock_skew_seconds,
            max_ttl_seconds=max_ttl_seconds,
            audiences=audiences,
            max_attributes=max_attributes,
        )

    raise IdentityRejected(REJECT_SIGNATURE, "no verifier secret matched")


def jti_ledger_ttl(identity: VerifiedIdentity, *, now: int, clock_skew_seconds: int) -> int:
    """How long a `jti` must stay recorded: its remaining life plus the skew.

    Never zero. A token already inside the skew window still has to be
    unrepeatable, or the last second of its life is a free replay.
    """
    return max(1, identity.expires_at - now + max(0, clock_skew_seconds))

