"""Persistence for the growth log DB — the shared bus every growth agent reads
from and writes to.

One repository class per table group rather than one per table, because the
groups are what a caller actually needs together: an approval is meaningless
without its batch, a suppression without its consent. Every method takes
``tenant_id`` first and opens its own ``tenant_scope``; the RLS policy, not the
WHERE clause, is what confines the statement, and the WHERE clause is there
anyway so a policy that is ever mis-deployed fails closed on the query too.

The one exception is ``AgentModelConfigRepository``, whose platform-default rows
carry ``tenant_id IS NULL`` and are reached under ``admin_scope``. That table's
policy is written to make exactly those two access paths work and nothing else —
the reasoning is on the table in ``migrations/016_growth_core.sql``.

SQL lives here and in the services, nowhere else.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.core.db import admin_scope, tenant_scope
from app.core.logging import get_logger
from sqlalchemy import text

logger = get_logger(__name__)

# ``GrowthLeadsRepository`` defines a method called ``list``, which shadows the
# builtin inside every annotation in that class body. This alias is what lets the
# method keep the name that reads correctly at the call site.
Rows = list[dict[str, Any]]

# Same trap, same escape: ``GrowthCrmLinksRepository`` also has a ``list``, and
# its ``missing`` returns bare ids rather than row dicts.
Ids = list[UUID]

# And once more for the leads board's source chips.
Sources = list[str]

# The JSONB knob columns on growth_settings, in one place. Every one of them is
# read as a whole and written as a whole, so the read, the merge and the write
# can all iterate this rather than repeating nine column names three times.
SETTINGS_BLOCKS: tuple[str, ...] = (
    "profile",
    "hitl_levels",
    "send_caps",
    "follow_up",
    "offer_range",
    "languages",
    "grounding",
    "report_prefs",
    "channel_order",
)


def _dumps(value: Any) -> str:
    """JSON for a JSONB bind parameter, tolerant of what callers actually pass."""
    return json.dumps(_jsonable(value))


def _jsonable(value: Any) -> Any:
    """Coerce arbitrary payloads into something json can serialise.

    Same posture as the telemetry helper in ``conversation_repo``: callers pass
    UUIDs and datetimes freely, and a write that fails on a type it never
    promised to accept is a worse outcome than a stringified value.
    """
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


async def active_tenant_ids(limit: int = 200, after: UUID | None = None) -> list[UUID]:
    """Candidate selection for every growth cron task.

    Starts from ``tenants`` and never from a ``growth_*`` table, and that is
    load-bearing rather than stylistic. Every growth table carries FORCE RLS, so
    a query against one under ``admin_scope`` — which sets no ``app.tenant_id``,
    leaving ``current_tenant_id()`` NULL — matches no rows at all. A cross-tenant
    "which tenants have queued work" query would therefore return an empty list
    on a correctly-locked-down database and the whole scheduler would do nothing,
    silently and forever.

    ``tenants`` is the one table with no RLS policy, which is exactly why
    ``_email_retention`` selects from it too. The per-tenant check then happens
    inside that tenant's own scope, where the policy lets it see its own rows.

    ``after`` paginates by id so a deployment with more tenants than one tick's
    limit still reaches all of them across ticks, instead of serving the first
    200 forever.
    """
    clause = "AND id > :after" if after is not None else ""
    params: dict[str, Any] = {"limit": limit}
    if after is not None:
        params["after"] = after
    async with admin_scope() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id FROM tenants WHERE status = 'active' "  # noqa: S608
                    f"{clause} ORDER BY id LIMIT :limit"
                ),
                params,
            )
        ).mappings().all()
    return [row["id"] for row in rows]


class GrowthSettingsRepository:
    """The one configuration row per tenant."""

    async def get(self, tenant_id: UUID) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, tenant_id, profile, hitl_levels, send_caps, follow_up, "
                        "offer_range, languages, grounding, report_prefs, channel_order, "
                        "created_at, updated_at FROM growth_settings WHERE tenant_id = :tid"
                    ),
                    {"tid": tenant_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def upsert(self, tenant_id: UUID, blocks: dict[str, Any]) -> dict[str, Any]:
        """Write whole blocks, leaving untouched blocks exactly as they were.

        ``blocks`` holds only the block names the caller is changing, and each
        value is that block *complete*. This is the ``agentConfig.ts`` lesson
        applied server-side: a shallow merge of a partial block silently resets
        its siblings, so the merge is refused here and the service is required
        to hand over whole blocks it built on top of the stored ones.
        """
        unknown = set(blocks) - set(SETTINGS_BLOCKS)
        if unknown:
            raise ValueError(f"unknown growth settings block(s): {sorted(unknown)}")

        columns = [name for name in SETTINGS_BLOCKS if name in blocks]
        params: dict[str, Any] = {"tid": tenant_id}
        for name in columns:
            params[name] = _dumps(blocks[name])

        insert_cols = ", ".join(columns)
        insert_vals = ", ".join(f"CAST(:{name} AS jsonb)" for name in columns)
        updates = ", ".join(f"{name} = CAST(:{name} AS jsonb)" for name in columns)
        prefix = f", {insert_cols}" if columns else ""
        values = f", {insert_vals}" if columns else ""
        # a PUT that changes nothing still has to return a row, so the conflict
        # branch always has at least updated_at to set
        set_clause = f"{updates}, updated_at = now()" if columns else "updated_at = now()"

        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"INSERT INTO growth_settings (tenant_id{prefix}) "  # noqa: S608
                        f"VALUES (:tid{values}) "
                        f"ON CONFLICT (tenant_id) DO UPDATE SET {set_clause} "
                        "RETURNING id, tenant_id, profile, hitl_levels, send_caps, "
                        "follow_up, offer_range, languages, grounding, report_prefs, "
                        "channel_order, created_at, updated_at"
                    ),
                    params,
                )
            ).mappings().first()
        return dict(row) if row is not None else {}

    async def has_settings(self, tenant_id: UUID) -> bool:
        async with tenant_scope(tenant_id) as session:
            value = (
                await session.execute(
                    text("SELECT 1 FROM growth_settings WHERE tenant_id = :tid LIMIT 1"),
                    {"tid": tenant_id},
                )
            ).scalar_one_or_none()
        return value is not None


class GrowthIntegrationsRepository:
    """Provider connections. Never holds a secret, only a reference to one."""

    async def list(self, tenant_id: UUID) -> list[dict[str, Any]]:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, provider, status, external_ref, secret_ref, health, "
                        "created_at, updated_at FROM growth_integrations "
                        "WHERE tenant_id = :tid ORDER BY provider, external_ref"
                    ),
                    {"tid": tenant_id},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def get(
        self, tenant_id: UUID, provider: str, external_ref: str = ""
    ) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, provider, status, external_ref, secret_ref, health, "
                        "created_at, updated_at FROM growth_integrations "
                        "WHERE tenant_id = :tid AND provider = :provider "
                        "AND external_ref = :ref"
                    ),
                    {"tid": tenant_id, "provider": provider, "ref": external_ref},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def get_by_provider(
        self, tenant_id: UUID, provider: str
    ) -> dict[str, Any] | None:
        """The tenant's row for a provider, regardless of external_ref.

        Integrations are keyed (provider, external_ref) because a provider like
        a CRM can hold several connections, but some providers are
        single-connection by nature — a sending domain, a telegram binding —
        and their consumers must not have to guess the ref to find the row.
        Most recently updated wins if a re-connect left an older row behind.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, provider, status, external_ref, secret_ref, health, "
                        "created_at, updated_at FROM growth_integrations "
                        "WHERE tenant_id = :tid AND provider = :provider "
                        "ORDER BY updated_at DESC LIMIT 1"
                    ),
                    {"tid": tenant_id, "provider": provider},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def upsert(
        self,
        tenant_id: UUID,
        provider: str,
        *,
        status: str,
        external_ref: str = "",
        secret_ref: str | None = None,
        health: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update one connection.

        ``secret_ref`` of None leaves an existing reference alone rather than
        clearing it: a status update arriving from a health check must not
        disconnect the integration it was checking.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO growth_integrations "
                        "(tenant_id, provider, status, external_ref, secret_ref, health) "
                        "VALUES (:tid, :provider, :status, :ref, :secret_ref, "
                        "CAST(:health AS jsonb)) "
                        "ON CONFLICT (tenant_id, provider, external_ref) DO UPDATE SET "
                        "status = EXCLUDED.status, "
                        "secret_ref = COALESCE(EXCLUDED.secret_ref, "
                        "growth_integrations.secret_ref), "
                        "health = EXCLUDED.health, updated_at = now() "
                        "RETURNING id, provider, status, external_ref, secret_ref, "
                        "health, created_at, updated_at"
                    ),
                    {
                        "tid": tenant_id,
                        "provider": provider,
                        "status": status,
                        "ref": external_ref,
                        "secret_ref": secret_ref,
                        "health": _dumps(health or {}),
                    },
                )
            ).mappings().first()
        return dict(row) if row is not None else {}


