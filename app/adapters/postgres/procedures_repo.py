"""Postgres access for procedures, procedure runs, and the run timeline.

Raw `text()` SQL through SQLAlchemy Core under `tenant_scope`, like every other
repository here: RLS is the isolation mechanism, so the tenant context is what
makes a query safe rather than a `WHERE tenant_id = …` that a future edit could
quietly drop.

Four things in this file are not ordinary CRUD and are the reason it exists.

**There are no denormalised counters on `procedures`.** An earlier pass put
`triggered_count` / `completed_count` on the row; every run start would then write
the one row the per-turn trigger catalogue reads, and the Performance tab needs
*windowed* numbers that a lifetime counter cannot give. Every number comes off
`procedure_runs` on the `(tenant_id, procedure_id, started_at DESC)` index. The
single exception is `last_run_at`, which orders the trigger catalogue and is
touched once per run start by `touch_last_run`.

**A run is a purpose-built row, not a serialised graph.** There is no
checkpointer: the run has to survive process death on a scale-to-zero machine
*and* be readable by a support lead in the dashboard, so `step_path`, `variables`
with provenance and `branch_log` are named columns a human can read. The lookup
at the start of every turn — `active_run` — is one indexed read, and that read is
what replaces a checkpointer entirely.

**`save_run` writes only what it was given.** Every field is an optional keyword
and the SET list is built from the ones supplied, because the runtime advances
one aspect of a run at a time and a full-row UPDATE would let a stale in-memory
copy clobber a variable another path just collected.

**The one-active-run rule is the database's, not the loop's.** The partial unique
index `procedure_runs_one_active_per_conversation` is the enforcement;
`start_run` therefore inserts with `ON CONFLICT DO NOTHING` and raises rather
than pre-checking, so two concurrent turns on one conversation cannot both win.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from app.core.constants import UsageKind
from app.core.db import admin_scope, tenant_scope
from app.core.errors import ConflictError
from app.core.logging import get_logger
from sqlalchemy import text

logger = get_logger(__name__)

PROCEDURE_COLUMNS = """
    id, tenant_id, agent_id, name, trigger_text, steps, required_actions,
    redact_transcript_on_escalation, trigger_embedding, trigger_embedding_model,
    enabled, version, library_key, last_run_at, created_by, created_at, updated_at
"""

# The same list qualified for the list query's join. Spelled out rather than
# derived from the string above: a comprehension over a SQL fragment is a parser,
# and a parser in a query builder is where the injection bugs live.
PROCEDURE_COLUMNS_P = """
    p.id, p.tenant_id, p.agent_id, p.name, p.trigger_text, p.steps,
    p.required_actions, p.redact_transcript_on_escalation, p.trigger_embedding,
    p.trigger_embedding_model, p.enabled, p.version, p.library_key, p.last_run_at,
    p.created_by, p.created_at, p.updated_at
"""

RUN_COLUMNS = """
    id, tenant_id, agent_id, conversation_id, procedure_id, procedure_name,
    procedure_version, steps_snapshot, status, outcome, ended_reason, step_index,
    step_path, current_sid, variables, branch_log, turn_count, action_calls,
    bookkeeping_calls, escalation_id, started_at, last_activity_at, ended_at,
    expires_at
"""

# The runs list is paginated in the UI; this is the ceiling on one page so a
# caller cannot ask for an unbounded number of snapshots in one response.
MAX_RUNS_PAGE = 200

# The Performance tab never shows more than a quarter, and `?days=` is clamped to
# this before it reaches SQL.
MAX_TREND_DAYS = 90

# The number of runs the Performance tab lists under its chart.
RECENT_RUNS = 20

# The statuses that count as a live run. Mirrors the partial unique index.
PENDING_STATUSES = ("active", "awaiting_user")


def _json(value: Any, fallback: Any) -> Any:
    """Decode a JSONB column that may arrive decoded, as text, or as NULL."""
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


def _dumps(value: Any) -> str:
    """Render a value for a `CAST(:param AS jsonb)` bind.

    Accepts an already-serialised string so a caller that has JSON in hand does
    not pay to parse and re-render it.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else None)


def _procedure(row: Any) -> dict[str, Any]:
    """One `procedures` row as a plain dict, with JSONB and arrays normalised."""
    out = dict(row)
    out["steps"] = _json(out.get("steps"), [])
    out["required_actions"] = [UUID(str(item)) for item in (out.get("required_actions") or [])]
    embedding = out.get("trigger_embedding")
    out["trigger_embedding"] = [float(item) for item in embedding] if embedding else None
    return out


