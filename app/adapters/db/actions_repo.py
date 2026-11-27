"""Postgres access for the actions engine.

Raw `text()` SQL through SQLAlchemy Core under `tenant_scope`, like every other
repository here — RLS is the isolation mechanism, so the tenant context is what
makes a query safe rather than a `WHERE tenant_id = …` a future edit could drop.

Three things in this file are not ordinary CRUD and are the reason it exists:

**Promoted columns are always written from the definition.** `actions.definition`
is the authoritative JSONB and Pydantic owns its schema; the exploded columns
exist only so `ToolSet.resolve` can filter a turn's toolset in one indexed query
instead of validating twenty definitions per request. They are therefore derived
by `ActionDefinition.promoted_columns()` in the same statement and never accepted
from a caller — a promoted column edited independently is a filter that disagrees
with the thing it filters.

**Counters are derived, never incremented.** There is no `call_count` on
`actions`: an `UPDATE` per dispatch locks a hot row, and the Actions list needs
windowed metrics ("used 84 times this week") which a lifetime counter cannot
give. `last_called_at`, `last_error` and every rate come from `action_calls` on
the `(tenant_id, action_id, ts DESC)` index.

**The monthly quota is read from `usage_events`, not from `action_calls`.**
`usage_events` is the usage ledger and `action_calls` is pruned after 90 days;
counting the pruned table would make the quota disagree with the ledger.

The two cross-tenant sweepers (`prune_calls`, `expire_suspensions`) run under
`admin_scope()` because a cron job has no tenant. They return counts only.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from app.core.constants import UsageKind
from app.core.db import admin_scope, tenant_scope
from app.core.logging import get_logger
from app.core.security import decrypt_secret
from app.domain.actions import ActionDefinition
from sqlalchemy import text

logger = get_logger(__name__)

ACTION_COLUMNS = """
    id, tenant_id, name, display_name, description, definition, kind, effect,
    enabled, only_in_procedures, requires_confirmation, requires_verified_identity,
    channels, system_handler, target_host, library_key, setup_required, secret_ref,
    last_tested_at, last_test_status, version, created_by, created_at, updated_at
