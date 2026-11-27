"""Which lead-data and verification vendor a given tenant actually uses.

Two layers, and the order is the whole module:

1. **The tenant's own account**, connected on the Integrations screen. The key
   is a tenant secret, so the adapter is built per call and closed after it —
   the same rule ``ApprovalService._notifiers`` follows, for the same reason: a
   cached adapter either holds a decrypted credential in memory or serves a
   stale one after a rotation.
2. **The platform's key**, wired as a ``cached_property`` on the container.
   Shared, long-lived, and safe to cache because nothing about it is per tenant.

Either layer may be absent. Both absent is the ordinary state: web-only research
is the base case, and these vendors raise the verified-contact rate rather than
enabling the feature.

The caller is handed a context manager rather than an object, because "close it
if and only if you built it" is a rule every call site would otherwise have to
remember, and the one that forgets leaks an httpx client per research run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.core.logging import get_logger

logger = get_logger(__name__)

# growth_integrations.provider -> the tenant_secrets provider name its key is
# stored under. Mirrors SECRET_PROVIDERS in app/api/v1/growth.py, which is where
# the write side lives.
SECRET_PROVIDERS: dict[str, str] = {
    "apollo": "apollo",
    "hunter": "hunter",
    "email_verifier": "email_verifier",
}


@dataclass(slots=True)
class Leased:
    """A provider and whether this lease owns it.

    ``owned`` is what decides who closes the client. A tenant-keyed adapter was
    built for this call and must be closed by it; the container's is shared and
    closing it would break the next caller.
    """

    provider: Any | None
    owned: bool = False


class ProviderResolver:
    """Resolves the vendor a tenant's growth run should call."""

    def __init__(self, container: Any) -> None:
        self._container = container

    @asynccontextmanager
    async def lead_provider(
        self, tenant_id: UUID, vendor: str
    ) -> AsyncIterator[Any | None]:
        """Apollo or Hunter for this tenant, or None when neither layer has it."""
        leased = await self._lease_lead_provider(tenant_id, vendor)
        try:
            yield leased.provider
        finally:
            await self._release(leased)

    @asynccontextmanager
    async def email_verifier(self, tenant_id: UUID) -> AsyncIterator[Any | None]:
        """The verifier for this tenant, or None."""
        leased = await self._lease_verifier(tenant_id)
        try:
            yield leased.provider
        finally:
            await self._release(leased)

    # ---------------------------------------------------------------- private

    async def _lease_lead_provider(self, tenant_id: UUID, vendor: str) -> Leased:
        key = await self._tenant_key(tenant_id, vendor)
        timeout = float(self._container.settings.HTTP_TIMEOUT_SECONDS)
        if key:
            try:
                if vendor == "apollo":
                    from app.adapters.leads.apollo import build_apollo

                    return Leased(build_apollo(key, timeout=timeout), owned=True)
                if vendor == "hunter":
                    from app.adapters.leads.hunter import build_hunter

                    return Leased(build_hunter(key, timeout=timeout), owned=True)
            except Exception:
                # a tenant's own bad key must not stop the run: fall through to
                # the platform key, which is what would have been used anyway
                logger.warning(
                    "growth_tenant_provider_rejected",
                    tenant_id=str(tenant_id),
                    vendor=vendor,
                    exc_info=True,
                )
        return Leased(getattr(self._container, vendor, None), owned=False)

    async def _lease_verifier(self, tenant_id: UUID) -> Leased:
        key = await self._tenant_key(tenant_id, "email_verifier")
        settings = self._container.settings
        if key:
            from app.adapters.leads.verifier import build_verifier

            # the tenant supplies a key, never a vendor: which vendor the
            # platform integrates with is a platform decision, and a per-tenant
            # backend choice would be a second code path nobody exercises
            backend = settings.EMAIL_VERIFIER_BACKEND
            if backend not in ("", "none"):
                try:
                    return Leased(
                        build_verifier(
                            backend, key, timeout=float(settings.HTTP_TIMEOUT_SECONDS)
                        ),
                        owned=True,
                    )
                except Exception:
                    logger.warning(
                        "growth_tenant_verifier_rejected",
                        tenant_id=str(tenant_id),
                        exc_info=True,
                    )
        return Leased(getattr(self._container, "email_verifier", None), owned=False)

    async def _tenant_key(self, tenant_id: UUID, vendor: str) -> str:
        """The tenant's own credential for a vendor, or "".

        Requires the integration row to be `active`: a tenant who disconnected a
        provider must stop being billed by it, and leaving the secret readable
        after the row went inactive would keep the calls flowing.
        """
        provider = SECRET_PROVIDERS.get(vendor)
        if provider is None:
            return ""
        row = await self._container.growth.integrations.get(tenant_id, vendor)
        if row is None or row.get("status") != "active" or not row.get("secret_ref"):
            return ""

        from sqlalchemy import text

        from app.core.db import tenant_scope

        async with tenant_scope(tenant_id) as session:
            secret = (
                await session.execute(
                    text(
                        "SELECT ciphertext FROM tenant_secrets "
                        "WHERE tenant_id = :tid AND ref = :ref AND provider = :provider"
                    ),
                    {
                        "tid": tenant_id,
                        "ref": str(row["secret_ref"]),
                        "provider": provider,
                    },
                )
            ).mappings().first()
        if secret is None:
            logger.warning(
                "growth_provider_secret_missing", tenant_id=str(tenant_id), vendor=vendor
            )
            return ""
        try:
            from app.core.security import decrypt_secret

            return decrypt_secret(
                str(secret["ciphertext"]), self._container.settings.ENCRYPTION_KEY
            )
        except Exception:
            logger.warning(
                "growth_provider_secret_undecryptable",
                tenant_id=str(tenant_id),
                vendor=vendor,
            )
            return ""

    async def _release(self, leased: Leased) -> None:
        if not leased.owned or leased.provider is None:
            return
        try:
            await leased.provider.close()
        except Exception:
            logger.debug("growth_provider_close_failed", exc_info=True)
