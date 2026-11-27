"""Store connections: Shopify + WooCommerce sync, webhooks, CSV import.

One service for the three doors into ``growth_customers``/``growth_orders``.
The sync itself is incremental: each integration row's ``health`` carries one
opaque cursor per side (``sync_cursor_customers`` / ``sync_cursor_orders``),
each an ``updated_at`` bookmark owned by the adapter, and a run pages until
the adapter says done. A store that fails is marked ``error`` with the reason
in ``health`` and the next store still syncs — one tenant's dead WooCommerce
key must not stop their Shopify orders.

Secrets never touch these tables: the Shopify token and the WooCommerce
keypair live in ``tenant_secrets`` (Fernet envelope), one ref per
(provider, external_ref) so two shops do not share a credential row.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.domain.growth import lead_uid, normalise_email, normalise_phone

logger = get_logger(__name__)

STORE_SYNC_JOB = "growth_store_sync"
SYNC_TASK = "growth_store_sync"

STORE_PROVIDERS: tuple[str, ...] = ("shopify", "woocommerce")

# tenant_secrets provider names for the store credentials.
SECRET_PROVIDERS: dict[str, str] = {
    "shopify": "shopify",
    "woocommerce": "woocommerce",
}

# Statuses that still count as a live connection. `error` is in here on
# purpose: a store whose pull is backing off should still be allowed to push
# us an order and to mint a campaign's discount code, because rejecting those
# loses exactly the traffic the backoff was meant to protect. Only a status the
# tenant themselves set closes the door.
CONNECTED_STATUSES: frozenset[str] = frozenset({"active", "error"})

SYNC_USAGE_KIND = "store_sync"
FEATURE = "campaign"

MAX_PAGES_PER_RUN = 20
PAGE_SIZE = 100

# How a store that failed gets another chance.
#
# A store marked ``error`` used to be a store that never synced again: every
# entry point filters on ``active`` and nothing restored it, so one timeout on
# one hourly tick disabled that shop until a human reconnected it. The chosen
# recovery is **retry on a backoff**: an ``error`` store is still scheduled once
# its backoff has elapsed, and the first successful page flips it back to
# ``active`` (``_save_health`` writes that status on every page it persists).
# The alternative — degrade only after N failures — was not chosen because it
# still leaves the terminal state terminal; this way there is no state a store
# cannot climb out of without a human.
#
# The backoff doubles per consecutive failure off the hourly tick and stops at a
# day, so a genuinely dead store costs one request a day rather than 24.
RETRY_BACKOFF_MINUTES = 60
RETRY_BACKOFF_MAX_MINUTES = 24 * 60


class StoreSyncService:
    """Incremental sync, webhook upserts, and the structured CSV import."""

    def __init__(self, container: Any) -> None:
        self._c = container

    # ------------------------------------------------------------ cron fan-out

    async def run_due(self, limit: int = 200) -> dict[str, Any]:
        """Hourly ``growth_store_sync`` task: one job per tenant per store.

        Candidate selection under ``admin_scope`` against ``tenants`` (the only
        table without FORCE RLS), the per-tenant store list inside that
        tenant's own scope — the same shape ``approval_expiry`` uses, for the
        same reason. Per store rather than per tenant so one giant shop cannot
        starve the tenant's other shop inside a single callback's budget.
        """
        from app.adapters.postgres.growth_repo import active_tenant_ids
        from app.adapters.queue.base import Job

        tenants = await active_tenant_ids(limit=limit)
        queued = 0
        skipped = 0
        failures: list[str] = []
        for tenant_id in tenants:
            try:
                stores = await self._active_stores(tenant_id)
                if not stores:
                    skipped += 1
                    continue
                for provider, external_ref in stores:
                    payload = {"provider": provider, "external_ref": external_ref}
                    try:
                        await self._c.queue.enqueue(
                            Job(kind=STORE_SYNC_JOB, tenant_id=tenant_id, payload=payload)
                        )
                        queued += 1
                    except Exception:
                        logger.warning(
                            "growth_store_sync_enqueue_failed",
                            task=SYNC_TASK,
                            tenant_id=str(tenant_id),
                            provider=provider,
                            exc_info=True,
                        )
                        await self.sync_store(tenant_id, provider, external_ref)
                        queued += 1
            except Exception:
                failures.append(str(tenant_id))
                logger.warning(
                    "growth_store_sync_tenant_failed",
                    task=SYNC_TASK,
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
        return {
            "status": "ok",
            "tenants": len(tenants),
            "queued": queued,
            "skipped": skipped,
            "failed": failures,
        }

    async def _active_stores(self, tenant_id: UUID) -> list[tuple[str, str]]:
        """Stores worth syncing this tick: the healthy ones plus the ones due a retry."""
        rows = await self._c.growth.integrations.list(tenant_id)
        return sorted(
            {
                (str(r.get("provider")), str(r.get("external_ref") or ""))
                for r in rows
                if str(r.get("provider")) in STORE_PROVIDERS and _should_sync(r)
            }
        )

    async def _integrations(self, provider: str, tenant_id: UUID) -> list[dict[str, Any]]:
        rows = await self._c.growth.integrations.list(tenant_id)
        return [r for r in rows if str(r.get("provider")) == provider]

    # ------------------------------------------------------------------ sync

    async def sync_tenant(self, tenant_id: UUID) -> dict[str, Any]:
        """Sync every connected store for one tenant. One store's failure is data.

        Includes a store currently in ``error`` once its backoff has elapsed —
        see ``RETRY_BACKOFF_MINUTES`` for why that state has to be recoverable.
        """
        synced = {"customers": 0, "orders": 0, "stores": 0}
        for provider in STORE_PROVIDERS:
            for row in await self._integrations(provider, tenant_id):
                if not _should_sync(row):
                    continue
                try:
                    result = await self.sync_store(
                        tenant_id, provider, str(row.get("external_ref") or "")
                    )
                    synced["customers"] += int(result.get("customers", 0))
                    synced["orders"] += int(result.get("orders", 0))
                    synced["stores"] += 1
                except Exception:
                    logger.warning(
                        "growth_store_sync_failed",
                        tenant_id=str(tenant_id),
                        provider=provider,
                        store=str(row.get("external_ref") or ""),
                        exc_info=True,
                    )
                    await self._mark_error(
                        tenant_id, provider, str(row.get("external_ref") or ""), "sync failed"
                    )
        await self._meter(tenant_id, synced)
        return {"status": "ok", **synced}

    async def sync_store(
        self, tenant_id: UUID, provider: str, external_ref: str
    ) -> dict[str, Any]:
        """Page one store's customers then its orders (bounded), folding rows in.

        The two sides page independently off their own cursors: customers and
        orders change at different rates, and one side exhausting must not end
        the other's scan. Every page persists its cursor before the next fetch,
        so a callback killed mid-run resumes — it never restarts.
        """
        adapter = await self.provider_for(tenant_id, provider, external_ref)
        if adapter is None:
            raise ValueError(f"{provider} store {external_ref!r} is not connected")
        integration = await self._c.growth.integrations.get(
            tenant_id, provider, external_ref
        )
        health = dict((integration or {}).get("health") or {})
        customers = 0
        orders = 0
        try:
            pages = 0
            cursor = str(health.get("sync_cursor_customers") or "")
            while pages < MAX_PAGES_PER_RUN:
                previous = cursor
                page = await adapter.sync_customers(cursor=cursor, limit=PAGE_SIZE)
                for item in page.customers:
                    if await self._upsert_customer(tenant_id, provider, item):
                        customers += 1
                cursor = page.next_cursor
                pages += 1
                health["sync_cursor_customers"] = cursor
                health["last_sync_at"] = datetime.now(UTC).isoformat()
                # a page that came back is the recovery signal: the error trail
                # is dropped and _save_health writes status=active, so a store
                # that was in backoff is healthy again from here
                health.pop("last_error", None)
                health.pop("last_error_at", None)
                health.pop("consecutive_failures", None)
                await self._save_health(tenant_id, provider, external_ref, health)
                if _side_done(previous, cursor):
                    break
            cursor = str(health.get("sync_cursor_orders") or "")
            while pages < MAX_PAGES_PER_RUN * 2:
                previous = cursor
                page = await adapter.sync_orders(cursor=cursor, limit=PAGE_SIZE)
                for item in page.orders:
                    if await self._upsert_order(tenant_id, provider, item):
                        orders += 1
                cursor = page.next_cursor
                pages += 1
                health["sync_cursor_orders"] = cursor
                health["last_sync_at"] = datetime.now(UTC).isoformat()
                # a page that came back is the recovery signal: the error trail
                # is dropped and _save_health writes status=active, so a store
                # that was in backoff is healthy again from here
                health.pop("last_error", None)
                health.pop("last_error_at", None)
                health.pop("consecutive_failures", None)
                await self._save_health(tenant_id, provider, external_ref, health)
                if _side_done(previous, cursor):
                    break
        finally:
            try:
                await adapter.close()
            except Exception:
                logger.debug("growth_store_close_failed", exc_info=True)
        await self._c.growth.audit.record(
            tenant_id,
            agent="growth_store_sync",
            action="store_synced",
            input={"provider": provider, "store": external_ref},
            output={"customers": customers, "orders": orders},
            reason="hourly store sync",
        )
        return {"customers": customers, "orders": orders}

    def _guard_store_url(self, store_url: str) -> str:
        """The SSRF gate every WooCommerce adapter must pass through.

        A WooCommerce ``store_url`` is tenant-typed text that we then make an
        authenticated server-side request to, which is the exact shape of a
        proxy into our own network: ``http://169.254.169.254`` is the cloud
        metadata endpoint, ``10.x`` is the private subnet, and a hostile DNS
        record for a perfectly dotted name resolves into either. So the check is
        ``assert_fetchable`` — resolve the name and require every answer to be
        publicly routable — and not "the host contains a dot", which none of
        those fail.

        The scheme is filled in first because the adapter accepts a bare host
        and defaults it to https; the guard must see the same URL the client
        will actually open, not the one the tenant typed.
        """
        from app.core.urlguard import assert_fetchable

        value = (store_url or "").strip().rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            value = f"https://{value}"
        settings = self._c.settings
        assert_fetchable(
            value,
            allow_private=bool(settings.URL_FETCH_ALLOW_PRIVATE),
            allowed_ports=settings.url_fetch_ports,
        )
        return value

    async def verify_store_keys(
        self, store_url: str, consumer_key: str, consumer_secret: str
    ) -> None:
        """Prove a WooCommerce keypair against the store before it is stored.

        Raises ValidationError when the store refuses. The store's own answer is
        logged but never returned: this route is reachable by any tenant and the
        target is a URL they chose, so echoing the remote's exception text turns
        a rejection into a blind SSRF read — "connection refused" versus a TLS
        error versus a 401 maps out the internal network one attempt at a time.
        The adapter stays behind the service, where the one-way rule keeps it.
        """
        from app.core.errors import ValidationError

        guarded = self._guard_store_url(store_url)
        try:
            adapter = self._c.woocommerce.build_woocommerce(
                guarded,
                consumer_key,
                consumer_secret,
                timeout=self._c.settings.HTTP_TIMEOUT_SECONDS,
            )
        except ValueError as exc:
            raise ValidationError("that store URL is not usable") from exc
        if adapter is None:
            raise ValidationError("store_url, consumer_key and consumer_secret are all required")
        try:
            await adapter.sync_customers(cursor="", limit=1)
        except Exception as exc:
            logger.warning(
                "growth_store_verify_failed",
                provider="woocommerce",
                error=str(exc)[:200],
            )
            raise ValidationError(
                "the store did not accept these keys — check the URL and the "
                "consumer key/secret, then try again"
            ) from exc
        finally:
            try:
                await adapter.close()
            except Exception:
                logger.debug("growth_store_close_failed", exc_info=True)

    async def provider_for(
        self, tenant_id: UUID, provider: str, external_ref: str
    ) -> Any | None:
        """The bound adapter for one connected store, or None when unconfigured.

        The vendor modules come off the container — the only module that knows
        which implementation stands behind each port — rather than arriving as
        direct imports at each call site.
        """
        settings = self._c.settings
        if provider == "shopify":
            token = await self._read_secret(tenant_id, provider, external_ref)
            if not token:
                return None
            from app.adapters.stores.shopify import build_shopify

            # not tracked for shutdown: every caller closes in a finally, and
            # a per-call adapter outliving its call is a leak, not a pool
            return build_shopify(
                external_ref, token, timeout=settings.HTTP_TIMEOUT_SECONDS
            )
        if provider == "woocommerce":
            packed = await self._read_secret(tenant_id, provider, external_ref)
            if not packed:
                return None
            woo = self._c.woocommerce
            key, secret = woo.unpack_secret(packed)
            # re-guarded on every use, not only at connect: the stored
            # external_ref is a hostname, and a name that resolved publicly on
            # the day it was connected can be re-pointed at 10.0.0.5 tomorrow by
            # whoever controls its DNS. The sync path is the one that runs
            # unattended every hour, so it is the one that matters most
            return woo.build_woocommerce(
                self._guard_store_url(external_ref),
                key,
                secret,
                timeout=settings.HTTP_TIMEOUT_SECONDS,
            )
        return None

    # --------------------------------------------------- webhook registration

    async def register_webhooks(
        self, tenant_id: UUID, provider: str, external_ref: str
    ) -> dict[str, Any]:
        """Subscribe one connected store's topics to our ingress.

        Called at connect time. Nothing else creates a store subscription, so
        without this a connected store syncs hourly and never delivers a single
        webhook — the sync masks the failure, which is exactly why it has to be
        recorded on ``health`` rather than only logged.

        The delivery URL carries the tenant's ingress key in its query string.
        That is the sanctioned fallback (``ingress_url_with_key``, the same one
        the Telegram setup route hands out) and here it is the *only* option: a
        store's webhook subscription is a URL and nothing else — neither Shopify
        nor WooCommerce lets us attach a header to it.

        Never raises. A store that connects but cannot be subscribed is still a
        working store, and failing the connect over it would leave the tenant
        with a credential stored and no integration row to show for it.
        """
        from app.services.growth.ingresskey import ingress_url_with_key

        outcome: dict[str, Any]
        adapter = None
        try:
            adapter = await self.provider_for(tenant_id, provider, external_ref)
            if adapter is None:
                return {"status": "skipped", "reason": "store is not connected"}
            delivery_url = ingress_url_with_key(self._c.settings, tenant_id, provider)
            outcome = await adapter.register_webhooks(delivery_url)
        except Exception as exc:
            logger.warning(
                "growth_store_webhook_register_failed",
                tenant_id=str(tenant_id),
                provider=provider,
                store=external_ref,
                error=str(exc)[:200],
                exc_info=True,
            )
            await self._mark_health(
                tenant_id,
                provider,
                external_ref,
                {"webhook_error": str(exc)[:300], "webhooks_registered": []},
            )
            return {"status": "failed", "reason": "could not register webhooks"}
        finally:
            if adapter is not None:
                try:
                    await adapter.close()
                except Exception:
                    logger.debug("growth_store_close_failed", exc_info=True)

        registered = list(outcome.get("registered") or [])
        failed = dict(outcome.get("failed") or {})
        patch: dict[str, Any] = {
            "webhooks_registered": registered,
            "webhooks_registered_at": datetime.now(UTC).isoformat(),
        }
        if failed:
            # a partial registration is the dangerous case: orders arrive, buyers
            # do not, and nothing looks broken. Named on health so the panel can
            # say which topics are missing
            patch["webhook_error"] = "; ".join(
                f"{topic}: {reason}" for topic, reason in sorted(failed.items())
            )[:300]
        else:
            patch["webhook_error"] = None
        await self._mark_health(tenant_id, provider, external_ref, patch)
        return {"status": "ok", "registered": registered, "failed": failed}

    # --------------------------------------------------------------- webhooks

    async def handle_webhook(
        self, tenant_id: UUID, provider: str, topic: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """One store event, already verified and tenant-resolved by the ingress."""
        if provider == "shopify":
            from app.adapters.stores.shopify import customer_from_webhook, order_from_webhook

            customer = customer_from_webhook(topic, payload)
            order = order_from_webhook(topic, payload)
            if customer is None and order is None:
                return {"status": "ignored", "reason": f"topic {topic!r} carries nothing"}
        elif provider == "woocommerce":
            from app.adapters.stores.woocommerce import CUSTOMER_TOPICS as WOO_CUSTOMER_TOPICS
            from app.adapters.stores.woocommerce import ORDER_TOPICS as WOO_ORDER_TOPICS
            from app.adapters.stores.woocommerce import (
                customer_from_webhook as woo_customer,
            )
            from app.adapters.stores.woocommerce import order_from_webhook as woo_order

            customer = woo_customer(payload) if topic in WOO_CUSTOMER_TOPICS else None
            order = woo_order(payload) if topic in WOO_ORDER_TOPICS else None
            if customer is None and order is None:
                return {"status": "ignored", "reason": f"topic {topic!r} carries nothing"}
        else:
            return {"status": "ignored", "reason": f"unknown store {provider!r}"}

        wrote = {"customers": 0, "orders": 0}
        if customer is not None and await self._upsert_customer(
            tenant_id, provider, customer
        ):
            wrote["customers"] = 1
        if order is not None and await self._upsert_order(tenant_id, provider, order):
            wrote["orders"] = 1
        return {"status": "ok", **wrote}

    # -------------------------------------------------------------- CSV import

    async def import_csv(
        self, tenant_id: UUID, data: bytes, *, source: str = "csv"
    ) -> dict[str, Any]:
        """Structured customer import. Never the document pipeline.

        Columns: name, phone, email, last order date, spend, tags, plus optional
        whatsapp_opt_in / email_opt_in / language / first_order_at. Per-row
        errors are reported, never silent; a bad row costs that row, not the file.
        """
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return {"status": "rejected", "reason": "file is not valid UTF-8 CSV"}
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            return {"status": "rejected", "reason": "empty CSV"}
        fields = {f.strip().lower() for f in reader.fieldnames if f}
        if not ({"phone", "email"} & fields):
            return {
                "status": "rejected",
                "reason": "CSV needs at least a phone or an email column",
            }

        imported = 0
        errors: list[dict[str, Any]] = []
        row_no = 1
        for raw in reader:
            row_no += 1
            try:
                ok = await self._import_row(tenant_id, _lower_keys(raw), source=source)
                if ok:
                    imported += 1
            except ValueError as exc:
                errors.append({"row": row_no, "error": str(exc)[:200]})
            except Exception:
                logger.warning(
                    "growth_csv_row_failed",
                    tenant_id=str(tenant_id),
                    row=row_no,
                    exc_info=True,
                )
                errors.append({"row": row_no, "error": "could not import this row"})
            if row_no > 10000:
                errors.append({"row": row_no, "error": "stopped after 10000 rows"})
                break

        await self._c.growth.audit.record(
            tenant_id,
            agent="owner",
            action="customers_imported",
            input={"source": source},
            output={"imported": imported, "errors": len(errors)},
            reason="CSV import",
        )
        return {"status": "ok", "imported": imported, "errors": errors}

    async def _import_row(
        self, tenant_id: UUID, row: dict[str, Any], *, source: str
    ) -> bool:
        phone = normalise_phone(str(row.get("phone") or ""))
        email = normalise_email(str(row.get("email") or ""))
        if not phone and not email:
            raise ValueError("row has no usable phone or email")
        try:
            uid = lead_uid(str(tenant_id), phone=phone or None, email=email or None)
        except ValueError:
            raise ValueError("row has no usable phone or email") from None
        existing = await self._c.growth.customers.get_by_uid(tenant_id, uid)
        tags = _split_tags(str(row.get("tags") or ""))
        spend_micros = _money_to_micros(
            str(row.get("spend") or row.get("total_spend") or "")
        )
        last_raw = (
            str(row.get("last order date") or "")
            or str(row.get("last_order_at") or "")
            or str(row.get("last order") or "")
        )
        last_at = _parse_date(last_raw)
        first_at = _parse_date(str(row.get("first_order_at") or "")) or last_at
        # Both default False. A column holding an address is a contact detail,
        # not permission to market to it — defaulting email consent to "the row
        # had an email" manufactured a consent row, with a proof of "csv import",
        # in the ledger every sender trusts as the record of what the customer
        # actually agreed to. Only an explicit opt-in column grants it.
        consent = {
            "email": _truthy(row.get("email_opt_in"), default=False),
            "whatsapp": _truthy(row.get("whatsapp_opt_in"), default=False),
        }
        merged_tags = sorted(set(list((existing or {}).get("tags") or []) + tags))
        await self._c.growth.customers.upsert(
            tenant_id,
            {
                "uid": uid,
                "name": str(row.get("name") or "")[:200] or str((existing or {}).get("name") or ""),
                "phone": phone or str((existing or {}).get("phone") or ""),
                "email": email or str((existing or {}).get("email") or ""),
                "tags": merged_tags,
                "consent": consent,
                "language": str(row.get("language") or "") or (existing or {}).get("language"),
                "first_order_at": first_at or (existing or {}).get("first_order_at"),
                "last_order_at": last_at or (existing or {}).get("last_order_at"),
                "total_spend_micros": max(
                    spend_micros, int((existing or {}).get("total_spend_micros") or 0)
                ),
                "order_count": max(
                    1 if (last_at or spend_micros) else 0,
                    int((existing or {}).get("order_count") or 0),
                ),
            },
        )
        await self._record_store_consent(
            tenant_id, phone, email, consent, source="csv import"
        )
        _ = source
        return True

    # ------------------------------------------------------------------ upsert

    async def _upsert_customer(
        self, tenant_id: UUID, provider: str, item: Any
    ) -> bool:
        phone = normalise_phone(getattr(item, "phone", ""))
        email = normalise_email(getattr(item, "email", ""))
        if not phone and not email:
            return False
        try:
            uid = lead_uid(
                str(tenant_id), phone=phone or None, email=email or None
            )
        except ValueError:
            return False
        existing = await self._c.growth.customers.get_by_uid(tenant_id, uid)
        tags = sorted(
            set(list((existing or {}).get("tags") or []) + list(getattr(item, "tags", []) or []))
        )
        consent = dict(getattr(item, "consent", None) or {})
        if existing and not consent:
            consent = dict((existing or {}).get("consent") or {})
        await self._c.growth.customers.upsert(
            tenant_id,
            {
                "uid": uid,
                "name": getattr(item, "name", "") or str((existing or {}).get("name") or ""),
                "phone": phone or str((existing or {}).get("phone") or ""),
                "email": email or str((existing or {}).get("email") or ""),
                "tags": tags,
                "consent": consent,
                "language": getattr(item, "language", None) or (existing or {}).get("language"),
                "first_order_at": getattr(item, "first_order_at", None)
                or (existing or {}).get("first_order_at"),
                "last_order_at": getattr(item, "last_order_at", None)
                or (existing or {}).get("last_order_at"),
                "total_spend_micros": max(
                    int(getattr(item, "total_spend_micros", 0) or 0),
                    int((existing or {}).get("total_spend_micros") or 0),
                ),
                "order_count": max(
                    int(getattr(item, "order_count", 0) or 0),
                    int((existing or {}).get("order_count") or 0),
                ),
            },
        )
        _ = provider
        await self._record_store_consent(
            tenant_id, phone, email, consent, source=f"{provider} checkout"
        )
        return True

    async def _upsert_order(self, tenant_id: UUID, provider: str, item: Any) -> bool:
        external = str(getattr(item, "external_id", "") or "")
        if not external:
            return False
        customer_id = None
        email = normalise_email(getattr(item, "customer_email", ""))
        phone = normalise_phone(getattr(item, "customer_phone", ""))
        ext_cid = str(getattr(item, "customer_external_id", "") or "")
        if email or phone:
            try:
                uid = lead_uid(str(tenant_id), phone=phone or None, email=email or None)
            except ValueError:
                uid = ""
            if uid:
                existing = await self._c.growth.customers.get_by_uid(tenant_id, uid)
                if existing is not None:
                    customer_id = existing["id"]
        row = await self._c.growth.orders.upsert(
            tenant_id,
            {
                "customer_id": customer_id,
                "external_id": external,
                "store_provider": provider,
                "total_micros": int(getattr(item, "total_micros", 0) or 0),
                "payment": getattr(item, "payment", "prepaid") or "prepaid",
                "status": getattr(item, "status", "") or "",
                "items": getattr(item, "items", None) or {},
                "ordered_at": getattr(item, "ordered_at", None),
            },
        )
        _ = ext_cid
        if customer_id is not None and row.get("id") is not None and row.get("inserted"):
            # stats fold only on first sight: a re-paged order collides on the
            # upsert and must not increment the rollup twice. The orders table
            # stays the exact record; these columns stay the fast one.
            try:
                await self._c.growth.customers.touch_order_stats(
                    tenant_id,
                    customer_id,
                    ordered_at=getattr(item, "ordered_at", None),
                    total_micros=int(getattr(item, "total_micros", 0) or 0),
                )
            except Exception:
                logger.debug("growth_order_stats_touch_failed", exc_info=True)
        return True

    # ------------------------------------------------------------------ secrets

    async def store_credential(
        self, tenant_id: UUID, provider: str, external_ref: str, packed: str
    ) -> str:
        """Encrypt one store's credential; return the ref for the integration row."""
        from sqlalchemy import text

        from app.core.db import tenant_scope
        from app.core.security import encrypt_secret

        ref = f"growth:{provider}:{external_ref}".lower()[:200]
        ciphertext = encrypt_secret(packed, self._c.settings.ENCRYPTION_KEY)
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO tenant_secrets (tenant_id, ref, provider, ciphertext) "
                    "VALUES (:tid, :ref, :provider, :ciphertext) "
                    "ON CONFLICT (tenant_id, ref) DO UPDATE SET "
                    "ciphertext = EXCLUDED.ciphertext, provider = EXCLUDED.provider, "
                    "rotated_at = now()"
                ),
                {
                    "tid": tenant_id,
                    "ref": ref,
                    "provider": SECRET_PROVIDERS.get(provider, provider),
                    "ciphertext": ciphertext,
                },
            )
        return ref

    async def _read_secret(
        self, tenant_id: UUID, provider: str, external_ref: str
    ) -> str | None:
        integration = await self._c.growth.integrations.get(
            tenant_id, provider, external_ref
        )
        ref = (integration or {}).get("secret_ref")
        if not ref:
            return None
        from sqlalchemy import text

        from app.core.db import tenant_scope

        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT ciphertext FROM tenant_secrets "
                        "WHERE tenant_id = :tid AND ref = :ref"
                    ),
                    {"tid": tenant_id, "ref": str(ref)},
                )
            ).mappings().first()
        if row is None:
            return None
        try:
            from app.core.security import decrypt_secret

            return decrypt_secret(str(row["ciphertext"]), self._c.settings.ENCRYPTION_KEY)
        except Exception:
            logger.warning(
                "growth_store_secret_undecryptable",
                tenant_id=str(tenant_id),
                provider=provider,
            )
            return None

    # ------------------------------------------------------------------ private

    async def _save_health(
        self, tenant_id: UUID, provider: str, external_ref: str, health: dict[str, Any]
    ) -> None:
        await self._c.growth.integrations.upsert(
            tenant_id,
            provider,
            status="active",
            external_ref=external_ref,
            health=health,
        )

    async def _mark_health(
        self, tenant_id: UUID, provider: str, external_ref: str, patch: dict[str, Any]
    ) -> None:
        """Merge keys into one integration's ``health`` without touching its status.

        Webhook registration reports through here: whether a subscription was
        created says nothing about whether the store is connected, and writing a
        status from a registration outcome would let a Shopify hiccup disable a
        store that is syncing perfectly. A ``None`` value clears its key.
        """
        integration = await self._c.growth.integrations.get(
            tenant_id, provider, external_ref
        )
        if integration is None:
            return
        health = dict(integration.get("health") or {})
        for key, value in patch.items():
            if value is None:
                health.pop(key, None)
            else:
                health[key] = value
        await self._c.growth.integrations.upsert(
            tenant_id,
            provider,
            status=str(integration.get("status") or "active"),
            external_ref=external_ref,
            health=health,
        )

    async def _mark_error(
        self, tenant_id: UUID, provider: str, external_ref: str, reason: str
    ) -> None:
        """Record one failure and degrade the store — recoverably.

        ``consecutive_failures`` is what the backoff in ``_should_sync`` reads,
        and it is cleared by the first page that comes back, so ``error`` is a
        state the store climbs out of on its own rather than one that needs a
        reconnect.
        """
        integration = await self._c.growth.integrations.get(
            tenant_id, provider, external_ref
        )
        health = dict((integration or {}).get("health") or {})
        health["last_error"] = reason[:500]
        health["last_error_at"] = datetime.now(UTC).isoformat()
        health["consecutive_failures"] = int(health.get("consecutive_failures") or 0) + 1
        await self._c.growth.integrations.upsert(
            tenant_id,
            provider,
            status="error",
            external_ref=external_ref,
            health=health,
        )
        await self._c.growth.audit.record(
            tenant_id,
            agent="growth_store_sync",
            action="store_sync_error",
            input={"provider": provider, "store": external_ref},
            reason=reason,
        )

    async def _record_store_consent(
        self,
        tenant_id: UUID,
        phone: str,
        email: str,
        consent: dict[str, Any],
        *,
        source: str,
    ) -> None:
        """Mirror the store's opt-ins into the consent ledger the senders read.

        The customer's own ``consent`` column is what segments read; this is
        what the send gate enforces. A checkout tick-box is the proof, and its
        source names the store so support can answer "why am I getting these".
        Refusals are impossible here — consent rows are additive; an opt-out
        stays an opt-out because suppressions are checked first at send time.
        """
        for channel, address in (("whatsapp", phone), ("email", email)):
            if not address or not consent.get(channel):
                continue
            try:
                await self._c.growth.consent.consent(
                    tenant_id,
                    channel=channel,
                    address=address,
                    source=source,
                    proof={"origin": source},
                )
            except Exception:
                logger.debug("growth_store_consent_failed", exc_info=True)

    async def _meter(self, tenant_id: UUID, synced: dict[str, Any]) -> None:
        from app.services.growth.metering import meter

        await meter(
            tenant_id,
            SYNC_USAGE_KIND,
            feature=FEATURE,
            quantity=1,
            customers=int(synced.get("customers", 0)),
            orders=int(synced.get("orders", 0)),
        )


