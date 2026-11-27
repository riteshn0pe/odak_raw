"""IndiaMART inbound: pull the enquiry feed, feed the shared pipeline.

IndiaMART is the one inbound source in this stage that is not mail: the
tenant's seller account exposes a pull API, so this service owns a schedule —
the hourly ``growth_indiamart_poll`` cron task enqueues a one-day backfill per
connected tenant, and the tenant's own backfill button runs the same job with
a wider window. Same job kind both ways, because "what changed since
yesterday" and "give me the last month" are the same operation at different
widths, and a second code path for the button is where the two would drift.

Nothing here is IndiaMART-specific past the fetch. Every enquiry goes through
``growth_lead_raw`` (deduped on the vendor's ``UNIQUE_QUERY_ID``, so a re-pulled
window is free) and then the same ``InboundService.ingest`` tail a website form
uses: same ``lead_uid`` dedupe, same qualification, same owner ping, same
acknowledgement. A marketplace enquiry is not a different kind of lead.

The pull itself is free — the API is included with the seller account — so
there is no metering on the fetch. The qualify pass inside ``ingest`` meters
as inbound feature usage, exactly as for a form.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.logging import get_logger

logger = get_logger(__name__)

AGENT_KEY = "growth_indiamart"
PROVIDER = "indiamart"

BACKFILL_JOB = "growth_inbound_backfill"
POLL_TASK = "growth_indiamart_poll"

MAX_BACKFILL_DAYS = 365


class IndiaMARTService:
    """Pull IndiaMART enquiries for one tenant and ingest each as a lead."""

    def __init__(self, container: Any) -> None:
        self._c = container

    # ------------------------------------------------------------------- jobs

    async def backfill(self, tenant_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        """The ``growth_inbound_backfill`` job, ``source == "indiamart"``.

        Run by the poll cron (``days=1``) and by the tenant's button (wider).
        Tile the window, pull each tile, and let the raw row's dedupe key make
        every pull idempotent — an enquiry already ingested is skipped by
        ``store`` returning None, not by this service remembering anything.
        """
        try:
            days = int(payload.get("days") or 1)
        except (TypeError, ValueError):
            days = 1
        days = max(1, min(days, MAX_BACKFILL_DAYS))

        integration = await self._c.growth.integrations.get(tenant_id, PROVIDER)
        if integration is None or integration.get("status") != "active":
            return {"status": "skipped", "reason": "indiamart is not connected"}

        api_key = await self._read_secret(tenant_id, str(integration.get("secret_ref") or ""))
        if not api_key:
            # a card that reads Connected while the key is unreadable would
            # silently stop delivering leads; failing the job is louder
            return {"status": "failed", "reason": "credential is missing or unreadable"}

        from app.adapters.inbound.indiamart import MAX_WINDOW_DAYS, build_indiamart

        source = build_indiamart(
            api_key, timeout=self._c.settings.HTTP_TIMEOUT_SECONDS
        )
        if source is None:
            return {"status": "failed", "reason": "credential is empty"}

        end = datetime.now(UTC).replace(microsecond=0)
        start = end - timedelta(days=days)
        ingested = 0
        duplicates = 0
        failed = 0
        tiles_failed = 0
        try:
            # tiles back to back; [start, end) tiles cover the window exactly,
            # and a boundary enquiry the vendor returns twice is deduped by
            # UNIQUE_QUERY_ID anyway. Errors are isolated per tile and per
            # enquiry: one vendor hiccup or one poisoned record must not cost
            # the rest of a 365-day window, because a "failed" job result is
            # still a 200 and the queue will not retry it
            tile_start = start
            while tile_start < end:
                tile_end = min(tile_start + timedelta(days=MAX_WINDOW_DAYS), end)
                try:
                    leads = await source.fetch_window(tile_start, tile_end)
                except Exception:
                    tiles_failed += 1
                    logger.warning(
                        "indiamart_tile_failed",
                        tenant_id=str(tenant_id),
                        window=f"{tile_start.isoformat()}..{tile_end.isoformat()}",
                        exc_info=True,
                    )
                    tile_start = tile_end
                    continue
                for lead in leads:
                    fields = {
                        "name": lead.name,
                        "company": lead.company,
                        "email": lead.email,
                        "phone": lead.phone,
                        "message": lead.message or lead.product,
                    }
                    try:
                        outcome = await self._ingest_one(tenant_id, lead, fields)
                    except Exception:
                        failed += 1
                        logger.warning(
                            "indiamart_ingest_failed",
                            tenant_id=str(tenant_id),
                            external_id=lead.external_id,
                            exc_info=True,
                        )
                        continue
                    if outcome == "duplicate":
                        duplicates += 1
                    elif outcome == "failed":
                        failed += 1
                    else:
                        ingested += 1
                tile_start = tile_end
        finally:
            await source.close()

        await self._save_health(tenant_id, failures=failed + tiles_failed)
        # a run where nothing arrived and something broke is a failure, not a
        # quiet "ok": the poll will not retry it, so the card must say so
        if ingested == 0 and (failed or tiles_failed):
            return {
                "status": "failed",
                "reason": (
                    f"{tiles_failed} window(s) and {failed} enquiry(ies) "
                    "failed, none ingested"
                ),
                "ingested": ingested,
                "duplicates": duplicates,
                "failed": failed,
                "days": days,
            }
        return {
            "status": "ok",
            "ingested": ingested,
            "duplicates": duplicates,
            "failed": failed,
            "days": days,
        }

    async def run_due(self, limit: int = 200) -> dict[str, Any]:
        """Hourly ``growth_indiamart_poll`` task: one one-day job per connected tenant.

        Candidate tenants under ``admin_scope`` against ``tenants`` (the only
        table without FORCE RLS), the per-tenant integration check inside that
        tenant's own scope — the same shape ``growth_stores.run_due`` uses, for
        the same reason. Paged by id like the CRM retry fanout, not one page:
        an unpaged fan-out serves the first 200 tenants forever and the rest
        never. A missed tick costs one day of enquiries and the next tick's
        window re-pulls them; a double-fire is deduped by the raw row.
        """
        from app.adapters.postgres.growth_repo import active_tenant_ids
        from app.adapters.queue.base import Job

        queued = 0
        skipped = 0
        after: UUID | None = None
        while True:
            tenants = await active_tenant_ids(limit=limit, after=after)
            if not tenants:
                break
            for tenant_id in tenants:
                try:
                    integration = await self._c.growth.integrations.get(
                        tenant_id, PROVIDER
                    )
                    if integration is None or integration.get("status") != "active":
                        skipped += 1
                        continue
                    await self._c.queue.enqueue(
                        Job(
                            kind=BACKFILL_JOB,
                            tenant_id=tenant_id,
                            payload={"source": PROVIDER, "days": 1},
                        )
                    )
                    queued += 1
                except Exception:
                    logger.warning(
                        "indiamart_poll_enqueue_failed",
                        task=POLL_TASK,
                        tenant_id=str(tenant_id),
                        exc_info=True,
                    )
            if len(tenants) < limit:
                break
            after = tenants[-1]
        return {
            "status": "ok",
            "task": POLL_TASK,
            "queued": queued,
            "skipped": skipped,
        }

    # ---------------------------------------------------------------- private

    async def _ingest_one(
        self, tenant_id: UUID, lead: Any, fields: dict[str, str]
    ) -> str:
        """Archive one enquiry then run it through the shared tail.

        "duplicate" when the enquiry already became a lead, "failed" from
        ingest validation (no usable contact — recorded on the raw row),
        "ok" otherwise. A collision on the vendor's id is not automatically a
        duplicate: a previous run that died between archive and resolve left
        the row unclaimed, and skipping it would drop that enquiry forever —
        every future pull hits the same key.
        """
        raw_row = await self._c.growth.raw.store(
            tenant_id,
            source=PROVIDER,
            payload={
                "external_id": lead.external_id,
                "name": lead.name,
                "company": lead.company,
                "email": lead.email,
                "phone": lead.phone,
                "message": (lead.message or lead.product)[:4000],
                **dict(lead.raw),
            },
            dedupe_key=lead.external_id,
        )
        raw_id: UUID
        if raw_row is None:
            existing = await self._c.growth.raw.get_by_dedupe_key(
                tenant_id, PROVIDER, lead.external_id
            )
            if existing is None or existing.get("lead_id") is not None:
                return "duplicate"
            if existing.get("parse_error"):
                # already recorded as unusable; re-ingesting would only
                # record the same verdict again
                return "failed"
            raw_id = UUID(str(existing["id"]))
        else:
            raw_id = UUID(str(raw_row["id"]))

        result = await self._c.growth_inbound.ingest(
            tenant_id,
            source=PROVIDER,
            fields=fields,
            raw_id=raw_id,
        )
        if result.get("status") != "ok":
            return "failed"
        return "ok"

    async def _read_secret(self, tenant_id: UUID, ref: str) -> str | None:
        """The tenant's IndiaMART key, or None. Never stored in plain columns.

        Takes the secret reference the caller already read off the integration
        row, so a job does not query the same row twice.
        """
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
                    {"tid": tenant_id, "ref": ref},
                )
            ).mappings().first()
        if row is None:
            return None
        try:
            from app.core.security import decrypt_secret

            return decrypt_secret(
                str(row["ciphertext"]), self._c.settings.ENCRYPTION_KEY
            )
        except Exception:
            logger.warning(
                "indiamart_secret_undecryptable",
                tenant_id=str(tenant_id),
            )
            return None

    async def _save_health(self, tenant_id: UUID, failures: int = 0) -> None:
        """Note the pull on the integration row's ``health`` column.

        ``health`` is a whole-column replace at the SQL layer, so the row is
        re-read and merged at write time — never built from a snapshot the job
        started with. That also protects ``status``: ``upsert`` overwrites it
        unconditionally, and a tenant who disconnected the source while a
        365-day backfill ran must not have it written back to ``active``.
        Bookkeeping never kills the pull: a failure here is logged and
        swallowed.

        Failures surface on the card (``last_error``), because a pull that
        quietly swallowed a whole tile would otherwise stamp a fresh
        ``last_pull_at`` over a source that is actually broken.
        """
        try:
            current = await self._c.growth.integrations.get(tenant_id, PROVIDER)
            if current is None or current.get("status") != "active":
                # disconnected mid-job: leave it disconnected
                return
            health = dict(current.get("health") or {})
            health["last_pull_at"] = _now_iso()
            if failures:
                health["last_error"] = (
                    f"last pull dropped {failures} enquiry(ies) or window(s)"
                )[:500]
                health["last_error_at"] = _now_iso()
            else:
                health.pop("last_error", None)
            await self._c.growth.integrations.upsert(
                tenant_id,
                provider=PROVIDER,
                status="active",
                external_ref=str(current.get("external_ref") or ""),
                secret_ref=current.get("secret_ref"),
                health=health,
            )
        except Exception:
            logger.warning(
                "indiamart_health_write_failed", tenant_id=str(tenant_id), exc_info=True
            )


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