"""

# A tenant cannot be allowed to page an unbounded number of definitions into one
# response; the plan ceiling is 300 on enterprise, so this is headroom, not a
# functional limit.
MAX_ACTIONS_PAGE = 500


class ActionsRepo:
    """`actions`, `agent_actions`, `action_secrets`, `action_calls`,
    `action_suspensions`. No business rules, only storage."""

    # ------------------------------------------------------------------ actions

    async def create(
        self,
        tenant_id: UUID,
        definition: ActionDefinition,
        *,
        enabled: bool = False,
        library_key: str | None = None,
        setup_required: bool = False,
        created_by: UUID | None = None,
    ) -> dict[str, Any]:
        promoted = definition.promoted_columns()
        action_id = uuid4()
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""
                            INSERT INTO actions (
                                id, tenant_id, name, display_name, description,
                                definition, kind, effect, enabled,
                                only_in_procedures, requires_confirmation,
                                requires_verified_identity, channels,
                                system_handler, target_host, library_key,
                                setup_required, secret_ref, version, created_by
                            ) VALUES (
                                :id, :tenant_id, :name, :display_name, :description,
                                CAST(:definition AS jsonb), :kind, :effect, :enabled,
                                :only_in_procedures, :requires_confirmation,
                                :requires_verified_identity, :channels,
                                :system_handler, :target_host, :library_key,
                                :setup_required, :secret_ref, :version, :created_by
                            )
                            RETURNING {ACTION_COLUMNS}
                            """  # noqa: S608 - ACTION_COLUMNS is a module constant
                        ),
                        {
                            "id": action_id,
                            "tenant_id": tenant_id,
                            "definition": definition.model_dump_json(),
                            "enabled": enabled,
                            "library_key": library_key,
                            "setup_required": setup_required,
                            "created_by": created_by,
                            **promoted,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else {}

    async def update(
        self,
        tenant_id: UUID,
        action_id: UUID,
        definition: ActionDefinition,
        *,
        setup_required: bool | None = None,
    ) -> dict[str, Any] | None:
        """Rewrite the definition and every promoted column in one statement.

        `version` is bumped from the definition rather than in SQL so the stored
        JSONB and the column can never disagree about which version this is.
        """
        promoted = definition.promoted_columns()
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""
                            UPDATE actions SET
                                name = :name,
                                display_name = :display_name,
                                description = :description,
                                definition = CAST(:definition AS jsonb),
                                kind = :kind,
                                effect = :effect,
                                only_in_procedures = :only_in_procedures,
                                requires_confirmation = :requires_confirmation,
                                requires_verified_identity = :requires_verified_identity,
                                channels = :channels,
                                system_handler = :system_handler,
                                target_host = :target_host,
                                secret_ref = :secret_ref,
                                setup_required = COALESCE(:setup_required, setup_required),
                                version = :version,
                                updated_at = now()
                            WHERE id = :id
                            RETURNING {ACTION_COLUMNS}
                            """  # noqa: S608 - ACTION_COLUMNS is a module constant
                        ),
                        {
                            "id": action_id,
                            "definition": definition.model_dump_json(),
                            "setup_required": setup_required,
                            **promoted,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def get(self, tenant_id: UUID, action_id: UUID) -> dict[str, Any] | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(f"SELECT {ACTION_COLUMNS} FROM actions WHERE id = :id"),  # noqa: S608
                        {"id": action_id},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def get_by_name(self, tenant_id: UUID, name: str) -> dict[str, Any] | None:
        """One indexed lookup on `(tenant_id, name)`, the tool name the model used."""
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(f"SELECT {ACTION_COLUMNS} FROM actions WHERE name = :name"),  # noqa: S608
                        {"name": name},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def list_actions(
        self,
        tenant_id: UUID,
        *,
        enabled_only: bool = False,
        library_key: str | None = None,
        limit: int = MAX_ACTIONS_PAGE,
    ) -> list[dict[str, Any]]:
        clauses = ["TRUE"]
        params: dict[str, Any] = {"limit": min(limit, MAX_ACTIONS_PAGE)}
        if enabled_only:
            clauses.append("enabled")
        if library_key is not None:
            clauses.append("library_key = :library_key")
            params["library_key"] = library_key
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"SELECT {ACTION_COLUMNS} FROM actions "  # noqa: S608
                            f"WHERE {' AND '.join(clauses)} "
                            "ORDER BY display_name LIMIT :limit"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def resolve_for_turn(
        self, tenant_id: UUID, agent_id: UUID, *, channel: str
    ) -> list[dict[str, Any]]:
        """The per-turn toolset query: enabled, linked to this agent, this channel.

        One indexed query rather than loading definitions and filtering in Python,
        because it runs on every chat request for an actions-enabled agent. The
        channel test is on the promoted `channels` array so an action a channel
        cannot render is never even validated.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT {ACTION_COLUMNS}
                              FROM actions a
                              JOIN agent_actions link ON link.action_id = a.id
                             WHERE link.agent_id = :agent_id
                               AND a.enabled
                               AND NOT a.setup_required
                               AND :channel = ANY (a.channels)
                             ORDER BY a.name
                            """  # noqa: S608 - ACTION_COLUMNS is a module constant
                        ),
                        {"agent_id": agent_id, "channel": channel},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def set_enabled(self, tenant_id: UUID, action_id: UUID, enabled: bool) -> bool:
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    "UPDATE actions SET enabled = :enabled, updated_at = now() "
                    "WHERE id = :id"
                ),
                {"id": action_id, "enabled": enabled},
            )
        return bool(getattr(result, "rowcount", 0) or 0)

    async def delete(self, tenant_id: UUID, action_id: UUID) -> bool:
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text("DELETE FROM actions WHERE id = :id"), {"id": action_id}
            )
        return bool(getattr(result, "rowcount", 0) or 0)

    async def record_test(
        self, tenant_id: UUID, action_id: UUID, *, status: str
    ) -> None:
        """`last_tested_at` gates the Save button, so it lives on the row.

        Only the rare test endpoint writes it, which is why a column here does not
        have the hot-row problem a call counter would.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE actions SET last_tested_at = now(), "
                    "last_test_status = :status WHERE id = :id"
                ),
                {"id": action_id, "status": status},
            )

    async def count_actions(self, tenant_id: UUID) -> int:
        """For the `max_actions` plan ceiling, checked before a create."""
        async with tenant_scope(tenant_id) as session:
            value = (
                await session.execute(text("SELECT count(*) FROM actions"))
            ).scalar_one()
        return int(value or 0)

    # ------------------------------------------------------------ agent_actions

    async def link_agent(self, tenant_id: UUID, agent_id: UUID, action_id: UUID) -> None:
        """Idempotent: the toggle in the UI is not a transaction the user can see."""
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO agent_actions (tenant_id, agent_id, action_id) "
                    "VALUES (:tenant_id, :agent_id, :action_id) "
                    "ON CONFLICT (agent_id, action_id) DO NOTHING"
                ),
                {"tenant_id": tenant_id, "agent_id": agent_id, "action_id": action_id},
            )

    async def unlink_agent(self, tenant_id: UUID, agent_id: UUID, action_id: UUID) -> bool:
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    "DELETE FROM agent_actions "
                    "WHERE agent_id = :agent_id AND action_id = :action_id"
                ),
                {"agent_id": agent_id, "action_id": action_id},
            )
        return bool(getattr(result, "rowcount", 0) or 0)

    async def set_agent_links(
        self, tenant_id: UUID, agent_id: UUID, action_ids: list[UUID]
    ) -> None:
        """Replace an agent's whole link set in one transaction.

        The Actions screen sends the full selection, so a delete-then-insert is
        the honest translation of it. Both statements share the session, so a
        failure on the insert cannot leave the agent with no actions at all.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text("DELETE FROM agent_actions WHERE agent_id = :agent_id"),
                {"agent_id": agent_id},
            )
            for action_id in action_ids:
                await session.execute(
                    text(
                        "INSERT INTO agent_actions (tenant_id, agent_id, action_id) "
                        "VALUES (:tenant_id, :agent_id, :action_id) "
                        "ON CONFLICT (agent_id, action_id) DO NOTHING"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "agent_id": agent_id,
                        "action_id": action_id,
                    },
                )

    async def agent_ids_for_action(self, tenant_id: UUID, action_id: UUID) -> list[UUID]:
        """Which agents lose a tool if this action is deleted — shown before a delete."""
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text("SELECT agent_id FROM agent_actions WHERE action_id = :action_id"),
                    {"action_id": action_id},
                )
            ).all()
        return [row[0] for row in rows]

    # ----------------------------------------------------------- action_secrets

    async def set_secret_ref(
        self, tenant_id: UUID, action_id: UUID, name: str, secret_ref: str
    ) -> None:
        """Bind `{{secret.NAME}}` to a row in `tenant_secrets`.

        Only the reference is stored. The ciphertext stays in `tenant_secrets`
        under one encryption path, so there is exactly one place a credential can
        be decrypted and exactly one place to rotate.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "INSERT INTO action_secrets (tenant_id, action_id, name, secret_ref) "
                    "VALUES (:tenant_id, :action_id, :name, :secret_ref) "
                    "ON CONFLICT (action_id, name) DO UPDATE SET secret_ref = :secret_ref"
                ),
                {
                    "tenant_id": tenant_id,
                    "action_id": action_id,
                    "name": name,
                    "secret_ref": secret_ref,
                },
            )

    async def delete_secret_ref(self, tenant_id: UUID, action_id: UUID, name: str) -> bool:
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    "DELETE FROM action_secrets "
                    "WHERE action_id = :action_id AND name = :name"
                ),
                {"action_id": action_id, "name": name},
            )
        return bool(getattr(result, "rowcount", 0) or 0)

    async def secret_names(self, tenant_id: UUID, action_id: UUID) -> list[str]:
        """Names only. There is no repo method that returns a plaintext credential;
        decryption lives in `ActionSecretResolver` and its result never persists."""
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT name FROM action_secrets WHERE action_id = :action_id "
                        "ORDER BY name"
                    ),
                    {"action_id": action_id},
                )
            ).all()
        return [str(row[0]) for row in rows]

    # ------------------------------------------------------------- action_calls

    async def record_call(
        self,
        tenant_id: UUID,
        *,
        action_name: str,
        status: str,
        action_id: UUID | None = None,
        agent_id: UUID | None = None,
        conversation_id: UUID | None = None,
        message_id: UUID | None = None,
        procedure_run_id: UUID | None = None,
        http_status: int | None = None,
        duration_ms: int | None = None,
        request_bytes: int | None = None,
        response_bytes: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> UUID | None:
        """Append one ledger row. Never raises into a dispatch.

        A failed ledger write must not turn a successful side effect into an error
        the model retries, so this swallows and logs. `action_name` is denormalised
        because `action_id` is `ON DELETE SET NULL` — history has to survive the
        action being deleted, which is exactly when someone asks what it did.

        `error_message` is truncated here rather than at the call site: the ledger
        is read by the dashboard, and a provider that returns a megabyte of HTML
        in its error body should not put a megabyte in a Postgres row.
        """
        try:
            async with tenant_scope(tenant_id) as session:
                row = (
                    (
                        await session.execute(
                            text(
                                """
                                INSERT INTO action_calls (
                                    tenant_id, action_id, action_name, agent_id,
                                    conversation_id, message_id, procedure_run_id,
                                    status, http_status, duration_ms, request_bytes,
                                    response_bytes, error_code, error_message
                                ) VALUES (
                                    :tenant_id, :action_id, :action_name, :agent_id,
                                    :conversation_id, :message_id, :procedure_run_id,
                                    :status, :http_status, :duration_ms, :request_bytes,
                                    :response_bytes, :error_code, :error_message
                                )
                                RETURNING id
                                """
                            ),
                            {
                                "tenant_id": tenant_id,
                                "action_id": action_id,
                                "action_name": action_name,
                                "agent_id": agent_id,
                                "conversation_id": conversation_id,
                                "message_id": message_id,
                                "procedure_run_id": procedure_run_id,
                                "status": status,
                                "http_status": http_status,
                                "duration_ms": duration_ms,
                                "request_bytes": request_bytes,
                                "response_bytes": response_bytes,
                                "error_code": error_code,
                                "error_message": (
                                    error_message[:500] if error_message else None
                                ),
                            },
                        )
                    )
                    .mappings()
                    .first()
                )
        except Exception:
            logger.warning(
                "action_ledger_write_failed",
                tenant_id=str(tenant_id),
                action_name=action_name,
                status=status,
            )
            return None
        return row["id"] if row else None

    async def calls_in_conversation(
        self, tenant_id: UUID, conversation_id: UUID
    ) -> dict[str, int]:
        """The per-conversation ceilings, read as two numbers in one query.

        Only successful calls count: a call the SSRF guard refused did nothing, so
        charging it against the visitor's budget would let a misconfigured action
        silently disable every other one.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT count(*) FILTER (WHERE c.status = 'ok') AS calls,
                                   count(*) FILTER (
                                       WHERE c.status = 'ok'
                                         AND a.effect = 'write'
                                   ) AS writes
                              FROM action_calls c
                              LEFT JOIN actions a ON a.id = c.action_id
                             WHERE c.conversation_id = :conversation_id
                            """
                        ),
                        {"conversation_id": conversation_id},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return {"calls": 0, "writes": 0}
        return {"calls": int(row["calls"] or 0), "writes": int(row["writes"] or 0)}

    async def record_call_usage(
        self,
        tenant_id: UUID,
        *,
        action_name: str,
        conversation_id: UUID | None = None,
        quantity: int = 1,
    ) -> None:
        """Meter one action call into `usage_events`.

        Written here rather than through the conversation repo because usage
        metering belongs beside the ledger that recorded the call. Swallows on
        failure — an unmetered call is a rounding error, a raised exception after
        a live side effect is a duplicate charge on the tenant's own provider
        when the model retries.
        """
        try:
            async with tenant_scope(tenant_id) as session:
                await session.execute(
                    text(
                        "INSERT INTO usage_events (tenant_id, kind, quantity, meta) "
                        "VALUES (:tenant_id, :kind, :quantity, CAST(:meta AS jsonb))"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "kind": UsageKind.ACTION_CALL.value,
                        "quantity": quantity,
                        "meta": json.dumps(
                            {
                                "action": action_name,
                                "conversation_id": (
                                    str(conversation_id) if conversation_id else None
                                ),
                            }
                        ),
                    },
                )
        except Exception:
            logger.warning(
                "action_usage_write_failed",
                tenant_id=str(tenant_id),
                action_name=action_name,
            )

    async def call_metrics(
        self, tenant_id: UUID, action_id: UUID, *, days: int = 7
    ) -> dict[str, Any]:
        """The numbers on one action's card, over the `(tenant_id, action_id, ts DESC)`
        index. p95 by `percentile_disc` so it is an observed latency, not a
        synthetic average of two very different failure modes."""
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT count(*) AS calls,
                                   count(*) FILTER (WHERE status = 'ok') AS ok,
                                   max(ts) AS last_called_at,
                                   percentile_disc(0.95) WITHIN GROUP (
                                       ORDER BY duration_ms
                                   ) AS p95_ms,
                                   (
                                       SELECT error_message FROM action_calls
                                        WHERE action_id = :action_id
                                          AND status <> 'ok'
                                        ORDER BY ts DESC LIMIT 1
                                   ) AS last_error
                              FROM action_calls
                             WHERE action_id = :action_id
                               AND ts >= now() - make_interval(days => :days)
                            """
                        ),
                        {"action_id": action_id, "days": days},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return {"calls": 0, "ok": 0, "last_called_at": None, "p95_ms": None,
                    "last_error": None}
        return dict(row)

    async def recent_calls(
        self, tenant_id: UUID, *, action_id: UUID | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """The activity log. No response bodies are stored, so this is safe to
        show a tenant admin without a PII review."""
        clause = "action_id = :action_id" if action_id is not None else "TRUE"
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT id, action_id, action_name, conversation_id, "  # noqa: S608
                            "status, http_status, duration_ms, error_code, "
                            "error_message, ts "
                            f"FROM action_calls WHERE {clause} "
                            "ORDER BY ts DESC LIMIT :limit"
                        ),
                        {"action_id": action_id, "limit": min(limit, 200)},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    # -------------------------------------------------------- action_suspensions

    async def create_suspension(
        self,
        tenant_id: UUID,
        *,
        conversation_id: UUID,
        action_name: str,
        kind: str,
        call_id: str,
        arguments: dict[str, Any],
        resume_state: dict[str, Any],
        ttl_seconds: int,
        agent_id: UUID | None = None,
        action_id: UUID | None = None,
    ) -> dict[str, Any] | None:
        """Park a turn until the browser or the visitor answers.

        `ON CONFLICT (conversation_id, call_id)` returns the existing row instead
        of erroring: a widget that retries the same SSE connect must not create a
        second pending confirmation for one tool call, or a visitor who says yes
        once resolves only half of it.

        `expires_at` is computed in Postgres so a machine with a skewed clock
        cannot write a suspension that is already expired or never expires.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            INSERT INTO action_suspensions (
                                tenant_id, conversation_id, agent_id, action_id,
                                action_name, kind, call_id, arguments, resume_state,
                                expires_at
                            ) VALUES (
                                :tenant_id, :conversation_id, :agent_id, :action_id,
                                :action_name, :kind, :call_id,
                                CAST(:arguments AS jsonb), CAST(:resume_state AS jsonb),
                                now() + make_interval(secs => :ttl_seconds)
                            )
                            ON CONFLICT (conversation_id, call_id) DO UPDATE
                                SET resume_state = EXCLUDED.resume_state
                            RETURNING id, conversation_id, action_id, action_name, kind,
                                      call_id, arguments, resume_state, status,
                                      created_at, expires_at
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "conversation_id": conversation_id,
                            "agent_id": agent_id,
                            "action_id": action_id,
                            "action_name": action_name,
                            "kind": kind,
                            "call_id": call_id,
                            "arguments": json.dumps(arguments),
                            "resume_state": json.dumps(resume_state),
                            "ttl_seconds": ttl_seconds,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def get_suspension(
        self, tenant_id: UUID, conversation_id: UUID, call_id: str
    ) -> dict[str, Any] | None:
        """Read by the natural key the client knows. Expiry is evaluated here
        rather than trusted from `status`, because the sweeper runs on a cron and a
        row can be past `expires_at` while still marked pending."""
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT id, conversation_id, agent_id, action_id, action_name,
                                   kind, call_id, arguments, resume_state, status, result,
                                   created_at, resolved_at, expires_at,
                                   (expires_at <= now()) AS is_expired
                              FROM action_suspensions
                             WHERE conversation_id = :conversation_id
                               AND call_id = :call_id
                            """
                        ),
                        {"conversation_id": conversation_id, "call_id": call_id},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def pending_suspensions(
        self, tenant_id: UUID, conversation_id: UUID
    ) -> list[dict[str, Any]]:
        """What this conversation is still waiting on — used to refuse a second
        pending write and to re-render the confirmation card on reconnect."""
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT id, action_id, action_name, kind, call_id, arguments,
                                   resume_state, created_at, expires_at
                              FROM action_suspensions
                             WHERE conversation_id = :conversation_id
                               AND status = 'pending'
                               AND expires_at > now()
                             ORDER BY created_at
                            """
                        ),
                        {"conversation_id": conversation_id},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def resolve_suspension(
        self,
        tenant_id: UUID,
        conversation_id: UUID,
        call_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Single-winner transition. `status = 'pending'` in the WHERE clause is
        the whole concurrency control: the browser answering twice, or answering
        as the expiry sweeper fires, updates zero rows the second time and gets
        `None` — so a client action cannot be executed twice from one suspension.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            UPDATE action_suspensions
                               SET status = :status,
                                   result = CAST(:result AS jsonb),
                                   resolved_at = now()
                             WHERE conversation_id = :conversation_id
                               AND call_id = :call_id
                               AND status = 'pending'
                               AND expires_at > now()
                            RETURNING id, action_id, action_name, kind, call_id,
                                      arguments, resume_state, status, result,
                                      agent_id
                            """
                        ),
                        {
                            "conversation_id": conversation_id,
                            "call_id": call_id,
                            "status": status,
                            "result": json.dumps(result) if result is not None else None,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def cancel_suspensions(self, tenant_id: UUID, conversation_id: UUID) -> int:
        """Drop everything this conversation was waiting on. Called when the
        visitor sends a new message instead of answering, which is a decline in
        every way that matters."""
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    "UPDATE action_suspensions "
                    "SET status = 'cancelled', resolved_at = now() "
                    "WHERE conversation_id = :conversation_id AND status = 'pending'"
                ),
                {"conversation_id": conversation_id},
            )
        return int(getattr(result, "rowcount", 0) or 0)

    # ------------------------------------------------------ cross-tenant sweepers

    async def expire_suspensions(self, *, limit: int = 5000) -> int:
        """Mark timed-out awaits expired, across all tenants.

        `admin_scope()` because a cron job has no tenant. Bounded by `limit` so one
        run cannot hold a long write transaction over a hot table; the cron simply
        catches up on the next tick.
        """
        async with admin_scope() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE action_suspensions SET status = 'expired', resolved_at = now()
                     WHERE id IN (
                        SELECT id FROM action_suspensions
                         WHERE status = 'pending' AND expires_at <= now()
                         LIMIT :limit
                     )
                    """
                ),
                {"limit": limit},
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def prune_calls(self, *, older_than_days: int = 90, limit: int = 20000) -> int:
        """Delete ledger rows past the retention window, across all tenants.

        The ledger is the highest-volume table the actions engine writes. Deleting
        by a bounded id set rather than a bare `WHERE ts <` keeps the delete off a
        table-wide lock, which matters because this runs while calls are landing.
        """
        async with admin_scope() as session:
            result = await session.execute(
                text(
                    """
                    DELETE FROM action_calls WHERE id IN (
                        SELECT id FROM action_calls
                         WHERE ts < now() - make_interval(days => :days)
                         LIMIT :limit
                    )
                    """
                ),
                {"days": older_than_days, "limit": limit},
            )
        return int(getattr(result, "rowcount", 0) or 0)


