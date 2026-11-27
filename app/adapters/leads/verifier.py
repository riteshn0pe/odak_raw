"""Email verification, behind one port.

ZeroBounce is the default vendor — it separates a catch-all domain from a real
mailbox, which is the distinction that matters most in the Indian SME market
where a large share of company domains accept everything — and NeverBounce sits
behind the same port for the deployments that already have credits there. The
port is what makes that a one-line container change rather than a rewrite.

**Never raises.** A verifier outage returns ``risky``, and ``risky`` is
sendable. The alternative — treating "we could not check" as "do not send" —
would stop every tenant's outbound the moment the vendor had a bad afternoon,
and it would look from the outside like a quiet day rather than an outage.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.breaker import guard
from app.core.errors import CircuitOpenError, ProviderUnavailableError
from app.core.logging import get_logger
from app.domain.ports import VerificationResult

logger = get_logger(__name__)

ZEROBOUNCE_URL = "https://api.zerobounce.net/v2/validate"
NEVERBOUNCE_URL = "https://api.neverbounce.com/v4/single/check"

# Both vendors report a dozen sub-states. Anything not named here is `risky`,
# which is the honest default for a verdict we do not have a rule for: it is
# sendable, and it is visible in the panel as something less than confirmed.
ZEROBOUNCE_STATUS: dict[str, str] = {
    "valid": "deliverable",
    "invalid": "undeliverable",
    "spamtrap": "undeliverable",
    "abuse": "undeliverable",
    "do_not_mail": "undeliverable",
    "catch-all": "risky",
    "unknown": "risky",
}
NEVERBOUNCE_STATUS: dict[str, str] = {
    "valid": "deliverable",
    "invalid": "undeliverable",
    "disposable": "undeliverable",
    "catchall": "risky",
    "unknown": "risky",
}


class _Verifier:
    """Shared transport. The two vendors differ only in URL and vocabulary."""

    def __init__(self, api_key: str, timeout: float, provider: str) -> None:
        self._api_key = api_key
        self._provider = provider
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def name(self) -> str:
        return self._provider

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        try:
            with guard(self._provider):
                try:
                    response = await self._client.get(url, params=params)
                    if response.status_code != 200:
                        raise ProviderUnavailableError(
                            self._provider, f"http {response.status_code}"
                        )
                    body = response.json()
                    return dict(body) if isinstance(body, dict) else None
                except (httpx.HTTPError, ValueError) as exc:
                    raise ProviderUnavailableError(self._provider, str(exc)) from exc
        except CircuitOpenError:
            logger.info("email_verifier_circuit_open", provider=self._provider)
            return None
        except ProviderUnavailableError as exc:
            logger.warning(
                "email_verifier_failed", provider=self._provider, error=str(exc)
            )
            return None

    async def close(self) -> None:
        await self._client.aclose()


class ZeroBounceVerifier(_Verifier):
    def __init__(self, api_key: str, timeout: float = 12.0) -> None:
        super().__init__(api_key, timeout, "zerobounce")

    async def verify(self, email: str) -> VerificationResult:
        email = email.strip().lower()
        if not email:
            return VerificationResult(address=email, status="undeliverable", detail="empty")
        payload = await self._get(
            ZEROBOUNCE_URL, {"api_key": self._api_key, "email": email, "ip_address": ""}
        )
        if payload is None:
            return VerificationResult(address=email, status="risky", detail="unreachable")
        raw = str(payload.get("status") or "").strip().lower()
        return VerificationResult(
            address=email,
            status=ZEROBOUNCE_STATUS.get(raw, "risky"),
            detail=raw or str(payload.get("sub_status") or ""),
        )


class NeverBounceVerifier(_Verifier):
    def __init__(self, api_key: str, timeout: float = 12.0) -> None:
        super().__init__(api_key, timeout, "neverbounce")

    async def verify(self, email: str) -> VerificationResult:
        email = email.strip().lower()
        if not email:
            return VerificationResult(address=email, status="undeliverable", detail="empty")
        payload = await self._get(
            NEVERBOUNCE_URL, {"key": self._api_key, "email": email}
        )
        if payload is None:
            return VerificationResult(address=email, status="risky", detail="unreachable")
        raw = str(payload.get("result") or "").strip().lower()
        return VerificationResult(
            address=email,
            status=NEVERBOUNCE_STATUS.get(raw, "risky"),
            detail=raw,
        )


def build_verifier(
    backend: str, api_key: str, timeout: float = 12.0
) -> ZeroBounceVerifier | NeverBounceVerifier | None:
    """The configured verifier, or None.

    None is the ordinary unconfigured state: research still finds and writes
    contacts, they are simply marked unverified, and the sender treats an
    unverified address the way it treats a risky one — sendable, and visibly
    less than confirmed.
    """
    backend = (backend or "").strip().lower()
    if backend in ("", "none") or not api_key:
        return None
    if backend == "zerobounce":
        return ZeroBounceVerifier(api_key, timeout=timeout)
    if backend == "neverbounce":
        return NeverBounceVerifier(api_key, timeout=timeout)
    logger.warning("email_verifier_unknown_backend", backend=backend)
    return None