class GrowthLeadsRepository:
    """Leads, keyed by the deterministic uid from ``app/domain/growth.py``."""

    _COLUMNS = (
        "id, uid, company, person, role, email, phone, domain, source, "
        "research_summary, research_links, language, status, owner_notes, "
        "follow_up_count, last_outbound_at, paused_until, created_at, updated_at"
    )

    async def get_by_uid(self, tenant_id: UUID, uid: str) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_leads "  # noqa: S608
                        "WHERE tenant_id = :tid AND uid = :uid"
                    ),
                    {"tid": tenant_id, "uid": uid},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def upsert(self, tenant_id: UUID, lead: dict[str, Any]) -> dict[str, Any]:
        """Insert a lead, or overwrite the existing row for the same uid.

        The *merge* is not done here — ``dedupe_lead`` in the domain layer owns
        the field rules, and the service reads, merges and hands the whole row
        back. Splitting it that way is what lets the merge rules be unit-tested
        without a database, which is the only way anyone will trust them.

        The returned row carries a ``prior_status`` key that is not a column:
        the status this lead held before this statement ran, or ``''`` when the
        row is new. It exists because this method is the *other* way a lead's
        status moves — the ``ON CONFLICT`` sets ``status = EXCLUDED.status`` —
        and anything watching for status changes on ``set_status`` alone would
        be watching a convention rather than a gate. The ``prior`` CTE and the
        insert share one snapshot, so the pre-write value is read in the same
        statement that overwrites it and there is no read-then-write race.
        """
        payload = dict(lead)
        payload["tid"] = tenant_id
        payload["research_links"] = _dumps(lead.get("research_links") or [])
        for key in (
            "company", "person", "role", "email", "phone", "domain",
            "source", "research_summary", "owner_notes",
        ):
            payload.setdefault(key, "")
        payload.setdefault("language", None)
        payload.setdefault("status", "new")

        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "WITH prior AS ("  # noqa: S608
                        "  SELECT status FROM growth_leads "
                        "   WHERE tenant_id = :tid AND uid = :uid"
                        "), up AS ("
                        "  INSERT INTO growth_leads (tenant_id, uid, company, person, "
                        "  role, email, phone, domain, source, research_summary, "
                        "  research_links, language, status, owner_notes) "
                        "  VALUES (:tid, :uid, :company, :person, :role, :email, "
                        "  :phone, :domain, :source, :research_summary, "
                        "  CAST(:research_links AS jsonb), :language, :status, "
                        "  :owner_notes) "
                        "  ON CONFLICT (tenant_id, uid) DO UPDATE SET "
                        "  company = EXCLUDED.company, person = EXCLUDED.person, "
                        "  role = EXCLUDED.role, email = EXCLUDED.email, "
                        "  phone = EXCLUDED.phone, domain = EXCLUDED.domain, "
                        "  source = EXCLUDED.source, "
                        "  research_summary = EXCLUDED.research_summary, "
                        "  research_links = EXCLUDED.research_links, "
                        "  language = EXCLUDED.language, status = EXCLUDED.status, "
                        "  owner_notes = EXCLUDED.owner_notes, updated_at = now() "
                        f"  RETURNING {self._COLUMNS}"
                        ") "
                        "SELECT up.*, COALESCE(prior.status, '') AS prior_status "
                        "  FROM up LEFT JOIN prior ON TRUE"
                    ),
                    payload,
                )
            ).mappings().first()
        return dict(row) if row is not None else {}

    async def list(
        self,
        tenant_id: UUID,
        *,
        status: str | None = None,
        source: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Rows:
        clauses = []
        params: dict[str, Any] = {"tid": tenant_id, "limit": limit, "offset": offset}
        if status:
            clauses.append("status = :status")
            params["status"] = status
        if source:
            clauses.append("source = :source")
            params["source"] = source
        clause = f"AND {' AND '.join(clauses)}" if clauses else ""
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_leads "  # noqa: S608
                        f"WHERE tenant_id = :tid {clause} "
                        "ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def distinct_sources(self, tenant_id: UUID) -> Sources:
        """The source values on the tenant's board, for the Leads filter chips.

        Empty-string sources stay hidden: a lead with no source is not a
        filterable category, it is the absence of one, and a chip labelled
        "" cannot be read by the person it is for.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT DISTINCT source FROM growth_leads "
                        "WHERE tenant_id = :tid AND source <> '' ORDER BY source"
                    ),
                    {"tid": tenant_id},
                )
            ).scalars().all()
        return [str(row) for row in rows]

    async def get(self, tenant_id: UUID, lead_id: UUID) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_leads "  # noqa: S608
                        "WHERE tenant_id = :tid AND id = :id"
                    ),
                    {"tid": tenant_id, "id": lead_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def get_by_address(
        self, tenant_id: UUID, *, email: str = "", phone: str = ""
    ) -> dict[str, Any] | None:
        """The lead behind an inbound message, matched on either identifier.

        This is the reply router's entry question — *is this stranger somebody we
        mailed?* — so it is deliberately one query over both columns rather than
        two calls the caller has to order. The addresses must already be
        normalised by ``app/domain/growth.py``; matching raw input here would
        make dedup and routing disagree about who somebody is.

        ``won`` leads are excluded, and that exclusion is load-bearing. A lead
        who converted is a *customer*: their next email is a support question and
        belongs to the RAG agent. Without this predicate, any address that ever
        appeared in ``growth_leads`` would have every future message intercepted
        by the growth reply agent forever, with no way back — the sale would
        permanently break support for the person who made it.

        Every other status stays routable on purpose, including ``suppressed``:
        somebody who opted out and writes again must reach the path that knows
        they opted out, not the one that would answer them.
        """
        if not email and not phone:
            return None
        clauses = []
        params: dict[str, Any] = {"tid": tenant_id}
        if email:
            clauses.append("email = :email")
            params["email"] = email
        if phone:
            clauses.append("phone = :phone")
            params["phone"] = phone
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_leads "  # noqa: S608
                        f"WHERE tenant_id = :tid AND ({' OR '.join(clauses)}) "
                        "AND status <> 'won' "
                        "ORDER BY updated_at DESC LIMIT 1"
                    ),
                    params,
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def list_by_ids(
        self, tenant_id: UUID, lead_ids: Sequence[UUID]
    ) -> Rows:
        if not lead_ids:
            return []
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_leads "  # noqa: S608
                        "WHERE tenant_id = :tid AND id = ANY(:ids)"
                    ),
                    {"tid": tenant_id, "ids": lead_ids},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def set_status(self, tenant_id: UUID, lead_id: UUID, status: str) -> bool:
        """Move one lead's status. Returns whether it actually moved.

        The return value is what the CRM push hangs off: a status write that
        sets the status it already had is not a change, and enqueuing a push for
        it would turn a re-run of any idempotent service into CRM API traffic.
        The write itself is unconditional so ``updated_at`` still bumps — the
        leads list orders on it, and a caller re-touching a lead means the lead
        was touched.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "WITH prior AS ("
                        "  SELECT id, status FROM growth_leads "
                        "   WHERE tenant_id = :tid AND id = :id"
                        ") "
                        "UPDATE growth_leads AS l "
                        "   SET status = :status, updated_at = now() "
                        "  FROM prior p "
                        " WHERE l.tenant_id = :tid AND l.id = p.id "
                        "RETURNING p.status AS prior_status"
                    ),
                    {"tid": tenant_id, "id": lead_id, "status": status},
                )
            ).mappings().first()
        return row is not None and str(row["prior_status"]) != status

    async def advance_status(
        self,
        tenant_id: UUID,
        lead_ids: Sequence[UUID],
        *,
        from_status: str,
        to_status: str,
    ) -> int:
        """Move a whole batch forward, and only the rows still at ``from_status``.

        The predicate is the concurrency control, the same one
        ``approvals.decide`` uses: an owner approving a batch twice, or an
        approval and an expiry racing, must move each lead exactly once. Returns
        how many actually moved, which is what tells the caller whether the batch
        was already released.
        """
        if not lead_ids:
            return 0
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    "UPDATE growth_leads SET status = :to, updated_at = now() "
                    "WHERE tenant_id = :tid AND id = ANY(:ids) AND status = :from"
                ),
                {
                    "tid": tenant_id,
                    "ids": lead_ids,
                    "to": to_status,
                    "from": from_status,
                },
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def mark_contacted(self, tenant_id: UUID, lead_id: UUID) -> None:
        """Record that something went out to this lead just now.

        ``last_outbound_at`` and ``follow_up_count`` live on the lead rather than
        being derived from ``growth_messages`` because the follow-up query runs
        for every tenant on every dispatch tick, and a correlated aggregate over
        the message log is not a query you want on that path.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE growth_leads SET last_outbound_at = now(), "
                    "updated_at = now() WHERE tenant_id = :tid AND id = :id"
                ),
                {"tid": tenant_id, "id": lead_id},
            )

    async def bump_follow_up(self, tenant_id: UUID, lead_id: UUID) -> int:
        async with tenant_scope(tenant_id) as session:
            value = (
                await session.execute(
                    text(
                        "UPDATE growth_leads SET follow_up_count = follow_up_count + 1, "
                        "last_outbound_at = now(), updated_at = now() "
                        "WHERE tenant_id = :tid AND id = :id RETURNING follow_up_count"
                    ),
                    {"tid": tenant_id, "id": lead_id},
                )
            ).scalar_one_or_none()
        return int(value or 0)

    async def pause_until(
        self, tenant_id: UUID, lead_id: UUID, until: datetime | None
    ) -> None:
        """Hold the cadence without changing the lead's status.

        An out-of-office reply is not a rejection and must not read as one on
        the board — so the pause is its own column and the lead stays `mailed`.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE growth_leads SET paused_until = :until, updated_at = now() "
                    "WHERE tenant_id = :tid AND id = :id"
                ),
                {"tid": tenant_id, "id": lead_id, "until": until},
            )

    async def due_follow_ups(
        self, tenant_id: UUID, *, before: datetime, max_follow_ups: int, limit: int = 50
    ) -> Rows:
        """Mailed leads whose next follow-up is due now.

        ``paused_until`` and ``follow_up_count`` are both in the predicate rather
        than filtered in Python, so a tenant with ten thousand leads reads the
        handful that are due instead of the page that happens to be first.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_leads "  # noqa: S608
                        "WHERE tenant_id = :tid AND status = 'mailed' "
                        "AND follow_up_count < :max "
                        "AND last_outbound_at IS NOT NULL AND last_outbound_at <= :before "
                        "AND (paused_until IS NULL OR paused_until <= now()) "
                        "ORDER BY last_outbound_at LIMIT :limit"
                    ),
                    {
                        "tid": tenant_id,
                        "before": before,
                        "max": max_follow_ups,
                        "limit": limit,
                    },
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def count_by_status(self, tenant_id: UUID) -> dict[str, int]:
        """The board's counters, in one scan rather than one query per column."""
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT status, count(*) AS n FROM growth_leads "
                        "WHERE tenant_id = :tid GROUP BY status"
                    ),
                    {"tid": tenant_id},
                )
            ).mappings().all()
        return {str(row["status"]): int(row["n"]) for row in rows}


