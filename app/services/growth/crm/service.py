"""One-way CRM sync: leads out, activity notes out, nothing back.

The service owns three things the adapters deliberately cannot see:

*Which leads go.* A lead is pushed when it changes — status, owner notes, a new
sent or received message — and that decision is ``on_lead_changed``, which never
raises: it is called from the middle of the pipeline (a reply router, a research
run) and a CRM hiccup there must never cost a lead its reply. The row on
``growth_crm_links`` is created before the push, so the worst failure window
still leaves a receipt saying we tried.

*How often.* Two layers, on purpose. The Redis lease
(``locks.lock_key(tenant_id, f"crm:{lead_id}")``) collapses a burst that is
still in flight — five status changes inside a lease TTL cost one API call. The
``synced_status`` column collapses a burst that outlives the lease or happens
while Redis is down, which is exactly the case ``locks.acquire`` fails *open*
for: the second push reads "the CRM already has this status and there are no new
notes" and touches no API at all. Neither layer is mutual exclusion; both are
debounce. Correctness does not fully rest on them — a *contact* cannot be
duplicated because the adapters' own upsert/409 path adopts the existing
record — but a *note* has no server-side dedupe, which is why the lease TTL in
``push_lead`` is sized to outlast a push's worst case rather than a callback's.

*What a failure means.* ``CrmAuthError`` is a connection problem — the card goes
red, the link rows stay pending and pick up on their own after a reconnect.
Every other provider error is a row problem — the link row goes red with the
reason, the attempts counter climbs and the retry backoff widens. Collapsing
them would hide a revoked token behind one bad email address, or declare a
tenant's whole CRM broken over one record.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.errors import NotFoundError, ProviderError, ValidationError
from app.core.logging import get_logger
from app.domain.ports import CrmActivity, CrmContact

logger = get_logger(__name__)

CRM_PROVIDERS: tuple[str, ...] = ("zoho", "hubspot")
PUSH_JOB = "growth_crm_push"
RETRY_TASK = "growth_crm_retry"
USAGE_KIND = "crm_push"
FEATURE = "crm"

# Same reading as stores.py: a CRM in `error` (backing off) is still connected,
# because the backoff exists to protect traffic, not to cancel it.
CONNECTED_STATUSES: frozenset[str] = frozenset({"active", "error"})

# How many leads one Resync / connect backfill may queue. The queue is fair per
# tenant, so the cap is a cap on one button press, not on the tenant's day.
RESYNC_MAX_LEADS = 500

# How many link rows one retry sweep may push. Same reasoning as the stores'
# MAX_PAGES_PER_RUN: a sweep is one callback with a timeout, not a migration.
MAX_PUSHES_PER_SWEEP = 25

# How many activity notes one push may write. A lead with a long history and a
# reconnect that moved `notes_through` should not turn one push into a hundred
# API calls; the next sweep continues from the new high-water mark.
MAX_NOTES_PER_PUSH = 20

# The retry ladder off `attempts`. Doubles to a day, like the stores' backoff,
# and for the same reason: a genuinely dead row costs one call a day rather
# than an hour of them.
RETRY_BACKOFF_MINUTES = 60
RETRY_BACKOFF_MAX_MINUTES = 24 * 60


class CrmService:
    """Push leads and their correspondence into a tenant's connected CRM."""

    def __init__(self, container: Any) -> None:
        self._c = container

    # ------------------------------------------------------------------ connect

    def _app(self, provider: str) -> dict[str, str] | None:
        """The platform OAuth app for one CRM, or None when unconfigured."""
        # bound to an annotated local rather than returned directly: the
        # container is untyped, and a direct return hands back Any
        app: dict[str, str] | None = None
        if provider == "zoho":
            app = self._c.zoho_app
        elif provider == "hubspot":
            app = self._c.hubspot_app
        return app

    def _check_provider(self, provider: str) -> str:
        provider = (provider or "").strip().lower()
        if provider not in CRM_PROVIDERS:
            raise ValidationError(f"unknown CRM {provider!r}")
        return provider

    def regions(self, provider: str) -> tuple[str, ...]:
        """Region codes one CRM accepts, so the API layer never has to import
        an adapter to ask."""
        if self._check_provider(provider) == "zoho":
            from app.adapters.crm.zoho import LOCATIONS

            return tuple(LOCATIONS)
        return ()

    async def authorize(
        self,
        tenant_id: UUID,
        provider: str,
        *,
        region: str = "",
        state: str,
        verifier: str = "",
    ) -> str:
        """The consent screen URL for one CRM.

        ``state`` is minted by the API layer (``_oauth_state``) and names
        ``f"{provider}.{region}"`` as its second field, so the callback gets the
        tenant, the provider and the Zoho data centre back from one signed
        value rather than trusting anything the CRM echoes. ``verifier`` is the
        PKCE half the API layer minted alongside it; it rides inside the signed
        state, so it comes back tamper-evident without a second store.
        """
        from app.adapters.crm import hubspot as hs
        from app.adapters.crm import zoho as zoho_mod

        provider = self._check_provider(provider)
        app = self._app(provider)
        if app is None:
            raise ValidationError(f"{provider} is not configured on this deployment yet")
        redirect = self._redirect(provider)
        if provider == "zoho":
            return zoho_mod.authorize_url(
                app["client_id"],
                redirect,
                state,
                accounts_host=zoho_mod.accounts_host_for(region),
                verifier=verifier,
            )
        return hs.authorize_url(app["client_id"], redirect, state, verifier=verifier)

    def _redirect(self, provider: str) -> str:
        base = str(self._c.settings.PUBLIC_BASE_URL or "").rstrip("/")
        return f"{base}/v1/growth/crm/{provider}/callback"

    async def complete(
        self,
        tenant_id: UUID,
        provider: str,
        code: str,
        *,
        region: str = "",
        accounts_server: str = "",
        verifier: str = "",
    ) -> dict[str, Any]:
        """Trade the one-time code for a grant and store it. Raises on refusal.

        The whole OAuth blob — access token, refresh token, API host, expiry —
        is one encrypted document in ``tenant_secrets`` (see
        ``adapters/crm/tokens.py`` for why it is not four columns). A refused
        exchange raises; nothing is stored, and the card stays as it was.
        ``verifier`` answers the PKCE challenge the authorize URL carried — a
        code minted against a challenge we did not issue is refused here.
        """
        from app.adapters.crm import hubspot as hs
        from app.adapters.crm import zoho as zoho_mod

        provider = self._check_provider(provider)
        app = self._app(provider)
        if app is None:
            raise ValidationError(f"{provider} is not configured on this deployment yet")
        redirect = self._redirect(provider)
        account = ""
        if provider == "zoho":
            tokens = await zoho_mod.exchange_code(
                code,
                client_id=app["client_id"],
                client_secret=app["client_secret"],
                redirect_uri=redirect,
                accounts_host=zoho_mod.accounts_host_for(region, accounts_server),
                code_verifier=verifier,
            )
            # Zoho's token response carries no account label; the API host is
            # the one thing in it that distinguishes one org from another.
            account = tokens.api_domain
        else:
            tokens = await hs.exchange_code(
                code,
                client_id=app["client_id"],
                client_secret=app["client_secret"],
                redirect_uri=redirect,
                code_verifier=verifier,
            )
            info = await hs.account_info(tokens.access_token)
            account = str(info.get("hub_id") or "")
        await self._store_grant(tenant_id, provider, tokens)
        await self._c.growth.integrations.upsert(
            tenant_id,
            provider,
            status="active",
            external_ref="",
            health={
                "account": account[:200],
                "connected_at": datetime.now(UTC).isoformat(),
            },
        )
        await self._c.growth.audit.record(
            tenant_id,
            agent="growth_crm",
            action="crm_connected",
            input={"provider": provider},
            reason="OAuth connect",
        )
        # the backfill: leads that existed before the CRM was connected have no
        # link rows and no push scheduled. One sweep-shaped job, not the loop
        # inline — up to RESYNC_MAX_LEADS ensure+enqueue round trips do not
        # belong inside the browser-facing callback, where a slow DB or queue
        # would read to the owner as a connect that never finished.
        from app.adapters.queue.base import Job

        try:
            await self._c.queue.enqueue(
                Job(
                    kind=PUSH_JOB,
                    tenant_id=tenant_id,
                    payload={"provider": provider, "resync": True},
                )
            )
        except Exception:
            # the grant is stored and the card is green; a backfill that could
            # not be queued is a warning and a Resync press, not a failed
            # connect
            logger.warning(
                "growth_crm_backfill_enqueue_failed",
                tenant_id=str(tenant_id),
                provider=provider,
                exc_info=True,
            )
        return {"status": "ok", "provider": provider, "backfill": "queued"}

    async def disconnect(self, tenant_id: UUID, provider: str) -> dict[str, Any]:
        """The tenant closed the door themselves.

        The grant stays in tenant_secrets (rotating it out is the CRM's job at
        their end; we cannot revoke it), but the integration row goes to a
        status the push loop refuses, so nothing is sent to a CRM the tenant
        has asked us to stop writing to.
        """
        provider = self._check_provider(provider)
        # `not_connected`, the same word the generic integrations PUT uses for
        # the same act, so the card's state machine has one vocabulary.
        await self._c.growth.integrations.upsert(
            tenant_id, provider, status="not_connected", external_ref="", health={}
        )
        await self._c.growth.audit.record(
            tenant_id,
            agent="growth_crm",
            action="crm_disconnected",
            input={"provider": provider},
            reason="owner disconnect",
        )
        return {"status": "ok", "provider": provider}

    async def connected_providers(self, tenant_id: UUID) -> list[str]:
        """Providers whose integration row still counts as a live connection."""
        rows = await self._c.growth.integrations.list(tenant_id)
        return sorted(
            {
                str(r.get("provider"))
                for r in rows
                if str(r.get("provider")) in CRM_PROVIDERS
                and str(r.get("status") or "") in CONNECTED_STATUSES
            }
        )

    # ------------------------------------------------------------- the triggers

    async def on_lead_changed(self, tenant_id: UUID, lead_id: UUID) -> None:
        """Queue a push for one changed lead, on every connected CRM.

        Called from the middle of the pipeline, so it never raises: a CRM that
        is down, unconfigured, or failing to enqueue must not cost a lead its
        reply. The link row is created here — before any push — so the first
        thing a failure can lose is the API call, never the receipt.
        """
        from app.adapters.queue.base import Job

        try:
            for provider in await self.connected_providers(tenant_id):
                try:
                    await self._c.growth.crm_links.ensure(tenant_id, provider, lead_id)
                    await self._c.queue.enqueue(
                        Job(
                            kind=PUSH_JOB,
                            tenant_id=tenant_id,
                            payload={"provider": provider, "lead_id": str(lead_id)},
                        )
                    )
                except Exception:
                    logger.warning(
                        "growth_crm_trigger_failed",
                        tenant_id=str(tenant_id),
                        provider=provider,
                        lead_id=str(lead_id),
                        exc_info=True,
                    )
        except Exception:
            # even the provider list is not the caller's problem
            logger.warning(
                "growth_crm_trigger_failed",
                tenant_id=str(tenant_id),
                lead_id=str(lead_id),
                exc_info=True,
            )

    async def resync(self, tenant_id: UUID, provider: str) -> dict[str, Any]:
        """The Resync button and the connect backfill: queue every unlinked lead.

        Existing rows are left alone — a synced row is re-pushed by its next
        real change, and re-pushing two thousand healthy rows because somebody
        pressed a button is how a CRM hits its rate limit for no new
        information. What gets queued is the leads with no link row at all.
        """
        from app.adapters.queue.base import Job

        provider = self._check_provider(provider)
        if provider not in await self.connected_providers(tenant_id):
            return {"status": "skipped", "reason": "not connected", "queued": 0}
        lead_ids = await self._c.growth.crm_links.missing(
            tenant_id, provider, limit=RESYNC_MAX_LEADS
        )
        queued = 0
        for lead_id in lead_ids:
            try:
                await self._c.growth.crm_links.ensure(tenant_id, provider, lead_id)
                await self._c.queue.enqueue(
                    Job(
                        kind=PUSH_JOB,
                        tenant_id=tenant_id,
                        payload={"provider": provider, "lead_id": str(lead_id)},
                    )
                )
                queued += 1
            except Exception:
                logger.warning(
                    "growth_crm_resync_enqueue_failed",
                    tenant_id=str(tenant_id),
                    provider=provider,
                    exc_info=True,
                )
        return {"status": "ok", "queued": queued}

    async def retry(
        self, tenant_id: UUID, lead_id: UUID, provider: str
    ) -> dict[str, Any]:
        """The per-row Retry button: one lead, pushed now, backoff waived.

        ``force`` is what separates this from the trigger path: an error row
        whose status and notes have not moved would otherwise be skipped as
        unchanged, and the button would look broken.
        """
        from app.adapters.queue.base import Job

        provider = self._check_provider(provider)
        if provider not in await self.connected_providers(tenant_id):
            # same refusal as resync: enqueueing a push against a CRM that is
            # not connected creates an error row and a no-op job per press
            return {"status": "skipped", "reason": "not connected"}
        if await self._c.growth.leads.get(tenant_id, lead_id) is None:
            # the link table's FK would turn a stale or guessed id into a 500;
            # the button deserves the same "no such lead" the lead routes give
            raise NotFoundError("no such lead")
        await self._c.growth.crm_links.ensure(tenant_id, provider, lead_id)
        await self._c.queue.enqueue(
            Job(
                kind=PUSH_JOB,
                tenant_id=tenant_id,
                payload={"provider": provider, "lead_id": str(lead_id), "force": True},
            )
        )
        return {"status": "ok", "provider": provider, "lead_id": str(lead_id)}

    # ---------------------------------------------------------------- the job

    async def run(self, tenant_id: UUID, payload: dict[str, Any] | None) -> dict[str, Any]:
        """``growth_crm_push`` entry. Four shapes of payload:

        * ``{provider, lead_id}`` — one changed lead (the trigger path)
        * ``{provider, lead_id, force}`` — one lead, backoff and
          unchanged-skip waived (the Retry button)
        * ``{provider}`` — the retry sweep: every due link row for that
          provider, backoff applied (the cron)
        * ``{provider, resync}`` — the connect backfill: queue every lead this
          CRM has never been linked to
        """
        payload = payload or {}
        provider = self._check_provider(str(payload.get("provider") or ""))
        if payload.get("resync"):
            return await self.resync(tenant_id, provider)
        lead_raw = str(payload.get("lead_id") or "")
        if lead_raw:
            return await self.push_lead(
                tenant_id, provider, UUID(lead_raw), force=bool(payload.get("force"))
            )
        return await self.retry_due(tenant_id, provider)

    async def retry_due(self, tenant_id: UUID, provider: str) -> dict[str, Any]:
        """Push every due link row for one provider, oldest attempt first.

        An ``error`` row is retried only once its backoff has elapsed —
        ``attempts`` is the exponent and ``updated_at`` is when the last attempt
        happened. A row with an unreadable ``updated_at`` is treated as due
        rather than never-due, for the same reason as the stores: a corrupt
        timestamp must not be what permanently disables a lead.
        """
        if provider not in await self.connected_providers(tenant_id):
            return {"status": "skipped", "reason": "not connected"}
        rows = await self._c.growth.crm_links.due(
            tenant_id, provider, limit=MAX_PUSHES_PER_SWEEP
        )
        pushed = 0
        failed = 0
        skipped = 0
        for row in rows:
            if not self._retry_ready(row):
                skipped += 1
                continue
            lead_id = UUID(str(row.get("lead_id")))
            try:
                result = await self.push_lead(tenant_id, provider, lead_id, force=True)
            except Exception:
                # push_lead already recorded the row-level failure; this net is
                # only here so one row cannot end the sweep
                failed += 1
                logger.warning(
                    "growth_crm_sweep_row_failed",
                    tenant_id=str(tenant_id),
                    provider=provider,
                    lead_id=str(lead_id),
                    exc_info=True,
                )
                continue
            if str(result.get("status")) in ("synced", "skipped"):
                pushed += 1
            elif str(result.get("status")) in ("busy", "record_gone"):
                # `busy`: another push for this lead is in flight right now; it
                # will settle the row, and counting it as a failure would be
                # noise. `record_gone`: the row was reset and the recreate
                # queued — done, not failed, and not pushed either.
                skipped += 1
            else:
                failed += 1
        return {"status": "ok", "pushed": pushed, "failed": failed, "backing_off": skipped}

    @staticmethod
    def _retry_ready(row: dict[str, Any]) -> bool:
        """Has this error row's backoff elapsed?"""
        attempts = int(row.get("attempts") or 0)
        if attempts <= 0:
            return True
        updated = row.get("updated_at")
        if not isinstance(updated, datetime):
            return True
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        backoff = min(
            RETRY_BACKOFF_MINUTES * (2 ** (attempts - 1)), RETRY_BACKOFF_MAX_MINUTES
        )
        return datetime.now(UTC) - updated >= timedelta(minutes=backoff)

    # ---------------------------------------------------------------- the push

    async def push_lead(
        self, tenant_id: UUID, provider: str, lead_id: UUID, *, force: bool = False
    ) -> dict[str, Any]:
        """Push one lead: contact first, then the notes that are new to the CRM.

        The lease is debounce, not correctness (see the module docstring): when
        it is held, the burst's later changes are already queued behind the job
        that is running, so dropping this one costs nothing. When Redis is
        down, ``acquire`` grants it and the ``synced_status`` check is the thing
        that prevents a duplicate write.
        """
        from uuid import uuid4

        from app.services.growth import locks

        provider = self._check_provider(provider)
        key = locks.lock_key(tenant_id, f"crm:{lead_id}")
        token = uuid4().hex
        # the lease must outlast the push's worst case — one contact upsert
        # plus MAX_NOTES_PER_PUSH note calls at the HTTP timeout each — because
        # notes have no server-side dedupe: a lease that expires mid-push lets
        # the job queued behind it log the same notes again. The send family
        # keeps the shorter default in locks.py.
        timeout = float(self._c.settings.HTTP_TIMEOUT_SECONDS or 20)
        ttl = int(timeout * (MAX_NOTES_PER_PUSH + 2)) + 60
        if not await locks.acquire(self._c, key, token, ttl=ttl):
            return {"status": "busy"}
        try:
            return await self._push_lead_locked(tenant_id, provider, lead_id, force=force)
        finally:
            await locks.release(self._c, key, token)

    async def _push_lead_locked(
        self, tenant_id: UUID, provider: str, lead_id: UUID, *, force: bool
    ) -> dict[str, Any]:
        from app.adapters.crm.tokens import CrmRecordGoneError

        lead = await self._c.growth.leads.get(tenant_id, lead_id)
        if lead is None:
            return {"status": "missing"}
        # the enqueue-time filter cannot stop a job that was already in the
        # queue when the tenant disconnected (or that the at-least-once queue
        # redelivered). This check is the one that honours the disconnect.
        if provider not in await self.connected_providers(tenant_id):
            return {"status": "not_connected"}
        link = await self._c.growth.crm_links.ensure(tenant_id, provider, lead_id)
        status = str(link.get("status") or "pending")
        if status == "skipped" and not force:
            return {"status": "skipped"}
        if not str(lead.get("email") or "") and not str(lead.get("phone") or ""):
            # a CRM record needs a handle to dedupe on; with neither, every
            # push would create a fresh "Unknown" row. Not an error — the
            # tenant cannot fix it from the CRM, and a red row that says
            # "impossible" is noise.
            await self._c.growth.crm_links.mark_skipped(
                tenant_id, provider, lead_id, reason="lead has no email or phone"
            )
            return {"status": "skipped"}

        notes_through = link.get("notes_through")
        messages = await self._c.growth.messages.for_lead_since(
            tenant_id, lead_id, notes_through if isinstance(notes_through, datetime) else None,
            limit=MAX_NOTES_PER_PUSH,
        )
        lead_status = str(lead.get("status") or "")
        if (
            not force
            and str(link.get("external_id") or "")
            and status == "synced"
            and lead_status == str(link.get("synced_status") or "")
            and not messages
        ):
            return {"status": "unchanged"}

        try:
            adapter = await self._provider_for(tenant_id, provider)
        except Exception as exc:
            # a refused token refresh is a connection problem and is routed
            # like one — the card goes red, this lead's row stays pending —
            # rather than blamed on the lead
            await self._record_failure(tenant_id, provider, lead_id, exc)
            status_word = "auth_error" if _is_auth_error(exc) else "error"
            return {"status": status_word, "reason": str(exc)[:200]}
        if adapter is None:
            # no grant stored at all: the connection is gone, which is not
            # this lead's failure. The row stays as it was and picks up after
            # a reconnect.
            return {"status": "not_connected"}
        try:
            receipt = await adapter.upsert_contact(
                _contact(lead), external_id=str(link.get("external_id") or "")
            )
        except CrmRecordGoneError:
            # the record we hold an id for was deleted in the tenant's own CRM.
            # Retrying the same id fails identically forever, so: drop the id,
            # reset the row to pending with the backoff cleared, and queue the
            # recreate — which adopts by email (upsert/409) rather than making
            # the tenant a duplicate. Not routed through _record_failure: this
            # is not a failure of the lead or the connection.
            await self._c.growth.crm_links.reset_external_id(
                tenant_id, provider, lead_id
            )
            from app.adapters.queue.base import Job

            try:
                await self._c.queue.enqueue(
                    Job(
                        kind=PUSH_JOB,
                        tenant_id=tenant_id,
                        payload={"provider": provider, "lead_id": str(lead_id)},
                    )
                )
            except Exception:
                # the row is already pending; the retry sweep will pick it up
                # even if this enqueue did not land
                logger.warning(
                    "growth_crm_recreate_enqueue_failed",
                    tenant_id=str(tenant_id),
                    provider=provider,
                    lead_id=str(lead_id),
                    exc_info=True,
                )
            try:
                await adapter.close()
            except Exception:
                logger.debug("growth_crm_close_failed", exc_info=True)
            return {"status": "record_gone"}
        try:
            if not receipt.external_id or len(receipt.external_id) > 200:
                # a blank id would mark the row synced while invisible to the
                # duplicate guard (the partial unique index skips ''), and an
                # over-long one would be truncated into a collision with
                # another lead's stored id. Neither may be stored.
                raise ProviderError(provider, "CRM returned an unusable record id")
            last_note_at: datetime | None = None
            for message in messages:
                await adapter.log_activity(_activity(message, receipt.external_id))
                at = message.get("created_at")
                if isinstance(at, datetime):
                    last_note_at = at
            await self._c.growth.crm_links.mark_synced(
                tenant_id,
                provider,
                lead_id,
                external_id=receipt.external_id,
                synced_status=lead_status,
                notes_through=last_note_at,
            )
            if len(messages) >= MAX_NOTES_PER_PUSH:
                # the cap cut the history short, and a synced row is never
                # re-pushed by the retry sweep — without this, everything past
                # the first MAX_NOTES_PER_PUSH notes waits for the lead's next
                # real change. The cursor has advanced, so the follow-up picks
                # up exactly where this push stopped.
                from app.adapters.queue.base import Job

                try:
                    await self._c.queue.enqueue(
                        Job(
                            kind=PUSH_JOB,
                            tenant_id=tenant_id,
                            payload={"provider": provider, "lead_id": str(lead_id)},
                        )
                    )
                except Exception:
                    logger.warning(
                        "growth_crm_notes_followup_failed",
                        tenant_id=str(tenant_id),
                        provider=provider,
                        lead_id=str(lead_id),
                        exc_info=True,
                    )
        except Exception as exc:
            await self._record_failure(tenant_id, provider, lead_id, exc)
            status_word = "auth_error" if _is_auth_error(exc) else "error"
            return {"status": status_word, "reason": str(exc)[:200]}
        finally:
            try:
                await adapter.close()
            except Exception:
                logger.debug("growth_crm_close_failed", exc_info=True)
        await self._recover_integration(tenant_id, provider)
        await self._meter(tenant_id, provider, str(receipt.external_id))
        return {"status": "synced", "external_id": receipt.external_id}

    # --------------------------------------------------------------- the grant

    async def _provider_for(self, tenant_id: UUID, provider: str) -> Any | None:
        """The bound adapter for one CRM, refreshing the token first if needed.

        Built per call and closed by the caller, like ``stores.provider_for``:
        a bound adapter holds a tenant's access token, and caching it here
        would outlive the token's own expiry.
        """
        from app.adapters.crm import hubspot as hs
        from app.adapters.crm import tokens as crm_tokens
        from app.adapters.crm import zoho as zoho_mod

        packed = await self._read_grant(tenant_id, provider)
        tokens = crm_tokens.unpack(packed or "")
        if not tokens.access_token:
            return None
        if tokens.expired:
            app = self._app(provider)
            if app is None or not tokens.refresh_token:
                return None
            try:
                if provider == "zoho":
                    tokens = await zoho_mod.refresh_tokens(
                        tokens,
                        client_id=app["client_id"],
                        client_secret=app["client_secret"],
                    )
                else:
                    tokens = await hs.refresh_tokens(
                        tokens,
                        client_id=app["client_id"],
                        client_secret=app["client_secret"],
                    )
            except Exception as exc:
                # a refused refresh must reach the caller as the connection
                # problem it is. Returning None here used to make every due
                # row in a sweep go red with "not connected" while the card
                # stayed green, because no push ever saw an auth error to
                # route. CrmAuthError (revoked token, bad scope) goes to the
                # card; a transient ProviderError goes to the row — exactly
                # as if the push itself had failed that way.
                logger.warning(
                    "growth_crm_refresh_failed",
                    tenant_id=str(tenant_id),
                    provider=provider,
                    error=str(exc)[:200],
                )
                raise
            await self._store_grant(tenant_id, provider, tokens, status=None)
        if provider == "zoho":
            return zoho_mod.build_zoho(
                tokens, timeout=float(self._c.settings.HTTP_TIMEOUT_SECONDS or 20)
            )
        return hs.build_hubspot(
            tokens, timeout=float(self._c.settings.HTTP_TIMEOUT_SECONDS or 20)
        )

    async def _store_grant(
        self,
        tenant_id: UUID,
        provider: str,
        tokens: Any,
        *,
        status: str | None = "active",
    ) -> str:
        """Encrypt the OAuth blob; return the ref for the integration row.

        ``status`` is the integration status written alongside the ref. The
        refresh path passes ``None`` to keep whatever the row already says: a
        token refreshed mid-push must not flip a card the tenant disconnected
        (or that an earlier auth failure reddened) back to green — a
        successful push is what recovers an error card, not a token refresh.
        """
        from sqlalchemy import text

        from app.adapters.crm import tokens as crm_tokens
        from app.core.db import tenant_scope
        from app.core.security import encrypt_secret

        ref = f"growth:{provider}:".lower()[:200]
        ciphertext = encrypt_secret(
            crm_tokens.pack(tokens), self._c.settings.ENCRYPTION_KEY
        )
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO tenant_secrets (tenant_id, ref, provider, ciphertext) "
                    "VALUES (:tid, :ref, :provider, :ciphertext) "
                    "ON CONFLICT (tenant_id, ref) DO UPDATE SET "
                    "ciphertext = EXCLUDED.ciphertext, provider = EXCLUDED.provider, "
                    "rotated_at = now()"
                ),
                {"tid": tenant_id, "ref": ref, "provider": provider, "ciphertext": ciphertext},
            )
        if status is None:
            integration = await self._c.growth.integrations.get(tenant_id, provider, "")
            status = str((integration or {}).get("status") or "active")
        await self._c.growth.integrations.upsert(
            tenant_id, provider, status=status, external_ref="", secret_ref=ref
        )
        return ref

    async def _read_grant(self, tenant_id: UUID, provider: str) -> str | None:
        integration = await self._c.growth.integrations.get(tenant_id, provider, "")
        ref = (integration or {}).get("secret_ref")
        if not ref:
            return None
        from sqlalchemy import text

        from app.core.db import tenant_scope
        from app.core.security import decrypt_secret

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
            return decrypt_secret(str(row["ciphertext"]), self._c.settings.ENCRYPTION_KEY)
        except Exception:
            logger.warning(
                "growth_crm_secret_undecryptable",
                tenant_id=str(tenant_id),
                provider=provider,
            )
            return None

    # -------------------------------------------------------------- the surfaces

    async def links(
        self,
        tenant_id: UUID,
        *,
        provider: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """The CRM links screen: one page of rows plus the per-provider header.

        ``total`` is the count under the same filters the page was fetched
        with, so the screen can tell "no more rows" from "the window ended"
        without asking for a page of nothing.
        """
        if provider is not None:
            provider = self._check_provider(provider)
        rows = await self._c.growth.crm_links.list(
            tenant_id, provider=provider, status=status, limit=limit, offset=offset
        )
        total = await self._c.growth.crm_links.count(
            tenant_id, provider=provider, status=status
        )
        summary = await self._c.growth.crm_links.summary(tenant_id)
        return {"items": rows, "summary": summary, "total": total}

    async def _record_failure(
        self, tenant_id: UUID, provider: str, lead_id: UUID, exc: Exception
    ) -> None:
        """One push failed. Route it: connection problems to the card, the
        rest to the link row it belongs to."""
        reason = str(exc)[:1000]
        if _is_auth_error(exc):
            # the link row stays as it was: an auth error is not this lead's
            # fault, and marking it error would burn its backoff for nothing.
            await self._mark_integration_error(tenant_id, provider, reason)
            return
        await self._c.growth.crm_links.mark_error(
            tenant_id, provider, lead_id, reason=reason
        )
        # a provider that is failing for every row (5xx, rate limit) also
        # colours the card, so the tenant sees it even before they open the
        # links screen
        if isinstance(exc, ProviderError) and not _is_auth_error(exc):
            integration = await self._c.growth.integrations.get(tenant_id, provider, "")
            health = dict((integration or {}).get("health") or {})
            health["last_push_error"] = reason[:300]
            await self._c.growth.integrations.upsert(
                tenant_id,
                provider,
                status=str((integration or {}).get("status") or "active"),
                external_ref="",
                health=health,
            )

    async def _mark_integration_error(
        self, tenant_id: UUID, provider: str, reason: str
    ) -> None:
        """The card goes red: revoked token, missing scope, dead app."""
        integration = await self._c.growth.integrations.get(tenant_id, provider, "")
        health = dict((integration or {}).get("health") or {})
        health["last_error"] = reason[:500]
        health["last_error_at"] = datetime.now(UTC).isoformat()
        health["consecutive_failures"] = int(health.get("consecutive_failures") or 0) + 1
        await self._c.growth.integrations.upsert(
            tenant_id, provider, status="error", external_ref="", health=health
        )
        await self._c.growth.audit.record(
            tenant_id,
            agent="growth_crm",
            action="crm_push_error",
            input={"provider": provider},
            reason=reason[:500],
        )

    async def _recover_integration(self, tenant_id: UUID, provider: str) -> None:
        """A push that came back is the recovery signal: the error trail is
        dropped and the card goes back to active."""
        integration = await self._c.growth.integrations.get(tenant_id, provider, "")
        if integration is None or str(integration.get("status")) != "error":
            return
        health = dict((integration or {}).get("health") or {})
        health.pop("last_error", None)
        health.pop("last_error_at", None)
        health.pop("consecutive_failures", None)
        health.pop("last_push_error", None)
        await self._c.growth.integrations.upsert(
            tenant_id, provider, status="active", external_ref="", health=health
        )

    async def _meter(self, tenant_id: UUID, provider: str, external_id: str) -> None:
        from app.services.growth.metering import meter

        await meter(
            tenant_id, USAGE_KIND, feature=FEATURE, quantity=1, provider=provider
        )


# ------------------------------------------------------------------ helpers


def _contact(lead: dict[str, Any]) -> CrmContact:
    """One ``growth_leads`` row into the neutral contact the port speaks."""
    return CrmContact(
        person=str(lead.get("person") or ""),
        company=str(lead.get("company") or ""),
        role=str(lead.get("role") or ""),
        email=str(lead.get("email") or ""),
        phone=str(lead.get("phone") or ""),
        website=str(lead.get("domain") or ""),
        summary=str(lead.get("research_summary") or ""),
        source=str(lead.get("source") or ""),
        status=str(lead.get("status") or ""),
    )


def _activity(message: dict[str, Any], external_id: str) -> CrmActivity:
    """One sent or received message into a note the CRM can show inline."""
    channel = str(message.get("channel") or "email")
    direction = str(message.get("direction") or "out")
    arrow = "to" if direction == "out" else "from"
    occurred = message.get("created_at")
    return CrmActivity(
        external_id=external_id,
        title=f"{channel} {direction}",
        body=f"[{arrow} lead] {str(message.get('content') or '')[:500]}",
        occurred_at=occurred if isinstance(occurred, datetime) else None,
    )


def _is_auth_error(exc: Exception) -> bool:
    from app.adapters.crm import tokens as crm_tokens

    return isinstance(exc, crm_tokens.CrmAuthError)
