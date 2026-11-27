"""The OAuth blob every CRM adapter stores, and the errors they raise.

Both CRMs here are refresh-token OAuth apps with short-lived access tokens —
Zoho's lasts an hour, HubSpot's half an hour — so "the credential" is never a
single string. It is an access token, the refresh token that mints the next
one, the account's own API host (Zoho hands you a different one per data
centre) and an expiry the caller can check without making a request.

That whole blob is one JSON document in ``tenant_secrets``, encrypted like
every other credential, because the alternative is four plaintext columns on
``growth_integrations`` and a refresh token in a plaintext column is a
permanent grant to somebody's CRM sitting in a table an analytics query might
one day select ``*`` from.

``CrmAuthError`` exists so the push loop can tell "your connection is dead,
reconnect it" from "that one contact was rejected". They land in different
places: the first is the integration card going red for the whole tenant, the
second is one link row going red on the leads screen. Collapsing them would
either hide a revoked token behind one bad lead or declare the whole CRM broken
because somebody's email address had a space in it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import ProviderError


class CrmAuthError(ProviderError):
    """The connection itself is broken: revoked token, missing scope, wrong app.

    Retrying does not help and the tenant has to reconnect, so the push loop
    stops on this rather than counting it as a per-lead failure.
    """

    code = "crm_auth_error"


class CrmRecordGoneError(ProviderError):
    """The record we hold an id for no longer exists in the tenant's CRM.

    The tenant deleted it in their own CRM (cleanup, GDPR). Retrying the same
    id fails the same way forever, so the push loop answers this by dropping
    the stored id and queueing the recreate — which adopts by email rather
    than duplicating — instead of counting it as a failure.
    """

    code = "crm_record_gone"


def challenge_for(verifier: str) -> str:
    """The S256 ``code_challenge`` for a PKCE verifier (RFC 7636).

    Shared by both adapters because the computation is provider-independent
    and a second copy is how the two would drift on the one part that must
    not drift — the padding-stripped base64url digest.
    """
    import base64
    import hashlib

    digest = hashlib.sha256((verifier or "").encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# Refresh this many seconds before the token actually expires. A token that
# expires mid-flight is a 401 the caller cannot distinguish from a revoked
# grant, and a minute of slack costs nothing.
EXPIRY_SKEW_SECONDS = 120


@dataclass(slots=True)
class CrmTokens:
    """One tenant's OAuth grant on one CRM."""

    access_token: str = ""
    refresh_token: str = ""
    api_domain: str = ""
    # absolute unix seconds, not a duration: a duration stored at connect time
    # is already wrong by the time anyone reads it
    expires_at: float = 0.0
    account: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        """True when the access token is gone or close enough to gone."""
        if not self.access_token:
            return True
        if not self.expires_at:
            # a grant stored without an expiry — refresh rather than gamble
            return True
        return time.time() >= self.expires_at - EXPIRY_SKEW_SECONDS


def pack(tokens: CrmTokens) -> str:
    """The blob to encrypt into ``tenant_secrets``."""
    return json.dumps(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "api_domain": tokens.api_domain,
            "expires_at": tokens.expires_at,
            "account": tokens.account,
            "extra": tokens.extra,
        }
    )


def unpack(packed: str) -> CrmTokens:
    """The blob back out. Unreadable means "not connected", never an error.

    Same posture as ``woocommerce.unpack_secret``: a credential that will not
    parse is indistinguishable to the caller from one that was never stored,
    and raising here would turn an un-reconnected integration into a 500 on
    whatever background job happened to touch it first.
    """
    try:
        body = json.loads(packed or "")
    except ValueError:
        return CrmTokens()
    if not isinstance(body, dict):
        return CrmTokens()
    extra = body.get("extra")
    return CrmTokens(
        access_token=str(body.get("access_token") or ""),
        refresh_token=str(body.get("refresh_token") or ""),
        api_domain=str(body.get("api_domain") or ""),
        expires_at=float(body.get("expires_at") or 0.0),
        account=str(body.get("account") or ""),
        extra=dict(extra) if isinstance(extra, dict) else {},
    )


def expiry_from(expires_in: Any) -> float:
    """An ``expires_in`` duration from a token response into absolute seconds."""
    try:
        seconds = float(expires_in)
    except (TypeError, ValueError):
        return 0.0
    return time.time() + seconds if seconds > 0 else 0.0