class GrowthMessagesRepository:
    """The outreach log, and the queue the dispatcher drains."""

    async def add(
        self,
        tenant_id: UUID,
        *,
        lead_id: UUID | None,
        channel: str,
        direction: str,
        content: str,
        language: str | None = None,
        template_id: str | None = None,
        provider_id: str | None = None,
        status: str = "queued",
        campaign_id: UUID | None = None,
        customer_id: UUID | None = None,
        cost_micros: int = 0,
        send_after: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Append one message.

        Returns None when the provider id collided — a webhook redelivery, which
        is a success, not an error. ``ON CONFLICT DO NOTHING`` plus a null return
        is how idempotency is expressed everywhere else in this codebase, so the
        caller treats "already recorded" and "recorded" the same way.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO growth_messages (tenant_id, lead_id, channel, "
                        "direction, content, language, template_id, provider_id, status, "
                        "campaign_id, customer_id, cost_micros, send_after) "
                        "VALUES (:tid, :lead_id, "
                        ":channel, :direction, :content, :language, :template_id, "
                        ":provider_id, :status, :campaign_id, :customer_id, "
                        ":cost_micros, :send_after) "
                        "ON CONFLICT DO NOTHING "
                        "RETURNING id, lead_id, channel, direction, content, language, "
                        "template_id, provider_id, status, campaign_id, customer_id, cost_micros, "
                        "send_after, created_at"
                    ),
                    {
                        "tid": tenant_id,
                        "lead_id": lead_id,
                        "channel": channel,
                        "direction": direction,
                        "content": content,
                        "language": language,
                        "template_id": template_id,
                        "provider_id": provider_id,
                        "status": status,
                        "campaign_id": campaign_id,
                        "customer_id": customer_id,
                        "cost_micros": cost_micros,
                        "send_after": send_after,
                    },
                )
            ).mappings().first()

            # the inbox orders on this column, so it has to move whenever the
            # thread does — in the same scope as the insert, so a message that
            # exists is never invisible to the screen that lists it. Skipped on a
            # provider-id collision: that insert wrote nothing, and bumping the
            # timestamp for it would float a conversation nothing happened in
            if row is not None and row.get("lead_id") is not None:
                await session.execute(
                    text(
                        "UPDATE growth_leads SET last_message_at = now() "
                        "WHERE tenant_id = :tid AND id = :lead_id"
                    ),
                    {"tid": tenant_id, "lead_id": row["lead_id"]},
                )
        return dict(row) if row is not None else None

    async def for_lead_since(
        self,
        tenant_id: UUID,
        lead_id: UUID,
        since: datetime | None,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """One lead's real correspondence after ``since``, oldest first.

        The CRM push turns these into notes on the tenant's own record, so it
        wants what actually happened and not what was merely scheduled or
        attempted: a ``queued`` row is a draft the dispatcher has not sent
        yet, and a ``failed`` row is one that was cancelled or bounced.
        Writing either into somebody's CRM would put an email in their
        timeline that never left the building. Oldest first because the caller
        advances a timestamp cursor and a note it failed to write must be the
        last one attempted, not one in the middle.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, channel, direction, content, status, created_at "
                        "  FROM growth_messages "
                        " WHERE tenant_id = :tid AND lead_id = :lead_id "
                        "   AND status NOT IN ('queued', 'failed') "
                        "   AND (CAST(:since AS timestamptz) IS NULL OR created_at > CAST(:since AS timestamptz)) "
                        " ORDER BY created_at "
                        " LIMIT :limit"
                    ),
                    {
                        "tid": tenant_id,
                        "lead_id": lead_id,
                        "since": since,
                        "limit": max(1, limit),
                    },
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def due_queued(
        self, tenant_id: UUID, now: datetime, limit: int
    ) -> list[dict[str, Any]]:
        """Outbound messages this tenant may send, campaign and one-to-one work
        interleaved, oldest first within each.

        ``send_after`` NULL means "as soon as the window allows"; a value means a
        follow-up or a deliberately delayed send. The cap is applied by the
        caller, because the cap depends on how much was already sent today and
        that is a different query.

        A plain ``ORDER BY created_at`` starved one stage with the other: a
        campaign released to five thousand customers is five thousand rows all
        older than tomorrow's cold mail, so it would consume the entire daily
        cap and the outbound ladder would go quiet for a day — the tenant's
        replies and meetings stopping as a side effect of a marketing send
        nobody connected to it. Ranking within each class and ordering by that
        rank interleaves them: whichever class has work gets its share, and a
        class with nothing queued gives up nothing, because the other one's
        ranks simply fill the batch.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, lead_id, channel, content, language, template_id, "
                        "campaign_id, customer_id, created_at FROM ("
                        "  SELECT id, lead_id, channel, content, language, template_id, "
                        "  campaign_id, customer_id, created_at, "
                        "  row_number() OVER ("
                        "    PARTITION BY (campaign_id IS NOT NULL) ORDER BY created_at"
                        "  ) AS rn "
                        "  FROM growth_messages "
                        "  WHERE tenant_id = :tid AND status = 'queued' "
                        "  AND direction = 'out' "
                        "  AND (send_after IS NULL OR send_after <= :now)"
                        ") ranked ORDER BY rn, created_at LIMIT :limit"
                    ),
                    {"tid": tenant_id, "now": now, "limit": limit},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def count_sent_since(self, tenant_id: UUID, since: datetime) -> int:
        """How many outbound messages actually left since ``since``.

        Counts everything past ``queued``, including failures and bounces: a
        message that reached the provider consumed reputation whatever happened
        to it afterwards, and the daily cap exists to protect reputation.
        """
        async with tenant_scope(tenant_id) as session:
            value = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM growth_messages WHERE tenant_id = :tid "
                        "AND direction = 'out' AND status <> 'queued' "
                        "AND created_at >= :since"
                    ),
                    {"tid": tenant_id, "since": since},
                )
            ).scalar_one_or_none()
        return int(value or 0)

    async def has_queued(self, tenant_id: UUID, now: datetime) -> bool:
        async with tenant_scope(tenant_id) as session:
            value = (
                await session.execute(
                    text(
                        "SELECT 1 FROM growth_messages WHERE tenant_id = :tid "
                        "AND status = 'queued' AND direction = 'out' "
                        "AND (send_after IS NULL OR send_after <= :now) LIMIT 1"
                    ),
                    {"tid": tenant_id, "now": now},
                )
            ).scalar_one_or_none()
        return value is not None

    async def set_status(
        self,
        tenant_id: UUID,
        message_id: UUID,
        status: str,
        *,
        provider_id: str | None = None,
        cost_micros: int | None = None,
    ) -> None:
        sets = ["status = :status"]
        params: dict[str, Any] = {
            "tid": tenant_id,
            "id": message_id,
            "status": status,
        }
        if provider_id is not None:
            sets.append("provider_id = :provider_id")
            params["provider_id"] = provider_id
        if cost_micros is not None:
            sets.append("cost_micros = :cost_micros")
            params["cost_micros"] = cost_micros
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    f"UPDATE growth_messages SET {', '.join(sets)} "  # noqa: S608
                    "WHERE tenant_id = :tid AND id = :id"
                ),
                params,
            )

    async def mark_thread_replied(self, tenant_id: UUID, lead_id: UUID) -> int:
        """Flip this lead's delivered outbound rows to ``replied``.

        Called once when an inbound message arrives, so the reply-rate query in
        the reports is a status count rather than a join between inbound and
        outbound rows. ``queued`` is deliberately excluded: a message that has
        not left yet did not earn the reply, and marking it would take it out of
        the send queue by side effect.
        """
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    "UPDATE growth_messages SET status = 'replied' "
                    "WHERE tenant_id = :tid AND lead_id = :lead_id AND direction = 'out' "
                    "AND status IN ('sent', 'delivered', 'opened')"
                ),
                {"tid": tenant_id, "lead_id": lead_id},
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def cancel_queued_for_lead(self, tenant_id: UUID, lead_id: UUID) -> int:
        """Drop this lead's unsent outbound rows.

        The stop button behind every terminal signal — an unsubscribe, a "not
        interested", a contact deletion. Marked ``failed`` rather than deleted so
        the audit trail still shows a message was written and why it never went,
        which is what someone answering a complaint needs to see.
        """
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    "UPDATE growth_messages SET status = 'failed' "
                    "WHERE tenant_id = :tid AND lead_id = :lead_id "
                    "AND direction = 'out' AND status = 'queued'"
                ),
                {"tid": tenant_id, "lead_id": lead_id},
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def cancel_queued_for_campaign(self, tenant_id: UUID, campaign_id: UUID) -> int:
        """Drop a campaign's unsent rows. The stop button behind a rejection."""
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    "UPDATE growth_messages SET status = 'failed' "
                    "WHERE tenant_id = :tid AND campaign_id = :cid "
                    "AND direction = 'out' AND status = 'queued'"
                ),
                {"tid": tenant_id, "cid": campaign_id},
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def cancel_queued_for_customers(
        self, tenant_id: UUID, customer_ids: list[UUID]
    ) -> int:
        """Drop queued campaign rows for these customers. The opt-out path."""
        if not customer_ids:
            return 0
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    "UPDATE growth_messages SET status = 'failed' "
                    "WHERE tenant_id = :tid AND customer_id = ANY(:ids) "
                    "AND direction = 'out' AND status = 'queued'"
                ),
                {"tid": tenant_id, "ids": list(customer_ids)},
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def campaign_customer_ids(
        self, tenant_id: UUID, campaign_id: UUID
    ) -> set[str]:
        """Customer ids already queued for this campaign, any status.

        The release job's resume record: positional offsets lie when a member
        was skipped (deleted customer writes no row), but this set cannot.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT customer_id FROM growth_messages "
                        "WHERE tenant_id = :tid AND campaign_id = :cid "
                        "AND customer_id IS NOT NULL"
                    ),
                    {"tid": tenant_id, "cid": campaign_id},
                )
            ).scalars().all()
        return {str(v) for v in rows if v is not None}

    async def campaign_stats(self, tenant_id: UUID, campaign_id: UUID) -> dict[str, int]:
        """Per-status counts for one campaign's sends, for the results panel."""
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT status, count(*) AS n FROM growth_messages "
                        "WHERE tenant_id = :tid AND campaign_id = :cid "
                        "GROUP BY status"
                    ),
                    {"tid": tenant_id, "cid": campaign_id},
                )
            ).mappings().all()
        return {str(row["status"]): int(row["n"]) for row in rows}

    async def last_outbound(
        self, tenant_id: UUID, lead_id: UUID
    ) -> dict[str, Any] | None:
        """The most recent thing we said, which is what a follow-up must not repeat."""
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, channel, content, language, status, created_at "
                        "FROM growth_messages WHERE tenant_id = :tid AND lead_id = :lead_id "
                        "AND direction = 'out' ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"tid": tenant_id, "lead_id": lead_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def has_outbound_for_lead(self, tenant_id: UUID, lead_id: UUID) -> bool:
        """Has anything at all been written to this lead?

        The compose agent's idempotency check: a redelivered ``growth_compose``
        callback must not queue a second cold mail for the same lead.
        """
        async with tenant_scope(tenant_id) as session:
            value = (
                await session.execute(
                    text(
                        "SELECT 1 FROM growth_messages WHERE tenant_id = :tid "
                        "AND lead_id = :lead_id AND direction = 'out' LIMIT 1"
                    ),
                    {"tid": tenant_id, "lead_id": lead_id},
                )
            ).scalar_one_or_none()
        return value is not None

    async def inbox(
        self, tenant_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> Rows:
        """One row per lead with a thread, newest activity first.

        Ordered by ``growth_leads.last_message_at`` — a denormalised column this
        repository maintains on every insert — and *not* by an aggregate over the
        message log. That is what bounds the work to one page.

        The earlier shape ordered by the lateral subquery's own ``created_at``,
        which meant Postgres had to evaluate the per-lead subquery for every lead
        the tenant had ever messaged before it could sort and take fifty. Cost
        scaled with the tenant's whole conversation history on every page load,
        and got worse with ``offset``. Now the partial index on
        ``(tenant_id, last_message_at DESC)`` produces outer rows already in
        order, so the planner stops after this page and the lateral runs only for
        the rows actually returned — the same denormalisation, and the same
        reasoning, as ``last_outbound_at`` for the follow-up query.

        The lateral stays because the screen needs the latest message's *text and
        direction*, not just its timestamp, and a second denormalised column
        holding message bodies on the lead row would be a cache to keep correct.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT l.id AS lead_id, l.company, l.person, l.email, l.phone, "
                        "l.status, l.language, l.source, m.channel, m.direction, "
                        "m.content, m.status AS message_status, m.created_at "
                        "FROM growth_leads l "
                        "JOIN LATERAL ("
                        "  SELECT channel, direction, content, status, created_at "
                        "    FROM growth_messages "
                        "   WHERE tenant_id = l.tenant_id AND lead_id = l.id "
                        "   ORDER BY created_at DESC LIMIT 1"
                        ") m ON true "
                        "WHERE l.tenant_id = :tid AND l.last_message_at IS NOT NULL "
                        "ORDER BY l.last_message_at DESC LIMIT :limit OFFSET :offset"
                    ),
                    {"tid": tenant_id, "limit": limit, "offset": offset},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def thread_for_lead(
        self, tenant_id: UUID, lead_id: UUID, limit: int = 50
    ) -> list[dict[str, Any]]:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, channel, direction, content, language, status, "
                        "cost_micros, created_at FROM growth_messages "
                        "WHERE tenant_id = :tid AND lead_id = :lead_id "
                        "ORDER BY created_at DESC LIMIT :limit"
                    ),
                    {"tid": tenant_id, "lead_id": lead_id, "limit": limit},
                )
            ).mappings().all()
        return [dict(row) for row in rows]


class GrowthApprovalsRepository:
    """The HITL ledger: pending -> approved | rejected | edited | expired."""

    _COLUMNS = (
        "id, batch_id, feature, level, payload, status, decided_by, decided_at, "
        "expires_at, created_at"
    )

    async def create(
        self,
        tenant_id: UUID,
        *,
        batch_id: UUID,
        feature: str,
        level: int,
        payload: dict[str, Any],
        expires_at: datetime,
        status: str = "pending",
        decided_by: str | None = None,
    ) -> dict[str, Any]:
        decided_at = (
            datetime.now(UTC) if status != "pending" else None
        )
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO growth_approvals (tenant_id, batch_id, feature, "  # noqa: S608
                        "level, payload, status, decided_by, decided_at, expires_at) "
                        "VALUES (:tid, :batch_id, :feature, :level, CAST(:payload AS jsonb), "
                        ":status, :decided_by, :decided_at, :expires_at) "
                        f"RETURNING {self._COLUMNS}"
                    ),
                    {
                        "tid": tenant_id,
                        "batch_id": batch_id,
                        "feature": feature,
                        "level": level,
                        "payload": _dumps(payload),
                        "status": status,
                        "decided_by": decided_by,
                        "decided_at": decided_at,
                        "expires_at": expires_at,
                    },
                )
            ).mappings().first()
        return dict(row) if row is not None else {}

    async def get(self, tenant_id: UUID, approval_id: UUID) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_approvals "  # noqa: S608
                        "WHERE tenant_id = :tid AND id = :id"
                    ),
                    {"tid": tenant_id, "id": approval_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def pending_for(self, tenant_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_approvals "  # noqa: S608
                        "WHERE tenant_id = :tid AND status = 'pending' "
                        "ORDER BY expires_at LIMIT :limit"
                    ),
                    {"tid": tenant_id, "limit": limit},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def decide(
        self,
        tenant_id: UUID,
        approval_id: UUID,
        *,
        status: str,
        decided_by: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Record a decision, but only on a row that is still pending.

        The ``status = 'pending'`` predicate is the concurrency control: two
        owners pressing approve and reject at the same moment produce one winner
        and one None, instead of a last-write-wins race over a money action.
        """
        sets = [
            "status = :status",
            "decided_by = :decided_by",
            "decided_at = now()",
        ]
        params: dict[str, Any] = {
            "tid": tenant_id,
            "id": approval_id,
            "status": status,
            "decided_by": decided_by,
        }
        if payload is not None:
            sets.append("payload = CAST(:payload AS jsonb)")
            params["payload"] = _dumps(payload)

        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"UPDATE growth_approvals SET {', '.join(sets)} "  # noqa: S608
                        "WHERE tenant_id = :tid AND id = :id AND status = 'pending' "
                        f"RETURNING {self._COLUMNS}"
                    ),
                    params,
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def expire_due(self, tenant_id: UUID, now: datetime) -> list[dict[str, Any]]:
        """Flip every lapsed pending row for one tenant and return them.

        The rows come back rather than a count, because the owner is re-pinged
        once per expired batch and the alert needs to say which batch.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "UPDATE growth_approvals SET status = 'expired', "  # noqa: S608
                        "decided_by = 'system', decided_at = now() "
                        "WHERE tenant_id = :tid AND status = 'pending' "
                        "AND expires_at <= :now "
                        f"RETURNING {self._COLUMNS}"
                    ),
                    {"tid": tenant_id, "now": now},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def has_expired(self, tenant_id: UUID, now: datetime) -> bool:
        async with tenant_scope(tenant_id) as session:
            value = (
                await session.execute(
                    text(
                        "SELECT 1 FROM growth_approvals WHERE tenant_id = :tid "
                        "AND status = 'pending' AND expires_at <= :now LIMIT 1"
                    ),
                    {"tid": tenant_id, "now": now},
                )
            ).scalar_one_or_none()
        return value is not None


class GrowthAuditRepository:
    """``growth_agent_actions`` — one row per agent decision, forever."""

    async def record(
        self,
        tenant_id: UUID,
        *,
        agent: str,
        action: str,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        reason: str = "",
        cost_micros: int = 0,
    ) -> None:
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO growth_agent_actions (tenant_id, agent, action, input, "
                    "output, reason, cost_micros) VALUES (:tid, :agent, :action, "
                    "CAST(:input AS jsonb), CAST(:output AS jsonb), :reason, :cost)"
                ),
                {
                    "tid": tenant_id,
                    "agent": agent,
                    "action": action,
                    "input": _dumps(input or {}),
                    "output": _dumps(output or {}),
                    "reason": reason[:2000],
                    "cost": cost_micros,
                },
            )

    async def recent(
        self, tenant_id: UUID, *, agent: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        clause = "AND agent = :agent" if agent else ""
        params: dict[str, Any] = {"tid": tenant_id, "limit": limit}
        if agent:
            params["agent"] = agent
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, agent, action, input, output, reason, cost_micros, "  # noqa: S608
                        f"created_at FROM growth_agent_actions WHERE tenant_id = :tid {clause} "
                        "ORDER BY created_at DESC LIMIT :limit"
                    ),
                    params,
                )
            ).mappings().all()
        return [dict(row) for row in rows]


class GrowthConsentRepository:
    """Suppressions and consents — the two halves of "may we contact them"."""

    async def suppressions(
        self, tenant_id: UUID, addresses: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Suppression rows, optionally narrowed to a set of addresses.

        The gate itself is the pure ``is_suppressed`` in the domain layer; this
        only supplies it with rows. Narrowing is by address and never by channel,
        because opting out on one channel blocks all of them.
        """
        clause = "AND address = ANY(:addresses)" if addresses else ""
        params: dict[str, Any] = {"tid": tenant_id}
        if addresses:
            params["addresses"] = addresses
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT channel, address, reason, source, created_at "  # noqa: S608
                        f"FROM growth_suppressions WHERE tenant_id = :tid {clause}"
                    ),
                    params,
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def suppress(
        self,
        tenant_id: UUID,
        *,
        channel: str,
        address: str,
        reason: str = "",
        source: str = "",
    ) -> None:
        """Record a do-not-contact. Idempotent — asking twice is not an error.

        ``DO UPDATE`` rather than ``DO NOTHING`` so a later, better reason
        ("complained") replaces an earlier vague one ("manual"), which is what
        the support team reads when the person writes in asking why.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO growth_suppressions (tenant_id, channel, address, "
                    "reason, source) VALUES (:tid, :channel, :address, :reason, :source) "
                    "ON CONFLICT (tenant_id, channel, address) DO UPDATE SET "
                    "reason = EXCLUDED.reason, source = EXCLUDED.source"
                ),
                {
                    "tid": tenant_id,
                    "channel": channel,
                    "address": address,
                    "reason": reason,
                    "source": source,
                },
            )

    async def consent(
        self,
        tenant_id: UUID,
        *,
        channel: str,
        address: str,
        source: str = "",
        proof: dict[str, Any] | None = None,
    ) -> None:
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO growth_consents (tenant_id, channel, address, source, "
                    "proof) VALUES (:tid, :channel, :address, :source, "
                    "CAST(:proof AS jsonb)) "
                    "ON CONFLICT (tenant_id, channel, address) DO UPDATE SET "
                    "source = EXCLUDED.source, proof = EXCLUDED.proof, "
                    "opted_in_at = now(), revoked_at = NULL"
                ),
                {
                    "tid": tenant_id,
                    "channel": channel,
                    "address": address,
                    "source": source,
                    "proof": _dumps(proof) if proof is not None else None,
                },
            )

    async def has_consent(self, tenant_id: UUID, channel: str, address: str) -> bool:
        async with tenant_scope(tenant_id) as session:
            value = (
                await session.execute(
                    text(
                        "SELECT 1 FROM growth_consents WHERE tenant_id = :tid "
                        "AND channel = :channel AND address = :address "
                        "AND revoked_at IS NULL LIMIT 1"
                    ),
                    {"tid": tenant_id, "channel": channel, "address": address},
                )
            ).scalar_one_or_none()
        return value is not None

    async def revoke_consent(self, tenant_id: UUID, address: str) -> int:
        """Revoke on every channel at once, which is the only correct reading."""
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    "UPDATE growth_consents SET revoked_at = now() "
                    "WHERE tenant_id = :tid AND address = :address AND revoked_at IS NULL"
                ),
                {"tid": tenant_id, "address": address},
            )
        return int(getattr(result, "rowcount", 0) or 0)


class AgentModelConfigRepository:
    """Which LLM each agent runs on: platform defaults plus tenant overrides.

    Platform rows (``tenant_id IS NULL``) are read and written under
    ``admin_scope``; tenant rows under that tenant's ``tenant_scope``. Both work
    because of the one non-standard RLS policy in migration 016, and neither can
    see the other tenant's rows.
    """

    _COLUMNS = (
        "id, tenant_id, agent_key, role, backend, model, updated_by, "
        "created_at, updated_at"
    )

    async def effective(self, tenant_id: UUID | None) -> list[dict[str, Any]]:
        """Every row this caller can see: platform defaults, plus one tenant's.

        One query, not two, precisely because the policy already draws the line —
        asking twice and merging in Python would be a second, weaker copy of the
        rule that RLS is already enforcing.
        """
        sql = text(
            f"SELECT {self._COLUMNS} FROM agent_model_config ORDER BY agent_key, role, "  # noqa: S608
            "tenant_id NULLS FIRST"
        )
        if tenant_id is None:
            async with admin_scope() as session:
                rows = (await session.execute(sql)).mappings().all()
        else:
            async with tenant_scope(tenant_id) as session:
                rows = (await session.execute(sql)).mappings().all()
        return [dict(row) for row in rows]

    async def resolve(
        self, tenant_id: UUID, agent_key: str, role: str
    ) -> dict[str, Any] | None:
        """The one row that applies: tenant override first, platform default next.

        ``ORDER BY tenant_id NULLS LAST LIMIT 1`` does the precedence in the
        database. Doing it here rather than in the service means every caller
        gets the same answer even if a future caller forgets the rule.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM agent_model_config "  # noqa: S608
                        "WHERE agent_key = :agent_key AND role = :role "
                        "AND (tenant_id = :tid OR tenant_id IS NULL) "
                        "ORDER BY tenant_id NULLS LAST LIMIT 1"
                    ),
                    {"tid": tenant_id, "agent_key": agent_key, "role": role},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def put(
        self,
        tenant_id: UUID | None,
        *,
        agent_key: str,
        role: str,
        backend: str,
        model: str,
        updated_by: str,
    ) -> dict[str, Any]:
        """Set a platform default (``tenant_id`` None) or a tenant override.

        Two statements rather than one ``ON CONFLICT``, because the uniqueness of
        the platform half is enforced by a *partial* unique index and
        ``ON CONFLICT`` cannot name a predicate-carrying index without repeating
        the predicate. An explicit update-then-insert is shorter to read and, at
        this write volume — a staff member clicking a dropdown — the extra round
        trip costs nothing.
        """
        params = {
            "tid": tenant_id,
            "agent_key": agent_key,
            "role": role,
            "backend": backend,
            "model": model,
            "updated_by": updated_by,
        }
        match_clause = (
            "tenant_id IS NULL" if tenant_id is None else "tenant_id = :tid"
        )

        async def _write(session: Any) -> dict[str, Any]:
            row = (
                await session.execute(
                    text(
                        "UPDATE agent_model_config SET backend = :backend, model = :model, "  # noqa: S608
                        "updated_by = :updated_by, updated_at = now() "
                        f"WHERE agent_key = :agent_key AND role = :role AND {match_clause} "
                        f"RETURNING {self._COLUMNS}"
                    ),
                    params,
                )
            ).mappings().first()
            if row is not None:
                return dict(row)
            inserted = (
                await session.execute(
                    text(
                        "INSERT INTO agent_model_config (tenant_id, agent_key, role, "  # noqa: S608
                        "backend, model, updated_by) VALUES (:tid, :agent_key, :role, "
                        f":backend, :model, :updated_by) RETURNING {self._COLUMNS}"
                    ),
                    params,
                )
            ).mappings().first()
            return dict(inserted) if inserted is not None else {}

        if tenant_id is None:
            async with admin_scope() as session:
                return await _write(session)
        async with tenant_scope(tenant_id) as session:
            return await _write(session)

    async def delete(
        self, tenant_id: UUID | None, *, agent_key: str, role: str
    ) -> int:
        params = {"tid": tenant_id, "agent_key": agent_key, "role": role}
        match_clause = "tenant_id IS NULL" if tenant_id is None else "tenant_id = :tid"
        sql = text(
            "DELETE FROM agent_model_config "  # noqa: S608
            f"WHERE agent_key = :agent_key AND role = :role AND {match_clause}"
        )
        if tenant_id is None:
            async with admin_scope() as session:
                result = await session.execute(sql, params)
                return int(getattr(result, "rowcount", 0) or 0)
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(sql, params)
            return int(getattr(result, "rowcount", 0) or 0)


