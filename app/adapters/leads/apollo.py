"""Apollo.io — company search and contact enrichment.

Apollo is the broadest of the B2B databases for the markets this platform sells
into, and the one whose company search actually understands an ICP sentence
rather than needing a boolean query. It is also metered per credit, which is why
every method here reports what it burned and the *service* writes the ledger
row: an adapter that meters itself is an adapter that will one day be called
from a test and bill a tenant.

Failure is empty, never an exception. Web-only research is the base case; an
Apollo outage has to degrade to it rather than fail the run.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.breaker import guard
from app.core.errors import CircuitOpenError, ProviderUnavailableError
from app.core.logging import get_logger
from app.domain.ports import LeadCandidate

logger = get_logger(__name__)

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"
PROVIDER = "apollo"

# Apollo will happily return 200 for a page far larger than anyone should ask
# for. The cap is here rather than at the call site because the credit cost is
# per record returned, and a caller's typo must not be able to spend a tenant's
# month of credits in one request.
MAX_PAGE = 100

# Roles worth mailing, in preference order. Apollo's `person_titles` filter is a
# contains-match, so these are stems rather than exact titles.
DEFAULT_TITLES: tuple[str, ...] = (
    "founder",
    "owner",
    "director",
    "head",
    "manager",
    "purchase",
    "procurement",
)


class ApolloLeads:
    """The ``LeadDataProvider`` port, over Apollo's REST API."""

    def __init__(self, api_key: str, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "X-Api-Key": api_key,
            },
        )

    @property
    def name(self) -> str:
        return PROVIDER

    async def search_companies(
        self, query: str, *, limit: int = 25, location: str = "", industry: str = ""
    ) -> list[LeadCandidate]:
        """Companies matching an ICP sentence.

        The ICP text goes in as a keyword query rather than being decomposed
        into Apollo's structured filters. Decomposing it would mean an LLM call
        per search to turn "small textile exporters in Surat" into Apollo's
        taxonomy, and Apollo's own keyword matching already does that job for
        free — with the honest failure mode of returning fewer, rather than
        wrong, companies.
        """
        query = query.strip()
        if not query:
            return []

        body: dict[str, Any] = {
            "q_organization_keyword_tags": [query],
            "page": 1,
            "per_page": max(1, min(int(limit), MAX_PAGE)),
        }
        if location:
            body["organization_locations"] = [location]
        if industry:
            body["q_organization_keyword_tags"] = [query, industry]

        payload = await self._post("/mixed_companies/search", body)
        if payload is None:
            return []

        raw = payload.get("organizations") or payload.get("accounts") or []
        out: list[LeadCandidate] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            candidate = _company(item)
            if candidate is not None:
                out.append(candidate)
        return out

    async def enrich_contact(
        self, *, domain: str, company: str = "", role: str = ""
    ) -> LeadCandidate | None:
        """One named human at that company, or None.

        One person, not a list, and that is a cost decision as much as a design
        one: cold outreach to five people at the same small company is how a
        sending domain earns a spam complaint, and each extra record is another
        credit. The research agent asks again for a different role if it wants
        one.
        """
        domain = domain.strip().lower()
        if not domain:
            return None

        titles = [role] if role else list(DEFAULT_TITLES)
        payload = await self._post(
            "/mixed_people/search",
            {
                "q_organization_domains": domain,
                "person_titles": titles,
                "page": 1,
                "per_page": 1,
            },
        )
        if payload is None:
            return None

        people = payload.get("people") or payload.get("contacts") or []
        if not isinstance(people, list) or not people:
            return None
        first = people[0]
        if not isinstance(first, dict):
            return None
        return _person(first, domain=domain, company=company)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any] | None:
        """One guarded call. Returns None on every failure, having logged it.

        The breaker sees the raised error so a sustained Apollo outage stops
        costing latency after a few failures; the None is what keeps the outage
        from reaching the research run.
        """
        try:
            with guard(PROVIDER):
                try:
                    response = await self._client.post(
                        f"{APOLLO_BASE_URL}{path}", json={**body, "api_key": self._api_key}
                    )
                    if response.status_code != 200:
                        raise ProviderUnavailableError(
                            PROVIDER, f"http {response.status_code}"
                        )
                    data = response.json()
                    return dict(data) if isinstance(data, dict) else None
                except (httpx.HTTPError, ValueError) as exc:
                    raise ProviderUnavailableError(PROVIDER, str(exc)) from exc
        except CircuitOpenError:
            logger.info("apollo_circuit_open", path=path)
            return None
        except ProviderUnavailableError as exc:
            logger.warning("apollo_call_failed", path=path, error=str(exc))
            return None

    async def close(self) -> None:
        await self._client.aclose()


def _company(item: dict[str, Any]) -> LeadCandidate | None:
    """One organization record, or None when it carries nothing identifying."""
    domain = str(item.get("primary_domain") or item.get("website_url") or "").strip()
    company = str(item.get("name") or "").strip()
    if not domain and not company:
        return None
    size = item.get("estimated_num_employees")
    return LeadCandidate(
        company=company,
        domain=domain,
        location=_location(item),
        size=str(size) if size else "",
        description=str(item.get("short_description") or "")[:1000],
        source=PROVIDER,
        raw=item,
    )


def _person(item: dict[str, Any], *, domain: str, company: str) -> LeadCandidate:
    org = item.get("organization")
    org = org if isinstance(org, dict) else {}
    name = str(item.get("name") or "").strip() or " ".join(
        part
        for part in (str(item.get("first_name") or ""), str(item.get("last_name") or ""))
        if part
    ).strip()
    return LeadCandidate(
        company=company or str(org.get("name") or "").strip(),
        domain=domain or str(org.get("primary_domain") or "").strip(),
        person=name,
        role=str(item.get("title") or "").strip(),
        # Apollo returns the literal string "email_not_unlocked@domain.com" for a
        # record whose email costs a further credit. Treating that as an address
        # would mail a placeholder, so it is dropped here rather than at the
        # sender, where it would look like a real bounce
        email=_email(item.get("email")),
        phone=str(item.get("sanitized_phone") or item.get("phone_number") or "").strip(),
        location=_location(item),
        source=PROVIDER,
        raw=item,
    )


def _email(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text or "not_unlocked" in text or text.startswith("email_not"):
        return ""
    return text


def _location(item: dict[str, Any]) -> str:
    parts = [
        str(item.get(key) or "").strip()
        for key in ("city", "state", "country")
    ]
    return ", ".join(part for part in parts if part)


def build_apollo(api_key: str, timeout: float = 15.0) -> ApolloLeads | None:
    """Apollo, or None when no key is configured."""
    if not api_key:
        return None
    return ApolloLeads(api_key, timeout=timeout)