def _run(row: Any) -> dict[str, Any]:
    """One `procedure_runs` row as a plain dict, with JSONB normalised."""
    out = dict(row)
    out["steps_snapshot"] = _json(out.get("steps_snapshot"), [])
    out["variables"] = _json(out.get("variables"), {})
    out["branch_log"] = _json(out.get("branch_log"), [])
    return out


class ProceduresRepo:
    """`procedures`, `procedure_runs`, `procedure_run_steps`.

    Storage only: no business rules, no validation, no plan checks. The publish
    rules of §1.5 and the ceilings of §6 belong to the service and the runtime,
    which is why `required_actions` arrives here already resolved.
    """

    # ------------------------------------------------------------- procedures

    async def create(
        self,
        tenant_id: UUID,
        agent_id: UUID,
        *,
        name: str,
        trigger_text: str,
        steps: Any,
        required_actions: list[UUID] | None = None,
        redact_transcript_on_escalation: bool = False,
        enabled: bool = False,
        library_key: str | None = None,
        created_by: UUID | None = None,
    ) -> dict[str, Any] | None:
        """Insert one procedure. Returns None when the name is already taken.

        The unique constraint is `(tenant_id, agent_id, name)`, so the conflict is
        resolved in the database rather than by a read-then-write that two
        concurrent creates could both pass.
        """
        procedure_id = uuid4()
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""
                            INSERT INTO procedures (
                                id, tenant_id, agent_id, name, trigger_text, steps,
                                required_actions, redact_transcript_on_escalation,
                                enabled, version, library_key, created_by
                            ) VALUES (
                                :id, :tenant_id, :agent_id, :name, :trigger_text,
                                CAST(:steps AS jsonb),
                                CAST(:required_actions AS uuid[]),
                                :redact, :enabled, 1, :library_key, :created_by
                            )
                            ON CONFLICT (tenant_id, agent_id, name) DO NOTHING
                            RETURNING {PROCEDURE_COLUMNS}
                            """  # noqa: S608
                        ),
                        {
                            "id": procedure_id,
                            "tenant_id": tenant_id,
                            "agent_id": agent_id,
                            "name": name,
                            "trigger_text": trigger_text,
                            "steps": _dumps(steps),
                            "required_actions": list(required_actions or []),
                            "redact": redact_transcript_on_escalation,
                            "enabled": enabled,
                            "library_key": library_key,
                            "created_by": created_by,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return _procedure(row) if row else None

    async def replace(
        self,
        tenant_id: UUID,
        procedure_id: UUID,
        *,
        name: str,
        trigger_text: str,
        steps: Any,
        required_actions: list[UUID] | None = None,
        redact_transcript_on_escalation: bool = False,
        enabled: bool = False,
    ) -> dict[str, Any] | None:
        """Full replacement of a definition, bumping `version`.

        A PUT rather than a PATCH because a step tree cannot be merged
        field-by-field without ambiguity about reordering. `version` is bumped in
        the same statement so a run started against the old text can say which
        version it followed. The trigger embedding is cleared: the trigger may
        have changed, and a stale vector would rank a procedure by prose it no
        longer has.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""
                            UPDATE procedures
                               SET name = :name,
                                   trigger_text = :trigger_text,
                                   steps = CAST(:steps AS jsonb),
                                   required_actions = CAST(:required_actions AS uuid[]),
                                   redact_transcript_on_escalation = :redact,
                                   enabled = :enabled,
                                   trigger_embedding = NULL,
                                   trigger_embedding_model = NULL,
                                   version = version + 1,
                                   updated_at = now()
                             WHERE id = :id
                            RETURNING {PROCEDURE_COLUMNS}
                            """  # noqa: S608
                        ),
                        {
                            "id": procedure_id,
                            "name": name,
                            "trigger_text": trigger_text,
                            "steps": _dumps(steps),
                            "required_actions": list(required_actions or []),
                            "redact": redact_transcript_on_escalation,
                            "enabled": enabled,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return _procedure(row) if row else None

    async def get(self, tenant_id: UUID, procedure_id: UUID) -> dict[str, Any] | None:
        """One procedure by id. The runtime's definition read."""
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(f"SELECT {PROCEDURE_COLUMNS} FROM procedures WHERE id = :id"),  # noqa: S608
                        {"id": procedure_id},
                    )
                )
                .mappings()
                .first()
            )
        return _procedure(row) if row else None

    async def get_by_name(
        self, tenant_id: UUID, agent_id: UUID, name: str
    ) -> dict[str, Any] | None:
        """One procedure by its unique `(agent, name)`, for the 409 on create."""
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"SELECT {PROCEDURE_COLUMNS} FROM procedures "  # noqa: S608
                            f"WHERE agent_id = :agent_id AND name = :name"
                        ),
                        {"agent_id": agent_id, "name": name},
                    )
                )
                .mappings()
                .first()
            )
        return _procedure(row) if row else None

    async def list_for_agent(self, tenant_id: UUID, agent_id: UUID) -> list[dict[str, Any]]:
        """Every procedure on this agent, with its lifetime run counts.

        One grouped subquery for the whole list rather than a counter per row: the
        counts are what the list screen shows next to each SOP, and a per-row
        query would be one round trip per procedure.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT {PROCEDURE_COLUMNS_P},
                                   coalesce(c.triggered, 0) AS triggered_count,
                                   coalesce(c.resolved, 0)  AS resolved_count
                              FROM procedures p
                              LEFT JOIN (
                                   SELECT procedure_id,
                                          count(*) AS triggered,
                                          count(*) FILTER (WHERE status = 'resolved')
                                              AS resolved
                                     FROM procedure_runs
                                    WHERE procedure_id IS NOT NULL
                                    GROUP BY procedure_id
                              ) c ON c.procedure_id = p.id
                             WHERE p.agent_id = :agent_id
                             ORDER BY p.name
                            """  # noqa: S608
                        ),
                        {"agent_id": agent_id},
                    )
                )
                .mappings()
                .all()
            )
        out: list[dict[str, Any]] = []
        for row in rows:
            item = _procedure(row)
            item["triggered_count"] = int(row["triggered_count"])
            item["resolved_count"] = int(row["resolved_count"])
            out.append(item)
        return out

    async def list_enabled(self, tenant_id: UUID, agent_id: UUID) -> list[dict[str, Any]]:
        """The enabled procedures for this agent, catalogue order.

        Most-recently-triggered first, on the partial index, so the model's
        attention lands on what this tenant actually uses. The shortlist
        pre-filter reads `trigger_embedding` off these same rows, which is why the
        vector is selected here rather than fetched per candidate.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT {PROCEDURE_COLUMNS}
                              FROM procedures
                             WHERE agent_id = :agent_id AND enabled
                             ORDER BY last_run_at DESC NULLS LAST, name
                            """  # noqa: S608
                        ),
                        {"agent_id": agent_id},
                    )
                )
                .mappings()
                .all()
            )
        return [_procedure(row) for row in rows]

    async def set_enabled(self, tenant_id: UUID, procedure_id: UUID, enabled: bool) -> bool:
        """Turn one procedure on or off. False when there was no such row."""
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    "UPDATE procedures SET enabled = :enabled, updated_at = now() "
                    "WHERE id = :id"
                ),
                {"id": procedure_id, "enabled": enabled},
            )
        return bool(getattr(result, "rowcount", 0) or 0)

    async def delete(self, tenant_id: UUID, procedure_id: UUID) -> bool:
        """Delete one procedure. Runs survive it — `procedure_id` is ON DELETE
        SET NULL and the run carries a denormalised name and version so it stays
        readable afterwards."""
        async with tenant_scope(tenant_id) as session:
            result = await session.execute(
                text("DELETE FROM procedures WHERE id = :id"), {"id": procedure_id}
            )
        return bool(getattr(result, "rowcount", 0) or 0)

    async def set_trigger_embedding(
        self,
        tenant_id: UUID,
        procedure_id: UUID,
        *,
        embedding: list[float] | None,
        model: str | None,
    ) -> None:
        """Store the trigger vector for the opt-in shortlist pre-filter.

        The model name travels with the vector: if it does not match the tenant's
        current embedding model the pre-filter is skipped rather than comparing
        vectors from two different spaces, which is a silently wrong ranking.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    "UPDATE procedures "
                    "   SET trigger_embedding = CAST(:embedding AS double precision[]), "
                    "       trigger_embedding_model = :model "
                    " WHERE id = :id"
                ),
                {
                    "id": procedure_id,
                    "embedding": list(embedding) if embedding else None,
                    "model": model,
                },
            )

    async def agent_action_index(
        self, tenant_id: UUID, agent_id: UUID
    ) -> dict[str, dict[str, Any]]:
        """The actions this agent can actually run, keyed by `@name`.

        Reads `actions` joined to `agent_actions` because that link — not tenant
        ownership — is what makes an `@name` in a step resolvable: a procedure is
        agent-scoped and its steps name actions that must be enabled *on that
        agent*. `linked` is returned separately from `enabled` so the editor can
        say which of the two is missing.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT a.id, a.name, a.kind, a.effect, a.enabled,
                                   a.only_in_procedures, a.requires_confirmation,
                                   a.requires_verified_identity, a.setup_required,
                                   a.channels,
                                   (l.action_id IS NOT NULL) AS linked
                              FROM actions a
                              LEFT JOIN agent_actions l
                                     ON l.action_id = a.id AND l.agent_id = :agent_id
                             ORDER BY a.name
                            """
                        ),
                        {"agent_id": agent_id},
                    )
                )
                .mappings()
                .all()
            )
        return {
            str(row["name"]): {
                "id": row["id"],
                "name": row["name"],
                "kind": row["kind"],
                "effect": row["effect"],
                "enabled": bool(row["enabled"]),
                "linked": bool(row["linked"]),
                "only_in_procedures": bool(row["only_in_procedures"]),
                "requires_confirmation": bool(row["requires_confirmation"]),
                "requires_verified_identity": bool(row["requires_verified_identity"]),
                "setup_required": bool(row["setup_required"]),
                "channels": list(row["channels"] or []),
            }
            for row in rows
        }

    # ------------------------------------------------------------------- runs

    async def active_run(
        self, tenant_id: UUID, conversation_id: UUID
    ) -> dict[str, Any] | None:
        """The live run on this conversation, if any.

        One indexed read at the start of every turn. This is what replaces a
        checkpointer, so it must stay a single statement against
        `procedure_runs_one_active_per_conversation` — anything joined onto it
        would be paid for on every message of every conversation.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT {RUN_COLUMNS}
                              FROM procedure_runs
                             WHERE conversation_id = :conversation_id
                               AND status IN ('active', 'awaiting_user')
                             ORDER BY started_at DESC
                             LIMIT 1
                            """  # noqa: S608
                        ),
                        {"conversation_id": conversation_id},
                    )
                )
                .mappings()
                .first()
            )
        return _run(row) if row else None

    async def start_run(
        self,
        tenant_id: UUID,
        *,
        agent_id: UUID | None,
        conversation_id: UUID,
        procedure_id: UUID | None,
        procedure_name: str,
        procedure_version: int,
        steps_snapshot: Any,
        expires_at: Any,
    ) -> UUID:
        """Open a run, and meter it.

        The insert carries `ON CONFLICT DO NOTHING` against the partial unique
        index, so the one-active-run rule is decided by the database and two
        concurrent turns on the same conversation cannot both open a run. A
        conflict is a `ConflictError` rather than a None return, because there is
        no sensible way for a caller to continue: the model asked to start a
        second procedure and must be told so.

        The pointer starts on the first entry of the snapshot, which is why the
        snapshot is passed in already flattened — the run's `step_index` is an
        ordinal into exactly this list, and computing it here from a nested tree
        would be a second walk that could disagree with the first.
        """
        snapshot = steps_snapshot if not isinstance(steps_snapshot, str) else None
        first: dict[str, Any] = {}
        if isinstance(snapshot, list) and snapshot:
            head = snapshot[0]
            first = head if isinstance(head, dict) else dict(head)

        run_id = uuid4()
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            INSERT INTO procedure_runs (
                                id, tenant_id, agent_id, conversation_id, procedure_id,
                                procedure_name, procedure_version, steps_snapshot,
                                status, step_index, step_path, current_sid, expires_at
                            ) VALUES (
                                :id, :tenant_id, :agent_id, :conversation_id,
                                :procedure_id, :procedure_name, :procedure_version,
                                CAST(:steps_snapshot AS jsonb), 'active', 0,
                                :step_path, :current_sid, :expires_at
                            )
                            ON CONFLICT (tenant_id, conversation_id)
                                WHERE status IN ('active', 'awaiting_user')
                            DO NOTHING
                            RETURNING id
                            """
                        ),
                        {
                            "id": run_id,
                            "tenant_id": tenant_id,
                            "agent_id": agent_id,
                            "conversation_id": conversation_id,
                            "procedure_id": procedure_id,
                            "procedure_name": procedure_name,
                            "procedure_version": procedure_version,
                            "steps_snapshot": _dumps(steps_snapshot),
                            "step_path": str(first.get("step_path") or "1"),
                            "current_sid": first.get("sid"),
                            "expires_at": expires_at,
                        },
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise ConflictError("a procedure run is already active on this conversation")

        await self._meter_run(tenant_id, procedure_name=procedure_name, run_id=run_id)
        return UUID(str(row["id"]))

    async def _meter_run(
        self, tenant_id: UUID, *, procedure_name: str, run_id: UUID
    ) -> None:
        """Meter one procedure run into `usage_events`, at zero.

        Metered like an escalation: visible in the usage breakdown so a tenant can
        see procedure volume, charged at nothing, because the real cost of a run
        is prefill tokens and those are already metered per turn. Written here
        rather than in the service so that no caller can open a run without it.
        Swallows on failure — an unmetered run at zero micros is a missing chart
        bar, and raising after the run row committed would strand the run.
        """
        try:
            async with tenant_scope(tenant_id) as session:
                await session.execute(
                    text(
                        "INSERT INTO usage_events (tenant_id, kind, quantity, meta) "
                        "VALUES (:tenant_id, :kind, 1, CAST(:meta AS jsonb))"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "kind": UsageKind.PROCEDURE_RUN.value,
                        "meta": json.dumps(
                            {"procedure": procedure_name, "run_id": str(run_id)}
                        ),
                    },
                )
        except Exception:
            logger.warning(
                "procedure_usage_write_failed",
                tenant_id=str(tenant_id),
                run_id=str(run_id),
            )

    async def save_run(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        status: str | None = None,
        step_index: int | None = None,
        step_path: str | None = None,
        current_sid: str | None = None,
        variables: Any = None,
        branch_log: Any = None,
        turn_count: int | None = None,
        action_calls: int | None = None,
        bookkeeping_calls: int | None = None,
    ) -> None:
        """Update the aspects of a run that were supplied, and nothing else.

        `last_activity_at` always moves, because that column is what the TTL sweep
        and the Pending count read: a run being worked on is by definition active,
        and a save that did not touch it would let a live conversation be swept.
        """
        sets: list[str] = ["last_activity_at = now()"]
        params: dict[str, Any] = {"id": run_id}

        if status is not None:
            sets.append("status = :status")
            params["status"] = status
        if step_index is not None:
            sets.append("step_index = :step_index")
            params["step_index"] = step_index
        if step_path is not None:
            sets.append("step_path = :step_path")
            params["step_path"] = step_path
        if current_sid is not None:
            sets.append("current_sid = :current_sid")
            params["current_sid"] = current_sid
        if variables is not None:
            sets.append("variables = CAST(:variables AS jsonb)")
            params["variables"] = _dumps(variables)
        if branch_log is not None:
            sets.append("branch_log = CAST(:branch_log AS jsonb)")
            params["branch_log"] = _dumps(branch_log)
        if turn_count is not None:
            sets.append("turn_count = :turn_count")
            params["turn_count"] = turn_count
        if action_calls is not None:
            sets.append("action_calls = :action_calls")
            params["action_calls"] = action_calls
        if bookkeeping_calls is not None:
            sets.append("bookkeeping_calls = :bookkeeping_calls")
            params["bookkeeping_calls"] = bookkeeping_calls

        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(f"UPDATE procedure_runs SET {', '.join(sets)} WHERE id = :id"),  # noqa: S608
                params,
            )

    async def bump_counters(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        turns: int = 0,
        action_calls: int = 0,
        bookkeeping_calls: int = 0,
    ) -> dict[str, int]:
        """Increment the runaway-loop counters in the database and read them back.

        Separate from `save_run` on purpose: the per-run ceilings of §6 are the
        ones that matter most, and `count = count + 1` in SQL cannot lose a
        concurrent increment the way a read-modify-write from two turns can.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            UPDATE procedure_runs
                               SET turn_count = turn_count + :turns,
                                   action_calls = action_calls + :action_calls,
                                   bookkeeping_calls =
                                       bookkeeping_calls + :bookkeeping_calls,
                                   last_activity_at = now()
                             WHERE id = :id
                            RETURNING turn_count, action_calls, bookkeeping_calls
                            """
                        ),
                        {
                            "id": run_id,
                            "turns": turns,
                            "action_calls": action_calls,
                            "bookkeeping_calls": bookkeeping_calls,
                        },
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return {"turn_count": 0, "action_calls": 0, "bookkeeping_calls": 0}
        return {
            "turn_count": int(row["turn_count"]),
            "action_calls": int(row["action_calls"]),
            "bookkeeping_calls": int(row["bookkeeping_calls"]),
        }

    async def append_step(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        ordinal: int,
        step_path: str,
        step_text: str,
        action_name: str | None,
        outcome: str,
        note: str | None = None,
    ) -> None:
        """Record one executed step on the run timeline.

        `(run_id, ordinal)` is unique, so a retried turn re-recording the same
        step is idempotent rather than a duplicate row in the timeline a support
        lead is reading. `step_text` is the rendered text the model was actually
        shown, variables interpolated — the whole point of the timeline is
        answering "what did it say", which the template alone cannot.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO procedure_run_steps (
                        tenant_id, run_id, ordinal, step_path, step_text,
                        action_name, outcome, note
                    ) VALUES (
                        :tenant_id, :run_id, :ordinal, :step_path, :step_text,
                        :action_name, :outcome, :note
                    )
                    ON CONFLICT (run_id, ordinal) DO UPDATE
                       SET step_path = excluded.step_path,
                           step_text = excluded.step_text,
                           action_name = excluded.action_name,
                           outcome = excluded.outcome,
                           note = excluded.note
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "run_id": run_id,
                    "ordinal": ordinal,
                    "step_path": step_path,
                    "step_text": step_text,
                    "action_name": action_name,
                    "outcome": outcome,
                    "note": note,
                },
            )

    async def end_run(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        status: str,
        outcome: str | None,
        ended_reason: str | None,
        escalation_id: UUID | None = None,
    ) -> None:
        """Close a run. Only ever moves a live run, never reopens a closed one.

        The `status IN ('active','awaiting_user')` guard is what makes this safe to
        call twice: a TTL sweep and an `end_procedure` racing on the same run
        produce one closure, and the first one wins rather than the last one
        overwriting the real reason with `ttl_expired`.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text(
                    """
                    UPDATE procedure_runs
                       SET status = :status,
                           outcome = :outcome,
                           ended_reason = :ended_reason,
                           escalation_id = coalesce(:escalation_id, escalation_id),
                           ended_at = now(),
                           last_activity_at = now()
                     WHERE id = :id
                       AND status IN ('active', 'awaiting_user')
                    """
                ),
                {
                    "id": run_id,
                    "status": status,
                    "outcome": outcome,
                    "ended_reason": ended_reason,
                    "escalation_id": escalation_id,
                },
            )

    async def touch_last_run(self, tenant_id: UUID, procedure_id: UUID) -> None:
        """Mark a procedure as just used, for catalogue ordering.

        The one write to the `procedures` row on a hot path, and the reason there
        are no other counters on it: catalogue order genuinely needs recency, and
        one timestamp is cheaper to keep than three counts.
        """
        async with tenant_scope(tenant_id) as session:
            await session.execute(
                text("UPDATE procedures SET last_run_at = now() WHERE id = :id"),
                {"id": procedure_id},
            )

    async def restart_count(
        self, tenant_id: UUID, conversation_id: UUID, procedure_id: UUID
    ) -> int:
        """How many times this procedure has already run on this conversation.

        The basis for `PROCEDURE_MAX_RESTARTS`: a visitor who abandons and
        restarts the same SOP four times is a loop, not a customer changing their
        mind.
        """
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT count(*) AS n FROM procedure_runs "
                            " WHERE conversation_id = :conversation_id "
                            "   AND procedure_id = :procedure_id"
                        ),
                        {
                            "conversation_id": conversation_id,
                            "procedure_id": procedure_id,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return int(row["n"]) if row else 0

    async def fail_active_runs(
        self,
        tenant_id: UUID,
        procedure_id: UUID,
        *,
        status: str = "failed",
        outcome: str | None = "handed_off",
        ended_reason: str = "definition_deleted",
    ) -> list[dict[str, Any]]:
        """Close every live run of one procedure, returning what was closed.

        Called when a definition is deleted: the run cannot continue and the
        visitor is mid-flow, so it closes and hands off to a person rather than
        stalling. The returned rows carry the conversation ids the caller needs in
        order to raise those handoffs.
        """
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            UPDATE procedure_runs
                               SET status = :status,
                                   outcome = :outcome,
                                   ended_reason = :ended_reason,
                                   ended_at = now(),
                                   last_activity_at = now()
                             WHERE procedure_id = :procedure_id
                               AND status IN ('active', 'awaiting_user')
                            RETURNING id, conversation_id, agent_id, procedure_name
                            """
                        ),
                        {
                            "procedure_id": procedure_id,
                            "status": status,
                            "outcome": outcome,
                            "ended_reason": ended_reason,
                        },
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def force_close(
        self,
        tenant_id: UUID,
        run_id: UUID,
        *,
        reason: str = "force_closed",
    ) -> dict[str, Any] | None:
        """Admin force-close of a stuck run, returning the closed row.

        `superseded` is the status a run reaches only this way — the runtime never
        writes it — which is what makes it readable as "a person intervened here".
        Returns None when the run was already closed, so the endpoint can answer
        409 rather than pretending to have done something.
        """
        status = "superseded" if reason == "superseded" else "failed"
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""
                            UPDATE procedure_runs
                               SET status = :status,
                                   outcome = 'not_applicable',
                                   ended_reason = :reason,
                                   ended_at = now(),
                                   last_activity_at = now()
                             WHERE id = :id
                               AND status IN ('active', 'awaiting_user')
                            RETURNING {RUN_COLUMNS}
                            """  # noqa: S608
                        ),
                        {"id": run_id, "status": status, "reason": reason},
                    )
                )
                .mappings()
                .first()
            )
        return _run(row) if row else None

    async def get_run(self, tenant_id: UUID, run_id: UUID) -> dict[str, Any] | None:
        """One run by id. Addressed on its own rather than under its procedure,
        because `procedure_id` is NULL once the definition is deleted."""
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(f"SELECT {RUN_COLUMNS} FROM procedure_runs WHERE id = :id"),  # noqa: S608
                        {"id": run_id},
                    )
                )
                .mappings()
                .first()
            )
        return _run(row) if row else None

    async def run_steps(self, tenant_id: UUID, run_id: UUID) -> list[dict[str, Any]]:
        """The run timeline, in order. What support reads most."""
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT ordinal, step_path, step_text, action_name, "
                            "       outcome, note, at "
                            "  FROM procedure_run_steps "
                            " WHERE run_id = :run_id ORDER BY ordinal"
                        ),
                        {"run_id": run_id},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    # -------------------------------------------------------------- analytics

    async def performance(
        self, tenant_id: UUID, procedure_id: UUID, *, days: int = 30
    ) -> dict[str, int]:
        """The three StatCards, over a window.

        Windowed rather than lifetime, and computed rather than counted: a
        denormalised counter cannot answer "this month" and would make every run
        start write the hot catalogue row. `pending` is returned alongside the
        three cards because the UI's Pending count is the same query.
        """
        window = max(1, min(int(days), MAX_TREND_DAYS))
        async with tenant_scope(tenant_id) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT count(*) AS triggered,
                                   count(*) FILTER (
                                       WHERE status IN ('active', 'awaiting_user')
                                   ) AS pending,
                                   count(*) FILTER (WHERE status = 'resolved')
                                       AS resolved,
                                   count(*) FILTER (WHERE status = 'handed_off')
                                       AS handed_off
                              FROM procedure_runs
                             WHERE procedure_id = :procedure_id
                               AND started_at >= now() - make_interval(days => :days)
                            """
                        ),
                        {"procedure_id": procedure_id, "days": window},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return {"triggered": 0, "pending": 0, "resolved": 0, "handed_off": 0}
        return {
            "triggered": int(row["triggered"]),
            "pending": int(row["pending"]),
            "resolved": int(row["resolved"]),
            "handed_off": int(row["handed_off"]),
        }

    async def trend(
        self, tenant_id: UUID, procedure_id: UUID, *, days: int = 30
    ) -> list[dict[str, Any]]:
        """The daily bars, gap-filled so the chart has no missing days.

        `generate_series` on the left of the join is what fills the gaps: a day
        with no runs has to render as a zero bar, and a client-side fill would
        need the client to know the server's timezone.
        """
        window = max(1, min(int(days), MAX_TREND_DAYS))
        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT d::date AS day,
                                   count(r.id) AS triggered,
                                   count(r.id) FILTER (WHERE r.status = 'resolved')
                                       AS resolved,
                                   count(r.id) FILTER (WHERE r.status = 'handed_off')
                                       AS handed_off
                              FROM generate_series(
                                       date_trunc('day', now())
                                           - make_interval(days => :days - 1),
                                       date_trunc('day', now()),
                                       interval '1 day') AS d
                              LEFT JOIN procedure_runs r
                                     ON r.procedure_id = :procedure_id
                                    AND date_trunc('day', r.started_at) = d
                             GROUP BY d
                             ORDER BY d
                            """
                        ),
                        {"procedure_id": procedure_id, "days": window},
                    )
                )
                .mappings()
                .all()
            )
        return [
            {
                "day": row["day"].isoformat() if row["day"] else None,
                "triggered": int(row["triggered"]),
                "resolved": int(row["resolved"]),
                "handed_off": int(row["handed_off"]),
            }
            for row in rows
        ]

    async def list_runs(
        self,
        tenant_id: UUID,
        procedure_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
        window_days: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of runs, newest first, plus the total for the pager.

        `steps_snapshot` is deliberately not selected: it is a few KB per run and
        the list renders none of it, so a fifty-row page would otherwise carry a
        few hundred KB nobody reads. The run detail endpoint fetches it per run.
        """
        size = max(1, min(int(limit), MAX_RUNS_PAGE))
        clauses = ["procedure_id = :procedure_id"]
        params: dict[str, Any] = {
            "procedure_id": procedure_id,
            "limit": size,
            "offset": max(0, int(offset)),
        }
        if window_days is not None:
            clauses.append("started_at >= now() - make_interval(days => :days)")
            params["days"] = max(1, min(int(window_days), MAX_TREND_DAYS))
        where = " AND ".join(clauses)

        async with tenant_scope(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT id, agent_id, conversation_id, procedure_id,
                                   procedure_name, procedure_version, status, outcome,
                                   ended_reason, step_index, step_path, current_sid,
                                   turn_count, action_calls, bookkeeping_calls,
                                   escalation_id, started_at, last_activity_at,
                                   ended_at, expires_at
                              FROM procedure_runs
                             WHERE {where}
                             ORDER BY started_at DESC
                             LIMIT :limit OFFSET :offset
                            """  # noqa: S608
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            total_row = (
                (
                    await session.execute(
                        text(f"SELECT count(*) AS n FROM procedure_runs WHERE {where}"),  # noqa: S608
                        {
                            key: value
                            for key, value in params.items()
                            if key not in {"limit", "offset"}
                        },
                    )
                )
                .mappings()
                .first()
            )
        total = int(total_row["n"]) if total_row else 0
        return [dict(row) for row in rows], total

    async def recent_runs(
        self, tenant_id: UUID, procedure_id: UUID, *, limit: int = RECENT_RUNS
    ) -> list[dict[str, Any]]:
        """The last runs under the Performance chart, each linking to its
        conversation. Runs are pruned at 400 days and the window never exceeds 90,
        so pruning is invisible here."""
        rows, _ = await self.list_runs(tenant_id, procedure_id, limit=limit, offset=0)
        return rows

    async def sweep_expired_runs(
        self, retention_days: int, *, tenant_limit: int = 200
    ) -> dict[str, int]:
        """Close runs whose TTL has passed, then delete runs past retention.

        Two statements, deliberately in this order and deliberately not merged.

        Closing is a correctness fix, not housekeeping. A run left `active` past
        its `expires_at` still holds the partial unique index on its conversation,
        so the next message on that conversation cannot start a fresh run. The
        runtime closes such a row the moment it sees one, but a visitor who simply
        never comes back leaves it for this sweep to find, and nothing else ever
        looks at that conversation again.

        Deleting is the retention half and only ever touches rows that have
        already ended, so it can never remove a run a conversation still needs.
        `procedure_run_steps` cascades from the run and `action_calls` is set to
        NULL, so no second delete is required and no action record is lost.

        The tenant list is gathered under `admin_scope` because a cron job has no
        tenant, but both statements run under that tenant's own `tenant_scope`, so
        the RLS policy — not this loop — is what confines each one. Only tenants
        with work to do are visited, and the list is bounded: a late sweep costs
        disk, so falling behind is safe and re-running is idempotent.
        """
        days = max(1, int(retention_days))
        async with admin_scope() as session:
            tenant_rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT DISTINCT tenant_id
                              FROM procedure_runs
                             WHERE (status IN ('active', 'awaiting_user')
                                    AND expires_at <= now())
                                OR (ended_at IS NOT NULL
                                    AND ended_at < now() - make_interval(days => :days))
                             LIMIT :limit
                            """
                        ),
                        {"days": days, "limit": tenant_limit},
                    )
                )
                .mappings()
                .all()
            )

        closed = 0
        deleted = 0
        for tenant_row in tenant_rows:
            tenant_id: UUID = tenant_row["tenant_id"]
            async with tenant_scope(tenant_id) as session:
                stale = await session.execute(
                    text(
                        """
                        UPDATE procedure_runs
                           SET status        = 'abandoned',
                               outcome       = 'abandoned_by_user',
                               ended_reason  = 'ttl_expired',
                               ended_at      = now()
                         WHERE status IN ('active', 'awaiting_user')
                           AND expires_at <= now()
                        """
                    )
                )
                closed += int(getattr(stale, "rowcount", 0) or 0)
                expired = await session.execute(
                    text(
                        """
                        DELETE FROM procedure_runs
                         WHERE ended_at IS NOT NULL
                           AND ended_at < now() - make_interval(days => :days)
                        """
                    ),
                    {"days": days},
                )
                deleted += int(getattr(expired, "rowcount", 0) or 0)

        if closed or deleted:
            logger.info(
                "procedure_runs_swept",
                tenants=len(tenant_rows),
                closed=closed,
                deleted=deleted,
            )
        return {"closed": closed, "deleted": deleted}