class ResearchRunsRepository:
    """``research_runs`` — the freshness clock, not a second lead store.

    ``growth_leads`` already answers "do we know this company". What it cannot
    answer is "did we already pay a provider to look them up recently", and that
    is the question that stops a re-run from re-billing Apollo for a company
    somebody researched last week.
    """

    async def record(
        self,
        tenant_id: UUID,
        *,
        query: str,
        source: str,
        subject: str = "",
        results: Any = None,
        lead_ids: list[UUID] | None = None,
    ) -> dict[str, Any]:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO research_runs (tenant_id, query, source, subject, "
                        "results, lead_ids) VALUES (:tid, :query, :source, :subject, "
                        "CAST(:results AS jsonb), :lead_ids) "
                        "RETURNING id, query, source, subject, lead_ids, created_at"
                    ),
                    {
                        "tid": tenant_id,
                        "query": query[:2000],
                        "source": source,
                        "subject": subject,
                        "results": _dumps(results if results is not None else {}),
                        "lead_ids": list(lead_ids or []),
                    },
                )
            ).mappings().first()
        return dict(row) if row is not None else {}

    async def fresh_subjects(
        self, tenant_id: UUID, subjects: list[str], since: datetime
    ) -> set[str]:
        """Which of these companies were already researched after ``since``.

        A set for the whole candidate batch rather than one query per company:
        a 25-lead run would otherwise open 25 scopes to answer a question one
        indexed scan answers, and the answer is used as a filter, not per row.
        """
        if not subjects:
            return set()
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT DISTINCT subject FROM research_runs "
                        "WHERE tenant_id = :tid AND subject = ANY(:subjects) "
                        "AND created_at >= :since"
                    ),
                    {"tid": tenant_id, "subjects": subjects, "since": since},
                )
            ).mappings().all()
        return {str(row["subject"]) for row in rows}

    async def recent(self, tenant_id: UUID, limit: int = 20) -> list[dict[str, Any]]:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, query, source, subject, lead_ids, created_at "
                        "FROM research_runs WHERE tenant_id = :tid "
                        "ORDER BY created_at DESC LIMIT :limit"
                    ),
                    {"tid": tenant_id, "limit": limit},
                )
            ).mappings().all()
        return [dict(row) for row in rows]


class GrowthLeadRawRepository:
    """``growth_lead_raw`` — every inbound payload, kept before it is believed.

    Written first and parsed second, so a malformed form post is a visible row
    with the body attached rather than a 500 in a log nobody kept.
    """

    async def store(
        self,
        tenant_id: UUID,
        *,
        source: str,
        payload: dict[str, Any],
        dedupe_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Archive one payload. None means this exact delivery was already seen.

        ``ON CONFLICT DO NOTHING`` on the provider's own idempotency handle: a
        redelivered webhook is a success that produces no second lead, which is
        the same shape ``growth_messages.add`` uses for the same reason.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO growth_lead_raw (tenant_id, source, payload, "
                        "dedupe_key) VALUES (:tid, :source, CAST(:payload AS jsonb), :key) "
                        "ON CONFLICT DO NOTHING "
                        "RETURNING id, source, payload, dedupe_key, received_at"
                    ),
                    {
                        "tid": tenant_id,
                        "source": source,
                        "payload": _dumps(payload),
                        "key": dedupe_key,
                    },
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def get(self, tenant_id: UUID, raw_id: UUID) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, source, payload, dedupe_key, lead_id, parse_error, "
                        "received_at FROM growth_lead_raw "
                        "WHERE tenant_id = :tid AND id = :id"
                    ),
                    {"tid": tenant_id, "id": raw_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def get_by_dedupe_key(
        self, tenant_id: UUID, source: str, dedupe_key: str
    ) -> dict[str, Any] | None:
        """The row a ``store`` collision left behind, for redelivery decisions.

        ``store`` returning None only says the key was seen, not that the
        enquiry became a lead: a job that died between the archive and the
        resolve leaves a row with neither ``lead_id`` nor ``parse_error``, and
        the redelivered job needs to look at that row to know whether to
        re-ingest (neither set), stop (lead already made) or stay stopped
        (already recorded as a parse failure). Served by the ``(tenant_id,
        source, dedupe_key)`` unique index 017 created for the collision.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, source, payload, dedupe_key, lead_id, parse_error, "
                        "received_at FROM growth_lead_raw "
                        "WHERE tenant_id = :tid AND source = :source "
                        "AND dedupe_key = :key"
                    ),
                    {"tid": tenant_id, "source": source, "key": dedupe_key},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def resolve(
        self,
        tenant_id: UUID,
        raw_id: UUID,
        *,
        lead_id: UUID | None = None,
        parse_error: str | None = None,
    ) -> None:
        """Attach the outcome to the archived payload.

        Both directions are recorded: the lead it produced, or why it produced
        none. A row with neither is a job that died between the two, which is
        itself the useful signal.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE growth_lead_raw SET lead_id = :lead_id, "
                    "parse_error = :parse_error WHERE tenant_id = :tid AND id = :id"
                ),
                {
                    "tid": tenant_id,
                    "id": raw_id,
                    "lead_id": lead_id,
                    "parse_error": parse_error[:1000] if parse_error else None,
                },
            )