# ------------------------------------------------------------------ helpers


def _should_sync(row: dict[str, Any]) -> bool:
    """Is this integration due a sync pass right now?

    ``active`` always is. ``error`` is too, once its backoff has elapsed — that
    is the whole recovery mechanism (see ``RETRY_BACKOFF_MINUTES``): the store
    is retried, and the first page that comes back clears the error and restores
    ``active``. Any other status (disconnected, pending) is not a store the
    tenant is asking us to sync.

    An ``error`` row with no readable ``last_error_at`` is treated as due rather
    than as never-due: an unparseable timestamp must not be what permanently
    disables a shop.
    """
    status = str(row.get("status") or "")
    if status == "active":
        return True
    if status != "error":
        return False
    health = row.get("health") or {}
    failures = 0
    try:
        failures = max(0, int(health.get("consecutive_failures") or 0))
    except (TypeError, ValueError):
        failures = 0
    backoff = min(
        RETRY_BACKOFF_MINUTES * (2 ** max(0, failures - 1)), RETRY_BACKOFF_MAX_MINUTES
    )
    last_error_at = _parse_iso(str(health.get("last_error_at") or ""))
    if last_error_at is None:
        return True
    from datetime import timedelta

    return datetime.now(UTC) - last_error_at >= timedelta(minutes=backoff)


