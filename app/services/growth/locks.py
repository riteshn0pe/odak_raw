"""One tenant's slot in one job family, as a Redis lease.

Lifted out of ``dispatch.py`` when the CRM push became the second caller. The
first version lived as private methods on ``DispatchService`` with a
``lock_key(tenant_id, family)`` that took no entity id, which was right while
"the tenant's outbound queue" was the only thing being serialised. It stops
being right the moment a second family wants per-entity locks: a copied
``_acquire`` is a copied fail-open policy and a copied TTL, and the two drift
the first time somebody tunes one of them.

Two things live here and nothing else:

* ``lock_key`` — the key shape. ``family`` may name an entity
  (``f"crm:{lead_id}"``), which is how the CRM push debounces per lead rather
  than per tenant. Locking per tenant there would serialise a tenant's whole
  CRM sync behind one lead.
* ``acquire`` / ``release`` — the lease, with the fail-open policy spelled out
  once. **A cache that is down grants the lock.** That is deliberate and it is
  the reason every caller must also have a correctness story that does not
  depend on the lock holding: the lock is fairness and debounce, never
  mutual exclusion for a thing that would be wrong to do twice. The CRM push
  gets that from the ``synced_status`` column and a stored ``external_id``;
  outbound gets it from the daily cap.
"""

from __future__ import annotations

from uuid import UUID

from app.core.logging import get_logger

logger = get_logger(__name__)

# Lease. Longer than a callback should take, short enough that a callback
# killed mid-flight does not park the tenant for an hour.
LOCK_TTL_SECONDS = 120


def lock_key(tenant_id: UUID, family: str) -> str:
    """Redis key for one tenant's slot in one job family.

    ``family`` is free-form and may carry an entity id — ``"send"`` is the
    tenant-wide outbound slot, ``f"crm:{lead_id}"`` is one lead's CRM push.
    """
    return f"t:{tenant_id}:growth:{family}"


async def acquire(container: object, key: str, token: str, *, ttl: int = LOCK_TTL_SECONDS) -> bool:
    """Take the lease, or report that somebody else holds it.

    Returns ``True`` when the cache is unreachable: with no working cache there
    is no fairness control, and the alternative — a Redis outage stopping every
    tenant's outbound entirely — is worse than the contention it would prevent.
    """
    cache = getattr(container, "cache", None)
    if cache is None:
        return True
    try:
        acquired: bool = await cache.acquire_lease(key, token, ttl)
        return acquired
    except Exception:
        logger.warning("growth_lock_unavailable", key=key, exc_info=True)
        return True


async def release(container: object, key: str, token: str) -> None:
    """Give the lease back. A failure here is a log line, never an error.

    The lease expires on its own; a release that could not reach Redis has
    already been made harmless by the TTL, and raising would turn a successful
    job into a failed one in its last statement.
    """
    cache = getattr(container, "cache", None)
    if cache is None:
        return
    try:
        await cache.release_lease(key, token)
    except Exception:
        logger.debug("growth_lock_release_failed", key=key, exc_info=True)