class GrowthWaTemplatesRepository:
    """``growth_wa_templates`` — the tenant's Meta-approved template catalogue."""

    _COLUMNS = (
        "id, name, language, category, body, status, external_ref, "
        "created_at, updated_at"
    )

    async def list(self, tenant_id: UUID) -> list[dict[str, Any]]:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_wa_templates "  # noqa: S608
                        "WHERE tenant_id = :tid ORDER BY name, language"
                    ),
                    {"tid": tenant_id},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def approved(
        self, tenant_id: UUID, name: str, language: str
    ) -> dict[str, Any] | None:
        """The one template that may be sent, or None.

        Falls back to the tenant's English version of the same template when the
        requested language has none approved: Meta approves per (name,
        language), and a lead whose language we resolved to Hindi is better
        served an approved English utility message than no acknowledgement at
        all.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_wa_templates "  # noqa: S608
                        "WHERE tenant_id = :tid AND name = :name AND status = 'approved' "
                        "AND language IN (:language, 'en') "
                        "ORDER BY (language = :language) DESC LIMIT 1"
                    ),
                    {"tid": tenant_id, "name": name, "language": language},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def upsert(
        self,
        tenant_id: UUID,
        *,
        name: str,
        language: str,
        category: str,
        body: str,
        status: str,
        external_ref: str = "",
    ) -> dict[str, Any]:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO growth_wa_templates (tenant_id, name, language, "  # noqa: S608
                        "category, body, status, external_ref) VALUES (:tid, :name, "
                        ":language, :category, :body, :status, :ref) "
                        "ON CONFLICT (tenant_id, name, language) DO UPDATE SET "
                        "category = EXCLUDED.category, body = EXCLUDED.body, "
                        "status = EXCLUDED.status, external_ref = EXCLUDED.external_ref, "
                        f"updated_at = now() RETURNING {self._COLUMNS}"
                    ),
                    {
                        "tid": tenant_id,
                        "name": name,
                        "language": language,
                        "category": category,
                        "body": body,
                        "status": status,
                        "ref": external_ref,
                    },
                )
            ).mappings().first()
        return dict(row) if row is not None else {}

    async def mark_meta_status(
        self,
        tenant_id: UUID,
        *,
        status: str,
        external_ref: str = "",
        name: str = "",
        language: str = "",
    ) -> int:
        """Apply Meta's verdict to the catalogue row; return rows touched.

        Meta's ``message_template_status_update`` names the template three ways
        and any of them can be the only usable one. ``external_ref`` (Meta's own
        template id, stored at submission) is preferred because it is exact even
        when two of our languages map onto one Meta locale — ``hinglish`` and
        ``en`` both submit as ``en_US``. The (name, language) pair is the
        fallback for a row submitted before the id was recorded, and it accepts
        either the stored language or the locale's prefix, since the webhook
        speaks Meta's dialect and the row speaks ours.

        Returns a count rather than a row so the webhook can log honestly when
        an update arrived for a template this tenant never submitted.
        """
        if not external_ref and not name:
            return 0
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "UPDATE growth_wa_templates SET status = :status, updated_at = now() "
                        "WHERE tenant_id = :tid AND ("
                        "(:ref <> '' AND external_ref = :ref) "
                        "OR (:ref = '' AND :name <> '' AND name = :name AND language = :language)"
                        ") RETURNING id"
                    ),
                    {
                        "tid": tenant_id,
                        "status": status,
                        "ref": external_ref,
                        "name": name,
                        "language": language,
                    },
                )
            ).mappings().all()
        return len(rows)


async def tenant_for_waba(waba_id: str) -> UUID | None:
    """Which tenant owns this WhatsApp Business Account.

    Meta's template-status callback carries the WABA id on the entry and no
    phone number id, so the existing ``resolve_active_by_phone_number_id``
    routing cannot answer it. Like ``active_tenant_ids`` above, this runs under
    ``admin_scope`` before any tenant context exists — ``whatsapp_integrations``
    is reachable there, and everything the caller does afterwards happens inside
    the tenant's own scope.
    """
    if not waba_id:
        return None
    async with admin_scope() as session:
        row = (
            await session.execute(
                text(
                    "SELECT tenant_id FROM whatsapp_integrations "
                    "WHERE waba_id = :waba AND status = 'active' "
                    "ORDER BY created_at LIMIT 1"
                ),
                {"waba": waba_id},
            )
        ).mappings().first()
    return UUID(str(row["tenant_id"])) if row is not None else None


class GrowthReportsRepository:
    """``growth_reports`` — the report agent's ledger, one row per period."""

    _COLUMNS = (
        "id, tenant_id, period, period_start, numbers, pdf_path, summary, "
        "status, created_at, updated_at"
    )

    async def list(
        self, tenant_id: UUID, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_reports "  # noqa: S608
                        "WHERE tenant_id = :tid ORDER BY created_at DESC "
                        "LIMIT :limit OFFSET :offset"
                    ),
                    {"tid": tenant_id, "limit": limit, "offset": offset},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def get(self, tenant_id: UUID, report_id: UUID) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_reports "  # noqa: S608
                        "WHERE tenant_id = :tid AND id = :id"
                    ),
                    {"tid": tenant_id, "id": report_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def for_period(
        self, tenant_id: UUID, period: str, period_start: Any
    ) -> dict[str, Any] | None:
        """The existing row for this tenant and period, if any.

        The on-demand dedupe: a second run of the same period regenerates this
        row rather than inserting a second one.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_reports "  # noqa: S608
                        "WHERE tenant_id = :tid AND period = :period "
                        "AND period_start = :start"
                    ),
                    {"tid": tenant_id, "period": period, "start": period_start},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def create(
        self,
        tenant_id: UUID,
        *,
        period: str,
        period_start: Any,
    ) -> dict[str, Any]:
        """Insert the queued row, or return the existing one for this period.

        The insert races a double-fired cron tick or two near-simultaneous
        on-demand presses; the unique key makes the second writer lose, and the
        loser gets the winner's row back so both callers regenerate the same
        report rather than one of them failing.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO growth_reports (tenant_id, period, period_start) "  # noqa: S608
                        "VALUES (:tid, :period, :start) "
                        "ON CONFLICT (tenant_id, period, period_start) DO NOTHING "
                        f"RETURNING {self._COLUMNS}"
                    ),
                    {"tid": tenant_id, "period": period, "start": period_start},
                )
            ).mappings().first()
            if row is None:
                existing = (
                    await session.execute(
                        text(
                            f"SELECT {self._COLUMNS} FROM growth_reports "  # noqa: S608
                            "WHERE tenant_id = :tid AND period = :period "
                            "AND period_start = :start"
                        ),
                        {"tid": tenant_id, "period": period, "start": period_start},
                    )
                ).mappings().first()
                return dict(existing) if existing is not None else {}
        return dict(row)

    async def mark_ready(
        self,
        tenant_id: UUID,
        report_id: UUID,
        *,
        numbers: dict[str, Any],
        pdf_path: str,
        summary: str,
    ) -> None:
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE growth_reports SET numbers = CAST(:numbers AS jsonb), "
                    "pdf_path = :path, summary = :summary, status = 'ready', "
                    "updated_at = now() WHERE tenant_id = :tid AND id = :id"
                ),
                {
                    "tid": tenant_id,
                    "id": report_id,
                    "numbers": _dumps(numbers),
                    "path": pdf_path,
                    "summary": summary,
                },
            )

    async def mark_failed(self, tenant_id: UUID, report_id: UUID) -> None:
        """Mark a build failed — unless it already finished.

        The `status <> 'ready'` guard is load-bearing: delivery and the build
        share a row, and a late failure anywhere downstream must not retract a
        report whose PDF is already in storage and already in the owner's chat.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE growth_reports SET status = 'failed', "
                    "updated_at = now() WHERE tenant_id = :tid AND id = :id "
                    "AND status <> 'ready'"
                ),
                {"tid": tenant_id, "id": report_id},
            )


class GrowthCustomersRepository:
    """``growth_customers`` — buyers from stores and the CSV import.

    Keyed by the same normalised-identity uid the leads use, so the same
    person arriving from Shopify and from a CSV merges into one row.
    """

    _COLUMNS = (
        "id, uid, name, phone, email, tags, consent, language, "
        "first_order_at, last_order_at, total_spend_micros, order_count, "
        "created_at, updated_at"
    )

    async def upsert(self, tenant_id: UUID, customer: dict[str, Any]) -> dict[str, Any]:
        payload = dict(customer)
        payload["tid"] = tenant_id
        payload["tags"] = _dumps(customer.get("tags") or [])
        payload["consent"] = _dumps(customer.get("consent") or {})
        for key in ("uid", "name", "phone", "email"):
            payload.setdefault(key, "")
        payload.setdefault("language", None)
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO growth_customers (tenant_id, uid, name, phone, "  # noqa: S608
                        "email, tags, consent, language, first_order_at, last_order_at, "
                        "total_spend_micros, order_count) VALUES (:tid, :uid, :name, "
                        ":phone, :email, CAST(:tags AS jsonb), CAST(:consent AS jsonb), "
                        ":language, :first_order_at, :last_order_at, "
                        ":total_spend_micros, :order_count) "
                        "ON CONFLICT (tenant_id, uid) DO UPDATE SET "
                        "name = EXCLUDED.name, phone = EXCLUDED.phone, "
                        "email = EXCLUDED.email, tags = EXCLUDED.tags, "
                        "consent = EXCLUDED.consent, "
                        "language = COALESCE(EXCLUDED.language, "
                        "growth_customers.language), "
                        "first_order_at = LEAST(growth_customers.first_order_at, "
                        "EXCLUDED.first_order_at), "
                        "last_order_at = GREATEST(growth_customers.last_order_at, "
                        "EXCLUDED.last_order_at), "
                        "total_spend_micros = GREATEST("
                        "growth_customers.total_spend_micros, "
                        "EXCLUDED.total_spend_micros), "
                        "order_count = GREATEST(growth_customers.order_count, "
                        "EXCLUDED.order_count), "
                        "updated_at = now() "
                        f"RETURNING {self._COLUMNS}"
                    ),
                    {
                        "tid": payload["tid"],
                        "uid": payload["uid"],
                        "name": payload.get("name") or "",
                        "phone": payload.get("phone") or "",
                        "email": payload.get("email") or "",
                        "tags": payload["tags"],
                        "consent": payload["consent"],
                        "language": payload.get("language"),
                        "first_order_at": payload.get("first_order_at"),
                        "last_order_at": payload.get("last_order_at"),
                        "total_spend_micros": int(payload.get("total_spend_micros") or 0),
                        "order_count": int(payload.get("order_count") or 0),
                    },
                )
            ).mappings().first()
        return dict(row) if row is not None else {}

    async def list(
        self, tenant_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> Rows:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_customers "  # noqa: S608
                        "WHERE tenant_id = :tid ORDER BY last_order_at DESC NULLS LAST, "
                        "updated_at DESC LIMIT :limit OFFSET :offset"
                    ),
                    {"tid": tenant_id, "limit": limit, "offset": offset},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def all_for_segments(self, tenant_id: UUID, limit: int = 5000) -> Rows:
        """Every customer row the segment builder reads. Bounded, newest first."""
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_customers "  # noqa: S608
                        "WHERE tenant_id = :tid ORDER BY updated_at DESC LIMIT :limit"
                    ),
                    {"tid": tenant_id, "limit": limit},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def get(self, tenant_id: UUID, customer_id: UUID) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_customers "  # noqa: S608
                        "WHERE tenant_id = :tid AND id = :id"
                    ),
                    {"tid": tenant_id, "id": customer_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def get_by_uid(self, tenant_id: UUID, uid: str) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_customers "  # noqa: S608
                        "WHERE tenant_id = :tid AND uid = :uid"
                    ),
                    {"tid": tenant_id, "uid": uid},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def get_by_address(
        self, tenant_id: UUID, *, email: str = "", phone: str = ""
    ) -> dict[str, Any] | None:
        """The customer behind an address, for the opt-out path.

        The compliance stop must find buyers as well as leads: a queued
        campaign row points at ``customer_id``, and without this lookup an
        opt-out would stop the next mail but not the one already queued.
        """
        if not email and not phone:
            return None
        clauses = []
        params: dict[str, Any] = {"tid": tenant_id}
        if email:
            clauses.append("email = :email")
            params["email"] = email
        if phone:
            clauses.append("phone = :phone")
            params["phone"] = phone
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_customers "  # noqa: S608
                        f"WHERE tenant_id = :tid AND ({' OR '.join(clauses)}) "
                        "ORDER BY updated_at DESC LIMIT 1"
                    ),
                    params,
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def count(self, tenant_id: UUID) -> int:
        async with tenant_scope(tenant_id) as session:
            value = (
                await session.execute(
                    text("SELECT count(*) FROM growth_customers WHERE tenant_id = :tid"),
                    {"tid": tenant_id},
                )
            ).scalar_one_or_none()
        return int(value or 0)

    async def touch_order_stats(
        self,
        tenant_id: UUID,
        customer_id: UUID,
        *,
        ordered_at: Any,
        total_micros: int,
    ) -> None:
        """Fold one synced order into the customer's rollup columns.

        ``GREATEST`` keeps a re-synced old order from shrinking the totals; the
        orders table stays the exact record and these columns stay the fast one.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE growth_customers SET "
                    "order_count = order_count + 1, "
                    "total_spend_micros = total_spend_micros + :total, "
                    "first_order_at = LEAST(COALESCE(first_order_at, :at), :at), "
                    "last_order_at = GREATEST(COALESCE(last_order_at, :at), :at), "
                    "updated_at = now() WHERE tenant_id = :tid AND id = :id"
                ),
                {"tid": tenant_id, "id": customer_id, "at": ordered_at, "total": total_micros},
            )


