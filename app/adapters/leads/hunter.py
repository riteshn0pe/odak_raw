"""Hunter.io — domain search and email-pattern discovery.

Hunter answers a narrower question than Apollo and answers it better: *given a
company's website, what do their email addresses look like, and who is behind
them*. It has no company search worth the name, so ``search_companies`` here
returns empty rather than pretending — the port allows that, and a vendor that
fakes a capability is worse than one that declines it.

The pattern is the real value. ``{first}.{last}@domain`` recovered from two
known addresses lets the research agent construct a plausible address for a
named person it found on a company's own site, which is then put through the
verifier before anything is sent. That chain — name from the site, shape from
Hunter, deliverability from the verifier — is how a contact gets found without
buying a record.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.breaker import guard
from app.core.errors import CircuitOpenError, ProviderUnavailableError
from app.core.logging import get_logger
from app.domain.ports import LeadCandidate

logger = get_logger(__name__)

HUNTER_BASE_URL = "https://api.hunter.io/v2"
PROVIDER = "hunter"

# Hunter charges a request per lookup regardless of how many addresses come
# back, so asking for one is the same price as asking for ten — but ten
# addresses at one small company is a spam complaint, not a lead.
DOMAIN_SEARCH_LIMIT = 5

# Confidence below this is Hunter guessing from a pattern it is unsure of. Those
# come back as candidates with no email rather than being dropped, so the
# research agent still gets the person's name and the company context.
MIN_CONFIDENCE = 70


class HunterLeads:
    """The ``LeadDataProvider`` port, over Hunter's REST API."""

    def __init__(self, api_key: str, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def name(self) -> str:
        return PROVIDER

    async def search_companies(
        self, query: str, *, limit: int = 25, location: str = "", industry: str = ""
    ) -> list[LeadCandidate]:
        """Not a capability Hunter has. Empty, deliberately and always.

        The alternative — running the ICP text through Hunter's domain search as
        if it were a domain — would return nothing while spending a request, and
        would look like a company search that found no companies rather than a
        capability that does not exist.
        """
        return []

    async def enrich_contact(
        self, *, domain: str, company: str = "", role: str = ""
    ) -> LeadCandidate | None:
        """The best-confidence named contact at that domain, or None."""
        domain = domain.strip().lower()
        if not domain:
            return None

        payload = await self._get(
            "/domain-search",
            {"domain": domain, "limit": DOMAIN_SEARCH_LIMIT, "type": "personal"},
        )
        if payload is None:
            return None

        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        emails = data.get("emails")
        emails = emails if isinstance(emails, list) else []

        best: dict[str, Any] | None = None
        for item in emails:
            if not isinstance(item, dict):
                continue
            if role and role.lower() not in str(item.get("position") or "").lower():
                continue
            if best is None or _confidence(item) > _confidence(best):
                best = item
        if best is None:
            return None

        name = " ".join(
            part
            for part in (
                str(best.get("first_name") or "").strip(),
                str(best.get("last_name") or "").strip(),
            )
            if part
        )
        return LeadCandidate(
            company=company or str(data.get("organization") or "").strip(),
            domain=domain,
            person=name,
            role=str(best.get("position") or "").strip(),
            # below the confidence floor Hunter is extrapolating from a pattern
            # it is unsure of; the name and role are still worth keeping, the
            # address is not worth a send
            email=(
                str(best.get("value") or "").strip().lower()
                if _confidence(best) >= MIN_CONFIDENCE
                else ""
            ),
            phone=str(best.get("phone_number") or "").strip(),
            source=PROVIDER,
            raw={"pattern": data.get("pattern"), "email": best},
        )

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """One guarded call. None on every failure, having logged it."""
        try:
            with guard(PROVIDER):
                try:
                    response = await self._client.get(
                        f"{HUNTER_BASE_URL}{path}",
                        params={**params, "api_key": self._api_key},
                    )
                    if response.status_code != 200:
                        raise ProviderUnavailableError(
                            PROVIDER, f"http {response.status_code}"
                        )
                    body = response.json()
                    return dict(body) if isinstance(body, dict) else None
                except (httpx.HTTPError, ValueError) as exc:
                    raise ProviderUnavailableError(PROVIDER, str(exc)) from exc
        except CircuitOpenError:
            logger.info("hunter_circuit_open", path=path)
            return None
        except ProviderUnavailableError as exc:
            logger.warning("hunter_call_failed", path=path, error=str(exc))
            return None

    async def close(self) -> None:
        await self._client.aclose()


def _confidence(item: dict[str, Any]) -> int:
    try:
        return int(item.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0


def build_hunter(api_key: str, timeout: float = 15.0) -> HunterLeads | None:
    """Hunter, or None when no key is configured."""
    if not api_key:
        return None
    return HunterLeads(api_key, timeout=timeout)