def _parse_iso(raw: str) -> datetime | None:
    """One ISO timestamp from ``health``, normalised to aware UTC."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _side_done(previous: str, cursor: str) -> bool:
    """Did this side's scan finish on that page?

    The adapters signal exhaustion by advancing the ``since`` half of the
    bookmark (to the ten-minute overlap); while pages remain only the paging
    half moves. Comparing halves — rather than trusting an empty cursor, which
    this bookmark shape never produces — is what ends the loop.
    """
    return previous.partition("|")[0] != cursor.partition("|")[0]


def _lower_keys(raw: dict[str, Any]) -> dict[str, Any]:
    return {(k or "").strip().lower(): v for k, v in raw.items()}


def _split_tags(raw: str) -> list[str]:
    return [t.strip()[:80] for t in raw.replace(";", ",").split(",") if t.strip()][:20]


def _truthy(value: Any, *, default: bool) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "y", "opt_in", "opted_in")


def _money_to_micros(raw: str) -> int:
    """A spend column ("₹1,299.50") as micros — millionths of a rupee.

    Micros throughout this codebase are millionths of a *major* unit, which is
    what ``_rupees()`` in the report renderer divides by and what the billing
    plans mean by ``cost_micros``. The multiplier is 1e6, not 1e5.
    """
    cleaned = raw.strip().replace(",", "").replace("₹", "").replace("rs", "").strip()
    if not cleaned:
        return 0
    try:
        return int(float(cleaned) * 1_000_000)
    except ValueError:
        return 0


def _parse_date(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