class GrowthOrdersRepository:
    """``growth_orders`` — the money, one row per store order."""

    _COLUMNS = (
        "id, customer_id, external_id, store_provider, total_micros, payment, "
        "status, items, ordered_at, created_at"
    )

    async def upsert(self, tenant_id: UUID, order: dict[str, Any]) -> dict[str, Any]:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO growth_orders (tenant_id, customer_id, external_id, "  # noqa: S608
                        "store_provider, total_micros, payment, status, items, ordered_at) "
                        "VALUES (:tid, :customer_id, :external_id, :provider, :total, "
                        ":payment, :status, CAST(:items AS jsonb), :ordered_at) "
                        "ON CONFLICT (tenant_id, store_provider, external_id) DO UPDATE SET "
                        "customer_id = COALESCE(EXCLUDED.customer_id, growth_orders.customer_id), "
                        "total_micros = EXCLUDED.total_micros, payment = EXCLUDED.payment, "
                        "status = EXCLUDED.status, items = EXCLUDED.items, "
                        "ordered_at = EXCLUDED.ordered_at "
                        f"RETURNING {self._COLUMNS}, (xmax = 0) AS inserted"
                    ),
                    {
                        "tid": tenant_id,
                        "customer_id": order.get("customer_id"),
                        "external_id": order.get("external_id") or "",
                        "provider": order.get("store_provider") or "",
                        "total": int(order.get("total_micros") or 0),
                        "payment": order.get("payment") or "prepaid",
                        "status": order.get("status") or "",
                        "items": _dumps(order.get("items") or {}),
                        "ordered_at": order.get("ordered_at"),
                    },
                )
            ).mappings().first()
        return dict(row) if row is not None else {}

    async def for_customer(
        self, tenant_id: UUID, customer_id: UUID, limit: int = 200
    ) -> Rows:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_orders "  # noqa: S608
                        "WHERE tenant_id = :tid AND customer_id = :cid "
                        "ORDER BY ordered_at DESC NULLS LAST LIMIT :limit"
                    ),
                    {"tid": tenant_id, "cid": customer_id, "limit": limit},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def recent(self, tenant_id: UUID, limit: int = 2000) -> Rows:
        """Recent orders for the segment builder and the attribution join."""
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_orders "  # noqa: S608
                        "WHERE tenant_id = :tid ORDER BY ordered_at DESC NULLS LAST "
                        "LIMIT :limit"
                    ),
                    {"tid": tenant_id, "limit": limit},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def attributed_to_code(
        self, tenant_id: UUID, code: str, *, since: Any = None
    ) -> Rows:
        """Orders carrying this campaign's discount code, from ``since`` onward.

        Three things this is careful about, because attribution decides what the
        panel calls revenue and an over-count here is a lie about money:

        * **Time.** ``since`` is the campaign's ``created_at``; an order placed
          before the campaign existed cannot have been caused by it. A caller
          that passes no bound gets the whole history and says so by omission —
          ``CampaignService.results`` always passes one.
        * **Scope.** The match walks the JSON *string leaves* of ``items``
          (``jsonb_path_query`` over ``$.**``) instead of casting the row to
          text. Each store names the field differently — Shopify
          ``discount_codes[].code``, Woo ``coupon_lines[].code`` — so the leaf
          walk is what keeps this provider-agnostic without also matching a key
          name, a customer note or a product title that merely embeds the
          string. Woo lower-cases coupon codes, hence the case-insensitive
          compare.
        * **The code is caller-supplied.** ``planner.draft`` takes it from the
          spec, so a ``%`` or ``_`` in it would otherwise turn the pattern into
          a match-everything. Equality carries the common case; the escaped
          LIKE is the fallback for a leaf that wraps the code in other words.

        The UTM in the message link is ``utm_campaign={code}`` — the same
        string — so a store that records landing UTMs on the order is joined by
        this same scan. The code stays authoritative because it is the only
        handle the *store* validates: a UTM is whatever survived the click,
        while a redeemed code is the store's own word that this order used it.
        """
        escaped = code.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params: dict[str, Any] = {
            "tid": tenant_id,
            "code": code,
            "code_like": f"%{escaped}%",
        }
        clause = ""
        if since is not None:
            clause = "AND o.ordered_at >= :since"
            params["since"] = since
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT o.id, o.customer_id, o.external_id, o.store_provider, "  # noqa: S608
                        "o.total_micros, o.payment, o.status, o.items, o.ordered_at, "
                        "o.created_at FROM growth_orders o "
                        "WHERE o.tenant_id = :tid "
                        "AND EXISTS (SELECT 1 FROM jsonb_path_query(o.items, '$.**') AS leaf "
                        "WHERE jsonb_typeof(leaf) = 'string' AND ("
                        "lower(leaf #>> '{}') = lower(:code) "
                        "OR lower(leaf #>> '{}') LIKE lower(:code_like) ESCAPE '\\')) "
                        f"{clause} ORDER BY o.ordered_at DESC LIMIT 500"
                    ),
                    params,
                )
            ).mappings().all()
        return [dict(row) for row in rows]


class GrowthCampaignsRepository:
    """``growth_campaigns`` — drafts through sends plus their results."""

    _COLUMNS = (
        "id, trigger, name, segment, offer, channels, status, "
        "approval_id, discount_code, messages, results, created_at, updated_at"
    )

    async def create(
        self,
        tenant_id: UUID,
        *,
        trigger: str,
        name: str,
        segment: dict[str, Any],
        offer: dict[str, Any],
        channels: list[str],
        status: str = "draft",
        approval_id: UUID | None = None,
        discount_code: str | None = None,
        messages: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO growth_campaigns (tenant_id, trigger, name, segment, "  # noqa: S608
                        "offer, channels, status, approval_id, discount_code, messages) "
                        "VALUES (:tid, "
                        ":trigger, :name, CAST(:segment AS jsonb), CAST(:offer AS jsonb), "
                        ":channels, :status, :approval_id, :code, CAST(:messages AS jsonb)) "
                        f"RETURNING {self._COLUMNS}"
                    ),
                    {
                        "tid": tenant_id,
                        "trigger": trigger,
                        "name": name[:200],
                        "segment": _dumps(segment),
                        "offer": _dumps(offer),
                        "channels": list(channels),
                        "status": status,
                        "approval_id": approval_id,
                        "code": discount_code,
                        "messages": _dumps(messages or {}),
                    },
                )
            ).mappings().first()
        return dict(row) if row is not None else {}

    async def get(self, tenant_id: UUID, campaign_id: UUID) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_campaigns "  # noqa: S608
                        "WHERE tenant_id = :tid AND id = :id"
                    ),
                    {"tid": tenant_id, "id": campaign_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def get_by_approval(
        self, tenant_id: UUID, approval_id: UUID
    ) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_campaigns "  # noqa: S608
                        "WHERE tenant_id = :tid AND approval_id = :approval_id "
                        "ORDER BY updated_at DESC LIMIT 1"
                    ),
                    {"tid": tenant_id, "approval_id": approval_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def list(
        self, tenant_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> Rows:
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_campaigns "  # noqa: S608
                        "WHERE tenant_id = :tid ORDER BY created_at DESC "
                        "LIMIT :limit OFFSET :offset"
                    ),
                    {"tid": tenant_id, "limit": limit, "offset": offset},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def set_status(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        status: str,
        *,
        approval_id: UUID | None = None,
        discount_code: str | None = None,
        results: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        sets = ["status = :status", "updated_at = now()"]
        params: dict[str, Any] = {
            "tid": tenant_id,
            "id": campaign_id,
            "status": status,
        }
        if approval_id is not None:
            sets.append("approval_id = :approval_id")
            params["approval_id"] = approval_id
        if discount_code is not None:
            sets.append("discount_code = :code")
            params["code"] = discount_code
        if results is not None:
            sets.append("results = CAST(:results AS jsonb)")
            params["results"] = _dumps(results)
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"UPDATE growth_campaigns SET {', '.join(sets)} "  # noqa: S608
                        "WHERE tenant_id = :tid AND id = :id "
                        f"RETURNING {self._COLUMNS}"
                    ),
                    params,
                )
            ).mappings().first()
        return dict(row) if row is not None else None


class GrowthInsightsRepository:
    """``growth_insights`` — what the agent found, ranked, deduped, acted on."""

    _COLUMNS = (
        "id, uid, date, type, summary, why, suggested_action, action_payload, "
        "source_url, confidence, urgency, acted_on, created_at"
    )

    # urgency, then confidence, then recency — the panel's order and the
    # digest's, so the top of one is the top of the other and an owner who reads
    # the WhatsApp message and then opens the panel sees the same first item.
    _RANK = "ORDER BY urgency DESC, confidence DESC, created_at DESC"

    async def existing_uids(self, tenant_id: UUID, uids: Sequence[str]) -> set[str]:
        """Which of these uids this tenant already has.

        The dedup bar is checked here rather than relying on ``ON CONFLICT``
        alone, because a duplicate is not merely a wasted insert: it is a
        candidate that must not be counted toward the day's digest, and the
        conflict clause cannot tell the caller which rows it swallowed.
        """
        wanted = [u for u in dict.fromkeys(uids) if u]
        if not wanted:
            return set()
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT uid FROM growth_insights "
                        "WHERE tenant_id = :tid AND uid = ANY(:uids)"
                    ),
                    {"tid": tenant_id, "uids": wanted},
                )
            ).mappings().all()
        return {str(row["uid"]) for row in rows}

    async def create_many(self, tenant_id: UUID, items: Sequence[dict[str, Any]]) -> Rows:
        """Insert the day's survivors, skipping any uid already present.

        Returns the rows that were actually written, so the caller's digest is
        built from what landed rather than from what it hoped to land.
        """
        if not items:
            return []
        written: Rows = []
        async with tenant_scope(tenant_id) as session:
            for item in items:
                row = (
                    await session.execute(
                        text(
                            "INSERT INTO growth_insights (tenant_id, uid, type, summary, "  # noqa: S608
                            "why, suggested_action, action_payload, source_url, "
                            "confidence, urgency) VALUES (:tid, :uid, :type, :summary, "
                            ":why, :action, CAST(:payload AS jsonb), :source_url, "
                            ":confidence, :urgency) "
                            "ON CONFLICT (tenant_id, uid) DO NOTHING "
                            f"RETURNING {self._COLUMNS}"
                        ),
                        {
                            "tid": tenant_id,
                            "uid": str(item["uid"])[:128],
                            "type": str(item["type"]),
                            "summary": str(item["summary"])[:1000],
                            "why": str(item["why"])[:2000],
                            "action": str(item["suggested_action"])[:2000],
                            "payload": _dumps(item.get("action_payload") or {}),
                            "source_url": str(item["source_url"])[:2000],
                            "confidence": int(item["confidence"]),
                            "urgency": int(item["urgency"]),
                        },
                    )
                ).mappings().first()
                if row is not None:
                    written.append(dict(row))
        return written

    async def list(
        self,
        tenant_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        open_only: bool = False,
    ) -> Rows:
        clause = "AND acted_on IS NULL" if open_only else ""
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_insights "  # noqa: S608
                        f"WHERE tenant_id = :tid {clause} {self._RANK} "
                        "LIMIT :limit OFFSET :offset"
                    ),
                    {"tid": tenant_id, "limit": limit, "offset": offset},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def count(self, tenant_id: UUID, *, open_only: bool = False) -> int:
        clause = "AND acted_on IS NULL" if open_only else ""
        async with tenant_scope(tenant_id) as session:
            value = await session.scalar(
                text(
                    "SELECT count(*) FROM growth_insights "  # noqa: S608
                    f"WHERE tenant_id = :tid {clause}"
                ),
                {"tid": tenant_id},
            )
        return int(value or 0)

    async def get(self, tenant_id: UUID, insight_id: UUID) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_insights "  # noqa: S608
                        "WHERE tenant_id = :tid AND id = :id"
                    ),
                    {"tid": tenant_id, "id": insight_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def set_acted_on(
        self, tenant_id: UUID, insight_id: UUID, acted_on: str
    ) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "UPDATE growth_insights SET acted_on = :acted "  # noqa: S608
                        "WHERE tenant_id = :tid AND id = :id "
                        f"RETURNING {self._COLUMNS}"
                    ),
                    {"tid": tenant_id, "id": insight_id, "acted": acted_on},
                )
            ).mappings().first()
        return dict(row) if row is not None else None


class GrowthEventsRepository:
    """``growth_events`` — the calendar, shared rows plus tenant-specific ones.

    The one table in the growth schema whose rows may carry ``tenant_id NULL``,
    the same shape ``AgentModelConfigRepository`` already uses for platform
    defaults. Reads run under ``tenant_scope`` and see both halves because the
    policy's USING clause is widened; shared writes run under ``admin_scope``
    because the policy's WITH CHECK clause admits a NULL row only when there is
    no tenant in scope at all. The reasoning is on the table in
    ``migrations/020_growth_insights.sql``.
    """

    _COLUMNS = "id, tenant_id, date, region, category, name, source_url, created_at"

    async def upcoming(
        self, tenant_id: UUID, *, days: int = 45, limit: int = 50
    ) -> Rows:
        """The next ``days`` of calendar for this tenant: shared plus their own."""
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_events "  # noqa: S608
                        "WHERE (tenant_id = :tid OR tenant_id IS NULL) "
                        "AND date >= CURRENT_DATE "
                        "AND date < CURRENT_DATE + CAST(:days AS INT) "
                        "ORDER BY date LIMIT :limit"
                    ),
                    {"tid": tenant_id, "days": int(days), "limit": limit},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def add_shared(self, items: Sequence[dict[str, Any]]) -> int:
        """Insert calendar rows visible to every tenant. Runs under admin_scope."""
        if not items:
            return 0
        written = 0
        async with admin_scope() as session:
            for item in items:
                row = (
                    await session.execute(
                        text(
                            "INSERT INTO growth_events (tenant_id, date, region, "
                            "category, name, source_url) "
                            "VALUES (NULL, CAST(:date AS DATE), :region, :category, "
                            ":name, :source_url) "
                            "ON CONFLICT (date, region, name) WHERE tenant_id IS NULL "
                            "DO NOTHING RETURNING id"
                        ),
                        _event_params(item),
                    )
                ).mappings().first()
                if row is not None:
                    written += 1
        return written

    async def add_for_tenant(
        self, tenant_id: UUID, items: Sequence[dict[str, Any]]
    ) -> int:
        """Insert calendar rows only this tenant should see."""
        if not items:
            return 0
        written = 0
        async with tenant_scope(tenant_id) as session:
            for item in items:
                params = _event_params(item)
                params["tid"] = tenant_id
                row = (
                    await session.execute(
                        text(
                            "INSERT INTO growth_events (tenant_id, date, region, "
                            "category, name, source_url) "
                            "VALUES (:tid, CAST(:date AS DATE), :region, :category, "
                            ":name, :source_url) "
                            "ON CONFLICT (tenant_id, date, region, name) "
                            "WHERE tenant_id IS NOT NULL DO NOTHING RETURNING id"
                        ),
                        params,
                    )
                ).mappings().first()
                if row is not None:
                    written += 1
        return written


def _event_params(item: dict[str, Any]) -> dict[str, Any]:
    """Bind parameters for one calendar row, clamped to the column widths."""
    return {
        "date": str(item["date"]),
        "region": (str(item.get("region") or "IN"))[:32],
        "category": str(item["category"]),
        "name": str(item["name"])[:200],
        "source_url": (str(item.get("source_url") or ""))[:2000],
    }