class ActionSecretResolver:
    """Turns a `tenant_secrets` reference into a plaintext credential.

    Satisfies the `SecretResolver` shape `ActionExecutor` expects. It is a class
    of its own rather than two more methods on the repo because of what it
    returns: every other method here returns something safe to log, and this one
    returns a live credential. Keeping it separate means "does this call site
    handle a secret" is answerable by reading the type.

    Nothing is cached. A rotated or revoked credential must stop working on the
    next call, and a decrypted value held in a process is a value that ends up in
    a heap dump.
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    async def _decrypt_ref(self, tenant_id: UUID, ref: str) -> str | None:
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT ciphertext FROM tenant_secrets "
                            "WHERE tenant_id = :tenant_id AND ref = :ref"
                        ),
                        {"tenant_id": tenant_id, "ref": ref},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            logger.warning("action_secret_missing", tenant_id=str(tenant_id), ref=ref)
            return None
        try:
            return decrypt_secret(row["ciphertext"], self._settings.ENCRYPTION_KEY)
        except Exception:
            # A wrong ENCRYPTION_KEY and a corrupt ciphertext are the same symptom
            # here. Log the ref, never the ciphertext or the exception text, which
            # in some Fernet builds includes fragments of the payload.
            logger.warning(
                "action_secret_undecryptable", tenant_id=str(tenant_id), ref=ref
            )
            return None

    async def resolve(self, tenant_id: UUID, ref: str) -> str | None:
        """One credential by `tenant_secrets.ref` — the `auth` block's `secret_ref`."""
        return await self._decrypt_ref(tenant_id, ref)

    async def resolve_named(self, tenant_id: UUID, action_id: UUID) -> dict[str, str]:
        """Every `{{secret.NAME}}` this action declares, decrypted.

        One query for the refs, then one decrypt each. A name whose secret is
        missing or undecryptable is omitted rather than returned empty, so the
        template renderer fails the call instead of sending an empty Authorization
        header to a live provider.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT s.name, t.ciphertext "
                            "FROM action_secrets s "
                            "JOIN tenant_secrets t "
                            "  ON t.tenant_id = s.tenant_id AND t.ref = s.secret_ref "
                            "WHERE s.action_id = :action_id"
                        ),
                        {"action_id": action_id},
                    )
                )
                .mappings()
                .all()
            )
        resolved: dict[str, str] = {}
        for row in rows:
            try:
                resolved[str(row["name"])] = decrypt_secret(
                    row["ciphertext"], self._settings.ENCRYPTION_KEY
                )
            except Exception:
                logger.warning(
                    "action_secret_undecryptable",
                    tenant_id=str(tenant_id),
                    action_id=str(action_id),
                    name=str(row["name"]),
                )
        return resolved
