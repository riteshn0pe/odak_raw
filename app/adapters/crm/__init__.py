"""CRM adapters. One vendor per module, one shared token blob.

The catalogue is deliberately two entries. Salesforce was considered and
rejected in ``boom-boom/implement.md`` §0.5 (its API is gated behind an edition
most of our tenants do not buy, and the ones who do have a Salesforce admin who
would rather integrate on their own terms) — do not add it back.

Everything here is write-only. See ``app.domain.ports.CrmProvider``: there is no
``fetch``, no ``search``, no ``list``, and adding one would make the tenant's CRM
a dependency of our pipeline rather than a destination for it.
"""

from app.adapters.crm.hubspot import HubspotCrm, build_hubspot
from app.adapters.crm.tokens import CrmAuthError, CrmTokens, expiry_from, pack, unpack
from app.adapters.crm.zoho import ZohoCrm, build_zoho

__all__ = [
    "CrmAuthError",
    "CrmTokens",
    "HubspotCrm",
    "ZohoCrm",
    "build_hubspot",
    "build_zoho",
    "expiry_from",
    "pack",
    "unpack",
]