class GrowthPageSnapshotsRepository:
    """``growth_page_snapshots`` — last seen state of each competitor URL.

    One row per (tenant, url), replaced in place. Deliberately not
    ``growth_integrations.health``: there is no integration row per competitor
    URL, ``health`` is written as a whole-column replace, and it already carries
    the store-sync cursors that a daily page snapshot would overwrite.
    """

    _COLUMNS = "id, url, content_hash, extract, fetched_at"

    async def get(self, tenant_id: UUID, url: str) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_page_snapshots "  # noqa: S608
                        "WHERE tenant_id = :tid AND url = :url"
                    ),
                    {"tid": tenant_id, "url": url},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def upsert(
        self, tenant_id: UUID, url: str, *, content_hash: str, extract: str
    ) -> dict[str, Any]:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO growth_page_snapshots (tenant_id, url, "  # noqa: S608
                        "content_hash, extract) VALUES (:tid, :url, :hash, :extract) "
                        "ON CONFLICT (tenant_id, url) DO UPDATE SET "
                        "content_hash = EXCLUDED.content_hash, "
                        "extract = EXCLUDED.extract, fetched_at = now() "
                        f"RETURNING {self._COLUMNS}"
                    ),
                    {
                        "tid": tenant_id,
                        "url": url[:2000],
                        "hash": content_hash[:128],
                        "extract": extract,
                    },
                )
            ).mappings().first()
        return dict(row) if row is not None else {}


