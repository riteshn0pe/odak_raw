"""Ingress keys: the credential a tenant pastes into somebody else's form stack.

The same reasoning as ``optout.py``, for the same reason. A random key looked up
in a table would need a cross-tenant read to answer "whose key is this", and
every growth table carries FORCE RLS — so that query matches nothing under
``admin_scope``. The tenant has to be *inside* the credential.

So an ingress key is an HMAC-signed statement: "this tenant, this provider".
Verifying the signature is what resolves the tenant, and there is no database
read before authentication.

Two properties worth stating, because both are load-bearing:

* **The key names its provider.** A website-form key must not be a valid
  IndiaMART key the day IndiaMART is added, so the provider is signed in and
  compared.
* **The key is not the switch.** Revocation is the ``growth_integrations`` row:
  a key stays cryptographically valid forever, and the ingress refuses it the
  moment the tenant sets that provider to anything but ``active``. Rotation is
  therefore "disconnect, reconnect", which is a button a tenant already has.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.errors import AuthenticationError
from app.core.security import sign_token, verify_token

AUDIENCE = "growth_ingress"

# Ten years. A key lives in someone else's form configuration, or in a script
# tag on a page nobody has edited since it was deployed, and an expiring key
# means inbound leads stop arriving silently on a Tuesday two years from now.
# The integration row is the revocation mechanism; expiry is not.
TTL_SECONDS = 10 * 365 * 24 * 3600


def mint_ingress_key(secret: str, tenant_id: UUID, provider: str) -> str:
    """A key for one tenant and one inbound provider."""
    return sign_token(
        {"aud": AUDIENCE, "t": str(tenant_id), "p": provider},
        secret,
        TTL_SECONDS,
    )


def parse_ingress_key(secret: str, key: str) -> tuple[UUID, str]:
    """The tenant and provider a key names. Raises on anything else."""
    if not key:
        raise AuthenticationError("missing ingress key")
    payload = verify_token(key, secret)
    if str(payload.get("aud") or "") != AUDIENCE:
        # a dashboard token or a widget session token is signed with the same
        # key; the audience is what stops either being replayed here
        raise AuthenticationError("this is not an ingress key")
    provider = str(payload.get("p") or "").strip()
    raw_tenant = str(payload.get("t") or "")
    if not provider or not raw_tenant:
        raise AuthenticationError("malformed ingress key")
    try:
        tenant_id = UUID(raw_tenant)
    except ValueError as exc:
        raise AuthenticationError("malformed ingress key") from exc
    return tenant_id, provider


# Provider mode's key: the same minting pattern, naming the platform bot
# rather than a tenant. A tenant key signs ``t``/``p`` and never ``sub``, and a
# platform key signs ``sub`` and never ``t``/``p`` — so neither population can
# be replayed against the other's door, whatever else shares the secret.
PLATFORM_SUBJECT = "telegram:platform"


def mint_platform_ingress_key(secret: str) -> str:
    """The platform bot's key: names the bot, names no tenant."""
    return sign_token({"aud": AUDIENCE, "sub": PLATFORM_SUBJECT}, secret, TTL_SECONDS)


def parse_platform_ingress_key(secret: str, key: str) -> str:
    """The subject a platform key names. Raises on anything else.

    Telegram is the only provider with a platform key today, so the subject is
    a constant compared by the caller rather than a routing fact — a key whose
    subject is not this door's is refused exactly like a key issued for
    another source.
    """
    if not key:
        raise AuthenticationError("missing ingress key")
    payload = verify_token(key, secret)
    if str(payload.get("aud") or "") != AUDIENCE:
        raise AuthenticationError("this is not an ingress key")
    subject = str(payload.get("sub") or "").strip()
    if not subject:
        # a tenant key or a dashboard token: signed right, wrong shape
        raise AuthenticationError("this is not a platform ingress key")
    return subject


def platform_ingress_url_with_key(settings: Any, provider: str) -> str:
    """The platform bot's webhook URL — the owner's one-time setWebhook target.

    This is the one credential in a URL that is not a tenant's: the platform
    owner sets it once against BotFather (a bot takes exactly one webhook and
    no custom headers, so the query string is the only form there is), and
    tenants never see or paste it — they connect by messaging the bot.
    """
    key = mint_platform_ingress_key(settings.SECRET_KEY)
    return f"{ingress_url(settings, provider)}?key={key}"


def ingress_url(settings: Any, provider: str) -> str:
    """The webhook endpoint, with no credential in it.

    The preferred form. The key travels in the ``x-ingress-key`` header, or in
    the body as ``key`` — both of which stay out of access logs, proxy logs,
    ``Referer`` headers and browser history, which a URL does not.
    """
    base = str(settings.PUBLIC_BASE_URL or "").rstrip("/")
    return f"{base}/webhooks/ingress/{provider}"


def ingress_url_with_key(settings: Any, tenant_id: UUID, provider: str) -> str:
    """The same endpoint with the key in the query string. A fallback, and only that.

    Some form builders accept a URL and nothing else — no headers, no control
    over the body — and for those tenants this is the difference between the
    feature working and not. But a credential in a URL is written into every log
    on its path, so this is offered second, labelled as a fallback, and its use
    is counted at the ingress.
    """
    key = mint_ingress_key(settings.SECRET_KEY, tenant_id, provider)
    return f"{ingress_url(settings, provider)}?key={key}"
