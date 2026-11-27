"""The tenant fact base: every value any agent's prompt template can read.

The facts live in ``growth_settings`` (``profile`` block and siblings, extended
by migration 026's schema work in :mod:`app.services.growth.profile`) — this
module is the read side: it turns the stored blocks into the ``company`` and
``owner`` namespaces the renderer resolves ``{{company.company_name}}`` against,
and declares :data:`FACT_FIELDS`, the closed set of paths a template may use.

The closed set is the point. A template with a typo (``{{company.compny}}``)
must fail at startup validation, not surface as an email that says "we sell
``‹compny›`` to our customers". Unknown path, no render, ever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import UUID

# Every company.* path a template may reference. Kept in step with ProfileBlock
# (app/services/growth/profile.py) plus the two derived entries (`languages`,
# joined from the languages block, and `icp.*`, the three ICP keys the profile
# agent fills). registry.py validates templates against this set at import.
FACT_FIELDS: frozenset[str] = frozenset(
    {
        # identity
        "company_name", "legal_name", "tagline", "industry",
        "city", "region", "country", "website",
        # offer
        "sells", "audience", "price_range", "offer_min", "offer_max",
        "services", "usp", "order_process", "certifications",
        # reach
        "hours", "contact_email", "contact_phone", "whatsapp_number",
        "booking_link", "delivery_areas", "delivery_time",
        "policies.returns", "policies.shipping", "policies.warranty",
        "policies.payment",
        # voice and rules
        "tone", "brand_voice", "avoid", "languages",
        # market
        "keywords", "competitors", "icp.industry", "icp.size", "icp.location",
        # outbound identity (CAN-SPAM / DPDP)
        "postal_address", "sender_name",
        # escalation text (prompt copy only — delivery stays in EscalationConfig)
        "escalation_contact", "escalation_rule",
        # gating
        "tenant_type",
    }
)

# The owner namespace. Two paths, both server-read: the owner never types these
# into a template because a template is authored by the platform and edited by
# the owner, and the renderer only ever reads what the database says.
OWNER_FIELDS: frozenset[str] = frozenset({"name", "email"})

# Namespaces a template may reference at all. company/owner come from the fact
# base; the rest are per-run and supplied by the calling service.
NAMESPACES: frozenset[str] = frozenset(
    {"company", "owner", "runtime", "lead", "customer", "conversation"}
)


@dataclass(slots=True)
class TenantFacts:
    """One tenant's facts, shaped for the renderer.

    ``company`` and ``owner`` are the namespaces ``render`` resolves against.
    ``version`` is the profile's ``facts_version`` — the cache key that makes a
    re-crawl or an owner edit visible to the next render without any explicit
    invalidation fan-out.
    """

    tenant_id: UUID
    company: dict[str, Any] = field(default_factory=dict)
    owner: dict[str, Any] = field(default_factory=dict)
    version: int = 0

    @property
    def namespaces(self) -> dict[str, Any]:
        return {"company": self.company, "owner": self.owner}

    def missing(self, required: tuple[str, ...]) -> list[str]:
        """Which required fact paths have no value, in the order declared."""
        absent: list[str] = []
        for path in required:
            node: Any = self.company
            for part in path.split("."):
                if not isinstance(node, Mapping) or part not in node:
                    node = None
                    break
                node = node[part]
            if node is None or (isinstance(node, str) and not node.strip()) or (
                isinstance(node, (list, tuple)) and not node
            ):
                absent.append(path)
        return absent


def build_company_namespace(settings: Mapping[str, Any]) -> dict[str, Any]:
    """The ``company`` namespace from merged growth settings.

    Pure, so the registry's tests can build a fact base without a database.
    List-valued profile fields are kept as lists — the renderer joins them for
    prompt text, while ``missing()`` still sees an empty list as absent.
    """
    profile = settings.get("profile") or {}
    company: dict[str, Any] = {}

    for key in FACT_FIELDS:
        if key.startswith(("policies.", "icp.")):
            continue
        if key in profile:
            company[key] = profile[key]

    # brand_voice derives from tone when the owner has not described their own
    # voice: tone is the older, always-present preference ("formal" by default)
    # and the templates read brand_voice only, so one derivation keeps both
    # honest without letting a template nest a fallback inside a fallback
    if not str(profile.get("brand_voice") or "").strip() and str(
        profile.get("tone") or ""
    ).strip():
        company["brand_voice"] = profile["tone"]

    # languages: the joined set from the languages block, not a profile field —
    # the block is the authority on what a reply may be written in
    languages = settings.get("languages") or {}
    allowed = languages.get("allowed") or []
    if allowed:
        company["languages"] = allowed

    policies = profile.get("policies")
    if isinstance(policies, Mapping):
        company["policies"] = {
            key: value for key, value in policies.items() if str(value or "").strip()
        }
    icp = profile.get("icp")
    if isinstance(icp, Mapping):
        company["icp"] = {
            key: value for key, value in icp.items() if str(value or "").strip()
        }
    return company