class GrowthCrmLinksRepository:
    """``growth_crm_links`` — the receipt for every lead pushed to a CRM.

    One row per (tenant, provider, lead). The row is created *before* the first
    push, so a push that creates the CRM record and then loses our transaction
    still has something to reconcile against; the alternative — write the row
    only on success — loses the fact that we ever tried, and the next push
    creates a second contact.

    Nothing here reads a CRM. The whole stage is one-way, and this table is the
    only memory of what was sent.
    """

    _COLUMNS = (
        "id, provider, lead_id, external_id, synced_status, notes_through, "
        "last_synced_at, status, last_error, attempts, created_at, updated_at"
    )

    async def get(
        self, tenant_id: UUID, provider: str, lead_id: UUID
    ) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_crm_links "  # noqa: S608
                        "WHERE tenant_id = :tid AND provider = :provider "
                        "AND lead_id = :lead"
                    ),
                    {"tid": tenant_id, "provider": provider, "lead": lead_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else None

    async def for_leads(self, tenant_id: UUID, lead_ids: Sequence[UUID]) -> Rows:
        """Every link row for a page of leads, across all providers.

        The leads screen renders a page at a time and asks once, rather than
        once per lead: a per-lead lookup would be fifty round trips to decorate
        one list, which is how a list screen becomes the slowest page in the
        product.
        """
        if not lead_ids:
            return []
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_crm_links "  # noqa: S608
                        "WHERE tenant_id = :tid AND lead_id = ANY(:ids)"
                    ),
                    {"tid": tenant_id, "ids": list(lead_ids)},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def list(
        self,
        tenant_id: UUID,
        *,
        provider: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Rows:
        """The CRM links screen. Joined to the lead so the screen shows a name.

        Ordered failures-first (``status <> 'synced'``) then by recency: the
        rows an owner can do something about are the ones a sync screen exists
        to surface, and burying them under two thousand healthy rows is the
        same as not showing them.
        """
        clauses = ["l.tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id, "limit": limit, "offset": offset}
        if provider:
            clauses.append("l.provider = :provider")
            params["provider"] = provider
        if status:
            clauses.append("l.status = :status")
            params["status"] = status
        where = " AND ".join(clauses)
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT l.id, l.provider, l.lead_id, l.external_id, "  # noqa: S608
                        "l.synced_status, l.notes_through, l.last_synced_at, "
                        "l.status, l.last_error, l.attempts, l.created_at, "
                        "l.updated_at, "
                        "g.company AS lead_company, g.person AS lead_person, "
                        "g.status AS lead_status "
                        "FROM growth_crm_links l "
                        "JOIN growth_leads g ON g.id = l.lead_id "
                        f"WHERE {where} "
                        "ORDER BY (l.status = 'synced'), l.updated_at DESC "
                        "LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def count(
        self,
        tenant_id: UUID,
        *,
        provider: str | None = None,
        status: str | None = None,
    ) -> int:
        """Rows under the same filters ``list`` was called with.

        The links screen's "load more" needs the total to tell "the window
        ended" from "there are no more rows"; asking by fetching an empty page
        costs a round trip and cannot distinguish the two either.
        """
        clauses = ["tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id}
        if provider:
            clauses.append("provider = :provider")
            params["provider"] = provider
        if status:
            clauses.append("status = :status")
            params["status"] = status
        async with tenant_scope(tenant_id) as session:
            total = await session.execute(
                text(
                    "SELECT count(*) FROM growth_crm_links "  # noqa: S608
                    f"WHERE {' AND '.join(clauses)}"
                ),
                params,
            )
            return int(total.scalar() or 0)

    async def summary(self, tenant_id: UUID) -> Rows:
        """Per-provider counts by status, for the screen's header."""
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT provider, status, count(*) AS total "
                        "FROM growth_crm_links WHERE tenant_id = :tid "
                        "GROUP BY provider, status"
                    ),
                    {"tid": tenant_id},
                )
            ).mappings().all()
        return [dict(row) for row in rows]

    async def ensure(
        self, tenant_id: UUID, provider: str, lead_id: UUID
    ) -> dict[str, Any]:
        """The link row for this lead, creating a pending one if there is none.

        ``DO UPDATE SET provider = EXCLUDED.provider`` rather than ``DO NOTHING``
        because ``DO NOTHING`` returns no row on conflict, which would make the
        common path — the row already exists — a second query. The assignment is
        a no-op by construction: ``provider`` is part of the conflict target.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO growth_crm_links (tenant_id, provider, lead_id) "  # noqa: S608
                        "VALUES (:tid, :provider, :lead) "
                        "ON CONFLICT (tenant_id, provider, lead_id) DO UPDATE SET "
                        "provider = EXCLUDED.provider "
                        f"RETURNING {self._COLUMNS}"
                    ),
                    {"tid": tenant_id, "provider": provider, "lead": lead_id},
                )
            ).mappings().first()
        return dict(row) if row is not None else {}

    async def mark_synced(
        self,
        tenant_id: UUID,
        provider: str,
        lead_id: UUID,
        *,
        external_id: str,
        synced_status: str,
        notes_through: datetime | None = None,
    ) -> dict[str, Any]:
        """Record a successful push. Clears the error and the attempt counter.

        ``notes_through`` of None leaves the existing cursor alone: a push that
        updated the contact but logged no new notes must not rewind the note
        high-water mark and re-send every note the CRM already has.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "UPDATE growth_crm_links SET external_id = :ext, "  # noqa: S608
                        "synced_status = :synced, "
                        "notes_through = COALESCE(:notes, notes_through), "
                        "last_synced_at = now(), status = 'synced', last_error = '', "
                        "attempts = 0, updated_at = now() "
                        "WHERE tenant_id = :tid AND provider = :provider "
                        "AND lead_id = :lead "
                        f"RETURNING {self._COLUMNS}"
                    ),
                    {
                        "tid": tenant_id,
                        "provider": provider,
                        "lead": lead_id,
                        "ext": external_id[:200],
                        "synced": synced_status[:50],
                        "notes": notes_through,
                    },
                )
            ).mappings().first()
        return dict(row) if row is not None else {}

    async def mark_error(
        self, tenant_id: UUID, provider: str, lead_id: UUID, *, reason: str
    ) -> dict[str, Any]:
        """Record a refused push, and count it.

        ``attempts`` is what the retry backoff reads, so it increments here and
        only here — a caller that logged the error without counting it would
        produce a link that retries at full rate forever.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "UPDATE growth_crm_links SET status = 'error', "  # noqa: S608
                        "last_error = :reason, attempts = attempts + 1, "
                        "updated_at = now() "
                        "WHERE tenant_id = :tid AND provider = :provider "
                        "AND lead_id = :lead "
                        f"RETURNING {self._COLUMNS}"
                    ),
                    {
                        "tid": tenant_id,
                        "provider": provider,
                        "lead": lead_id,
                        "reason": reason[:1000],
                    },
                )
            ).mappings().first()
        return dict(row) if row is not None else {}

    async def mark_skipped(
        self, tenant_id: UUID, provider: str, lead_id: UUID, *, reason: str
    ) -> None:
        """A lead this CRM will never accept — no email and no phone, say.

        Distinct from ``error`` on purpose: an error is retried and shown as a
        problem, a skip is neither. Retrying a lead with nothing to identify it
        would burn a call an hour forever and put a permanent red row on the
        tenant's screen for something they cannot fix.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE growth_crm_links SET status = 'skipped', "
                    "last_error = :reason, updated_at = now() "
                    "WHERE tenant_id = :tid AND provider = :provider "
                    "AND lead_id = :lead"
                ),
                {
                    "tid": tenant_id,
                    "provider": provider,
                    "lead": lead_id,
                    "reason": reason[:1000],
                },
            )

    async def reset_external_id(
        self, tenant_id: UUID, provider: str, lead_id: UUID
    ) -> None:
        """The CRM deleted our record: forget its id, keep everything else.

        Called only from the ``CrmRecordGoneError`` path. The stored ``external_id``
        is dead weight — every push with it fails the same way — so it is
        blanked (which also frees the partial unique index slot, letting the
        recreate adopt whatever id the CRM hands back) and the row returns to
        ``pending`` with the attempt counter cleared: this is not a failure of
        the lead, and it must not inherit a failure's backoff.

        ``synced_status`` is blanked too, so the recreate is a full push rather
        than an unchanged-skip on a status the CRM no longer holds.
        ``notes_through`` is deliberately kept: the notes already logged refer
        to the deleted record, not the new one, but re-sending them is a
        duplicate the tenant has to delete by hand, while skipping them loses
        nothing the contact itself will not carry.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE growth_crm_links SET external_id = '', "
                    "synced_status = '', status = 'pending', attempts = 0, "
                    "last_error = '', updated_at = now() "
                    "WHERE tenant_id = :tid AND provider = :provider "
                    "AND lead_id = :lead"
                ),
                {"tid": tenant_id, "provider": provider, "lead": lead_id},
            )

    async def missing(
        self, tenant_id: UUID, provider: str, *, limit: int = 500
    ) -> Ids:
        """Leads with no link row for this provider — the connect-time backfill.

        Newest first: a tenant connecting a CRM to six months of leads wants this
        month's pipeline in their CRM today, and the cap is a cap, not a promise
        to reach the bottom of the table in one pass. The rows this misses are
        picked up the next time the owner presses Resync. Ordered by
        ``created_at`` rather than ``updated_at`` because
        ``(tenant_id, created_at)`` is an existing index on ``growth_leads`` —
        either reading of "newest" serves the backfill, and the other one would
        be a top-N sort over the tenant's whole lead table on every connect.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT g.id FROM growth_leads g "
                        "LEFT JOIN growth_crm_links l "
                        "ON l.lead_id = g.id AND l.provider = :provider "
                        "AND l.tenant_id = g.tenant_id "
                        "WHERE g.tenant_id = :tid AND l.id IS NULL "
                        "ORDER BY g.created_at DESC LIMIT :limit"
                    ),
                    {"tid": tenant_id, "provider": provider, "limit": limit},
                )
            ).scalars().all()
        return [UUID(str(r)) for r in rows]

    async def due(
        self, tenant_id: UUID, provider: str, *, limit: int = 50
    ) -> Rows:
        """Link rows worth another attempt, oldest first.

        Only ``pending`` and ``error`` — a ``synced`` row is re-pushed by a
        status change enqueuing it directly, not by this sweep, and ``skipped``
        is by definition never due. The backoff itself is applied in the
        service, which owns the exponent; this returns candidates.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._COLUMNS} FROM growth_crm_links "  # noqa: S608
                        "WHERE tenant_id = :tid AND provider = :provider "
                        "AND status IN ('pending', 'error') "
                        "ORDER BY updated_at LIMIT :limit"
                    ),
                    {"tid": tenant_id, "provider": provider, "limit": limit},
                )
            ).mappings().all()
        return [dict(row) for row in rows]


class RunContextRepository:
    """``run_context`` — the swarm's per-day working memory.

    Keyed by the tenant's *calendar day*, not a run id, because there is no
    run to key on: the growth platform is 7+ independent cron tasks, each
    enqueuing queue jobs whose work happens later in a worker, and a run_id
    minted in one cron never reaches the others (audit B1, reasoning on the
    table in ``migrations/024_run_context.sql``). Every growth job that runs
    for a tenant on a day appends to that day's row set, whatever cron
    started it — no coordination needed, which is exactly what a fan-out
    architecture requires.

    Append-only, by method as well as by schema: there is no update and no
    delete-by-run here. An agent revising its own finding appends a
    superseding entry and consumers take the latest per kind; ``sweep_old``
    is the only delete and it is an age cutoff hung off the rollup cron, so
    no crash can orphan rows a later job still wanted to write.
    """

    async def append(
        self,
        tenant_id: UUID,
        *,
        local_date: Any,
        agent_key: str,
        kind: str,
        payload: Any,
        run_id: str = "",
    ) -> dict[str, Any]:
        """Add one entry to the tenant's day. Never edits, never raises shape.

        ``local_date`` is the caller's to compute — it comes from the profile
        timezone, which this repo deliberately does not fetch: the services
        already hold settings and the repo stays a dumb, testable SQL layer.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "INSERT INTO run_context "
                        "(tenant_id, local_date, agent_key, kind, payload, run_id) "
                        "VALUES (:tid, :day, :agent, :kind, "
                        "CAST(:payload AS jsonb), :run_id) "
                        "RETURNING id, local_date, agent_key, kind, payload, "
                        "run_id, created_at"
                    ),
                    {
                        "tid": tenant_id,
                        "day": local_date,
                        "agent": agent_key,
                        "kind": kind,
                        "payload": _dumps(payload),
                        "run_id": run_id,
                    },
                )
            ).mappings().first()
        return dict(row) if row is not None else {}

    async def read_day(
        self, tenant_id: UUID, local_date: Any
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """The whole day, grouped agent_key -> kind -> entries, oldest first.

        The grouping is Python, not SQL, on purpose: ``jsonb_each``-style
        aggregation in the database saves nothing on a day's worth of rows and
        costs the caller the plain-row shape every other repo method returns.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, agent_key, kind, payload, created_at "
                        "FROM run_context WHERE tenant_id = :tid "
                        "AND local_date = :day ORDER BY created_at"
                    ),
                    {"tid": tenant_id, "day": local_date},
                )
            ).mappings().all()
        out: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for row in rows:
            entry = dict(row)
            payload = entry.get("payload")
            entry["payload"] = payload if isinstance(payload, dict) else {}
            out.setdefault(str(entry["agent_key"]), {}).setdefault(
                str(entry["kind"]), []
            ).append(entry)
        return out

    async def latest(
        self, tenant_id: UUID, local_date: Any, agent_key: str, kind: str
    ) -> dict[str, Any] | None:
        """The newest entry one agent left for one kind that day, or None.

        Append-only means "the current value of X" is "the last X"; this is
        the one-row version of ``read_day`` for consumers that care about a
        single fact rather than the day's story.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, agent_key, kind, payload, created_at "
                        "FROM run_context WHERE tenant_id = :tid "
                        "AND local_date = :day AND agent_key = :agent "
                        "AND kind = :kind ORDER BY created_at DESC LIMIT 1"
                    ),
                    {
                        "tid": tenant_id,
                        "day": local_date,
                        "agent": agent_key,
                        "kind": kind,
                    },
                )
            ).mappings().first()
        if row is None:
            return None
        entry = dict(row)
        payload = entry.get("payload")
        entry["payload"] = payload if isinstance(payload, dict) else {}
        return entry

    async def sweep_old(self, *, days: int = 7) -> dict[str, int]:
        """Delete entries older than the cutoff. The rollup cron's age sweep.

        Per-tenant scope, not one admin-scope DELETE, for the reason on
        ``active_tenant_ids``: ``run_context`` carries FORCE RLS, so under
        ``admin_scope`` — which sets no ``app.tenant_id``, leaving
        ``current_tenant_id()`` NULL — a cross-tenant delete would match no
        rows at all, on a correctly locked-down database, silently and
        forever. Iterating the RLS-free ``tenants`` table and sweeping inside
        each tenant's own scope is the established pattern, and a tenant
        deleted between listing and sweeping has already had its rows
        cascade-deleted with it.
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        deleted = 0
        tenants = await active_tenant_ids()
        for tenant_id in tenants:
            async with tenant_scope(tenant_id) as session:
                result = await session.execute(
                    text(
                        "DELETE FROM run_context WHERE tenant_id = :tid "
                        "AND created_at < :cutoff"
                    ),
                    {"tid": tenant_id, "cutoff": cutoff},
                )
                deleted += int(getattr(result, "rowcount", 0) or 0)
        return {"tenants": len(tenants), "deleted": deleted}


class GrowthRepository:
    """One handle the container hands out, holding the per-group repositories.

    A single container property rather than nine keeps the wiring honest: a
    service that needs leads and messages together — which is most of them — asks
    for ``container.growth`` once instead of accumulating constructor arguments
    that all point at the same database.
    """

    def __init__(self) -> None:
        self.settings = GrowthSettingsRepository()
        self.integrations = GrowthIntegrationsRepository()
        self.leads = GrowthLeadsRepository()
        self.messages = GrowthMessagesRepository()
        self.approvals = GrowthApprovalsRepository()
        self.audit = GrowthAuditRepository()
        self.consent = GrowthConsentRepository()
        self.models = AgentModelConfigRepository()
        self.research = ResearchRunsRepository()
        self.raw = GrowthLeadRawRepository()
        self.wa_templates = GrowthWaTemplatesRepository()
        self.reports = GrowthReportsRepository()
        self.customers = GrowthCustomersRepository()
        self.orders = GrowthOrdersRepository()
        self.campaigns = GrowthCampaignsRepository()
        self.insights = GrowthInsightsRepository()
        self.events = GrowthEventsRepository()
        self.page_snapshots = GrowthPageSnapshotsRepository()
        self.crm_links = GrowthCrmLinksRepository()
        self.run_context = RunContextRepository()

    async def purge_tenant(self, tenant_id: UUID) -> dict[str, int]:
        """Delete every growth row for one tenant. The compliance nuclear option.

        Ordered child-before-parent because ``growth_messages.lead_id`` cascades
        but the explicit order documents the dependency and survives someone
        later dropping the cascade. ``growth_settings`` is deleted last so a
        failure part-way through leaves the tenant visibly mid-deletion rather
        than looking like a tenant that never onboarded.
        """
        counts: dict[str, int] = {}
        tables = (
            "growth_messages",
            "growth_crm_links",
            "growth_reports",
            "growth_insights",
            "growth_page_snapshots",
            "growth_events",
            "growth_campaigns",
            "growth_orders",
            "growth_customers",
            "growth_agent_actions",
            "growth_approvals",
            "growth_lead_raw",
            "research_runs",
            "run_context",
            "growth_wa_templates",
            "growth_leads",
            "growth_consents",
            "growth_suppressions",
            "growth_integrations",
            "growth_settings",
        )
        async with tenant_scope(tenant_id) as session:
            for table in tables:
                result = await session.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :tid"),  # noqa: S608
                    {"tid": tenant_id},
                )
                counts[table] = int(getattr(result, "rowcount", 0) or 0)
        return counts

    async def purge_contact(self, tenant_id: UUID, addresses: list[str]) -> dict[str, int]:
        """Erase one person, everywhere, and leave a tombstone so they stay gone.

        The tombstone is written by the compliance service, not here: this method
        only removes, and a method that both deletes and writes a blocking row is
        one someone will eventually call expecting only the first half.
        """
        if not addresses:
            return {}
        counts: dict[str, int] = {}
        async with tenant_scope(tenant_id) as session:
            lead_ids = (
                await session.execute(
                    text(
                        "SELECT id FROM growth_leads WHERE tenant_id = :tid "
                        "AND (email = ANY(:addrs) OR phone = ANY(:addrs))"
                    ),
                    {"tid": tenant_id, "addrs": addresses},
                )
            ).scalars().all()

            if lead_ids:
                result = await session.execute(
                    text(
                        "DELETE FROM growth_messages WHERE tenant_id = :tid "
                        "AND lead_id = ANY(:ids)"
                    ),
                    {"tid": tenant_id, "ids": list(lead_ids)},
                )
                counts["growth_messages"] = int(getattr(result, "rowcount", 0) or 0)

                # The CRM link would cascade with the lead; it is deleted
                # explicitly so the erasure report says so out loud. What this
                # cannot do is erase the contact from the tenant's own CRM —
                # that record belongs to the tenant, who is its controller, and
                # a product that silently deleted rows in a customer's
                # Salesforce-shaped system of record because someone pressed
                # erase here would be indefensible. The compliance service
                # tells the owner, and the owner deletes it there.
                result = await session.execute(
                    text(
                        "DELETE FROM growth_crm_links WHERE tenant_id = :tid "
                        "AND lead_id = ANY(:ids)"
                    ),
                    {"tid": tenant_id, "ids": list(lead_ids)},
                )
                counts["growth_crm_links"] = int(getattr(result, "rowcount", 0) or 0)

                # the archived inbound payload holds everything the person typed
                # — name, phone, email, their enquiry — verbatim. growth_lead_raw
                # references the lead ON DELETE SET NULL, so deleting the lead
                # would leave the payload behind with nothing pointing at it: a
                # copy of the contact that survives their erasure request and
                # that no later delete could find. It goes with the lead.
                result = await session.execute(
                    text(
                        "DELETE FROM growth_lead_raw WHERE tenant_id = :tid "
                        "AND lead_id = ANY(:ids)"
                    ),
                    {"tid": tenant_id, "ids": list(lead_ids)},
                )
                counts["growth_lead_raw"] = int(getattr(result, "rowcount", 0) or 0)

            result = await session.execute(
                text(
                    "DELETE FROM growth_leads WHERE tenant_id = :tid "
                    "AND (email = ANY(:addrs) OR phone = ANY(:addrs))"
                ),
                {"tid": tenant_id, "addrs": addresses},
            )
            counts["growth_leads"] = int(getattr(result, "rowcount", 0) or 0)

            result = await session.execute(
                text(
                    "DELETE FROM growth_consents WHERE tenant_id = :tid "
                    "AND address = ANY(:addrs)"
                ),
                {"tid": tenant_id, "addrs": addresses},
            )
            counts["growth_consents"] = int(getattr(result, "rowcount", 0) or 0)

            customer_ids = (
                await session.execute(
                    text(
                        "SELECT id FROM growth_customers WHERE tenant_id = :tid "
                        "AND (email = ANY(:addrs) OR phone = ANY(:addrs))"
                    ),
                    {"tid": tenant_id, "addrs": addresses},
                )
            ).scalars().all()
            if customer_ids:
                result = await session.execute(
                    text(
                        "DELETE FROM growth_messages WHERE tenant_id = :tid "
                        "AND customer_id = ANY(:ids)"
                    ),
                    {"tid": tenant_id, "ids": list(customer_ids)},
                )
                counts["growth_messages_customer"] = int(getattr(result, "rowcount", 0) or 0)
                result = await session.execute(
                    text(
                        "DELETE FROM growth_orders WHERE tenant_id = :tid "
                        "AND customer_id = ANY(:ids)"
                    ),
                    {"tid": tenant_id, "ids": list(customer_ids)},
                )
                counts["growth_orders"] = int(getattr(result, "rowcount", 0) or 0)
                result = await session.execute(
                    text(
                        "DELETE FROM growth_customers WHERE tenant_id = :tid "
                        "AND id = ANY(:ids)"
                    ),
                    {"tid": tenant_id, "ids": list(customer_ids)},
                )
                counts["growth_customers"] = int(getattr(result, "rowcount", 0) or 0)

            # The two JSONB trails nothing else reaches. `research_runs.results`
            # holds whatever the search returned about this person, and
            # `growth_agent_actions.input/output` holds the addresses the
            # compose and reply paths were given. Neither is findable by a
            # foreign key, so an erasure that stops at the tables with one
            # leaves the contact's details sitting in two audit trails — which
            # is not an erasure, it is a move.
            #
            # `position(addr in ...) > 0` rather than LIKE: an address is
            # user-supplied text and a `%` or `_` in it would turn a pattern
            # into a wildcard. Exact substring, no metacharacters, no escaping
            # to get wrong.
            if lead_ids:
                await session.execute(
                    text(
                        "UPDATE research_runs "
                        "SET lead_ids = ARRAY("
                        "  SELECT x FROM unnest(lead_ids) AS x "
                        "  WHERE NOT (x = ANY(:ids))"
                        ") WHERE tenant_id = :tid AND lead_ids && :ids"
                    ),
                    {"tid": tenant_id, "ids": list(lead_ids)},
                )
            result = await session.execute(
                text(
                    "UPDATE research_runs SET results = COALESCE(("
                    "  SELECT jsonb_agg(entry) "
                    "  FROM jsonb_array_elements(results) AS entry "
                    "  WHERE NOT EXISTS ("
                    "    SELECT 1 FROM unnest(CAST(:addrs AS text[])) AS addr "
                    "    WHERE addr <> '' AND position(addr in entry::text) > 0"
                    "  )"
                    "), '[]'::jsonb) "
                    "WHERE tenant_id = :tid AND jsonb_typeof(results) = 'array' "
                    "AND EXISTS ("
                    "  SELECT 1 FROM unnest(CAST(:addrs AS text[])) AS addr "
                    "  WHERE addr <> '' AND position(addr in results::text) > 0"
                    ")"
                ),
                {"tid": tenant_id, "addrs": addresses},
            )
            counts["research_runs_scrubbed"] = int(getattr(result, "rowcount", 0) or 0)

            # The action rows themselves go, rather than being rewritten: the
            # subject of the audit is the person being erased, so what would be
            # left after scrubbing every address out of it is a record of
            # nothing about nobody.
            result = await session.execute(
                text(
                    "DELETE FROM growth_agent_actions WHERE tenant_id = :tid "
                    "AND EXISTS ("
                    "  SELECT 1 FROM unnest(CAST(:addrs AS text[])) AS addr "
                    "  WHERE addr <> '' AND ("
                    "    position(addr in COALESCE(input::text, '')) > 0 "
                    "    OR position(addr in COALESCE(output::text, '')) > 0"
                    "  )"
                    ")"
                ),
                {"tid": tenant_id, "addrs": addresses},
            )
            counts["growth_agent_actions"] = int(getattr(result, "rowcount", 0) or 0)
        return counts


def new_batch_id() -> UUID:
    """A fresh batch id. Here so callers do not each import uuid4 separately."""
    return uuid4()


def default_expiry(hours: int) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)
