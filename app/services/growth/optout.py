"""Unsubscribe tokens: the link in every outbound mail, and what it proves.

A token, not a database row. The alternative — a random id looked up in a table
— would need a cross-tenant read to resolve *which* tenant an unsubscribe
belongs to, and every growth table carries FORCE RLS, so that lookup matches
nothing under ``admin_scope``. The tenant has to be inside the credential.

So the link carries a signed statement — "this address, for this tenant" —
signed with the platform's HMAC key. Forging one means forging the signature;
reading one tells an attacker an address they already had, because they were
sent the mail.

Two verbs, deliberately:

* ``GET`` renders a confirmation page and changes nothing. A GET that
  unsubscribes is a URL that a mail scanner, a link-preview fetcher or a
  browser's speculative prefetch will eventually press on the recipient's
  behalf — the same reasoning that keeps the approval decision on a POST.
* ``POST`` performs it, which is also what RFC 8058 one-click asks for, so the
  recipient's mail client can offer its native unsubscribe button.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.errors import AuthenticationError
from app.core.security import sign_token, verify_token

# The token's audience. Present so a widget-session token or a dashboard token
# can never be replayed here, even though they are signed with the same key.
AUDIENCE = "growth_unsubscribe"

# Ten years. An unsubscribe link has to work in a mail somebody archived and
# comes back to; a link that expires is a link that turns into a spam report.
TTL_SECONDS = 10 * 365 * 24 * 3600


def mint(secret: str, tenant_id: UUID, address: str) -> str:
    """A signed unsubscribe credential for one address at one tenant."""
    return sign_token(
        {"aud": AUDIENCE, "t": str(tenant_id), "a": address},
        secret,
        TTL_SECONDS,
    )


def parse(secret: str, token: str) -> tuple[UUID, str]:
    """The tenant and address a token names. Raises on anything else."""
    payload = verify_token(token, secret)
    if str(payload.get("aud") or "") != AUDIENCE:
        raise AuthenticationError("this link is not an unsubscribe link")
    address = str(payload.get("a") or "").strip()
    raw_tenant = str(payload.get("t") or "")
    if not address or not raw_tenant:
        raise AuthenticationError("malformed unsubscribe link")
    try:
        tenant_id = UUID(raw_tenant)
    except ValueError as exc:
        raise AuthenticationError("malformed unsubscribe link") from exc
    return tenant_id, address


def unsubscribe_url(settings: Any, tenant_id: UUID, address: str) -> str:
    """The absolute link that goes in the mail body and the List-Unsubscribe header."""
    base = str(settings.PUBLIC_BASE_URL or "").rstrip("/")
    token = mint(settings.SECRET_KEY, tenant_id, address)
    return f"{base}/growth/unsubscribe/{token}"
