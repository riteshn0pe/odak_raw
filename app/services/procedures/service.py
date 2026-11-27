"""The authoring service: publish rules, template installs, and analytics reads.

This is the tenant-facing half of procedures. The runtime half — trigger
injection, the reserved tools, the run loop — lives beside it and shares only the
repository and the domain models, because the two halves have opposite shapes: a
run is one indexed read on a hot path, and authoring is a handful of rules applied
once, at save time, where being thorough costs nothing anybody notices.

Four rules are enforced here and nowhere else.

**References resolve at publish time, not at run time.** `@name` tokens are parsed
out of the step text, matched against the actions actually linked to *this* agent,
and the union of resolved ids is denormalised to `procedures.required_actions`.
That array is what makes the per-turn availability check one comparison instead of
a JSONB walk, and it is why every write path recomputes it rather than trusting
what the client sent.

**A dangling reference saves but cannot be enabled.** Half-authored procedures are
normal in a builder, so the save warns. Turning one on is refused, because a run
that collects an order number and then discovers step 7 cannot fire is the exact
failure this design exists to prevent.

**Installing a template installs what it needs.** A gallery entry names actions by
name; the tenant may not have them. Anything the shipped action library knows is
created disabled with `setup_required`, then linked to the agent, so the installed
procedure is one connection away from working rather than dead on arrival. A name
the action library does not know is a 400 — that is a defect in the template, not
something to paper over.

**A duplicate is a copy, never a link.** A procedure's steps name actions that
must be enabled on its agent and its trigger competes with that agent's other
triggers for the model's attention, so sharing one row across two agents breaks it
silently. Copying to an agent that lacks a required action is refused rather than
copied broken.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.adapters.postgres.procedures_repo import MAX_TREND_DAYS, ProceduresRepo
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.domain.procedures import (
    ProcedureDefinition,
    ValidationResult,
)
from app.services.actions import library as action_library
from app.services.procedures import library as procedure_library
from app.services.procedures.validate import (
    build_definition,
    resolve_references,
    validate_definition,
)

logger = get_logger(__name__)

# How many " 2", " 3" suffixes to try when a template's name is already taken.
# Installing the same gallery entry twice is a reasonable thing to do — a tenant
# with two refund flows for two brands — and the documented status codes for the
# install endpoints do not include 409, so the name is de-duplicated instead.
MAX_NAME_ATTEMPTS = 9


class ProcedureService:
    """Authoring, template installs, and the Performance tab's numbers.

    `actions` is the `ActionService`, used only to reach the actions repository
    when a template install has to create a missing action. It is optional so the
    service still constructs on a deployment where the actions engine is off; a
    template that needs an action it cannot create then fails with a clear 400
    rather than an AttributeError.
    """

    def __init__(
        self,
        *,
        repo: ProceduresRepo,
        actions: Any = None,
        settings: Any = None,
    ) -> None:
        self._repo = repo
        self._actions = actions
        self._settings = settings

    @property
    def repo(self) -> ProceduresRepo:
        return self._repo

    # -------------------------------------------------------------- validation

    async def action_index(
        self, tenant_id: UUID, agent_id: UUID
    ) -> dict[str, dict[str, Any]]:
        """The actions resolvable from this agent's procedures, keyed by `@name`."""
        return await self._repo.agent_action_index(tenant_id, agent_id)

    async def prepare(
        self,
        tenant_id: UUID,
        agent_id: UUID,
        payload: dict[str, Any],
        *,
        for_enable: bool | None = None,
    ) -> tuple[ProcedureDefinition | None, ValidationResult, dict[str, dict[str, Any]]]:
        """Parse, resolve and validate one submitted definition.

        Returns the definition with every reference resolved, the findings, and
        the action index the caller may want to reuse. A None definition means the
        payload did not even parse into the domain model, and the findings say
        where.

        `for_enable` defaults to whether the payload asks to be enabled, which is
        what makes "save a draft with a dangling reference" work while "turn that
        draft on" is refused.
        """
        wants_enabled = bool(payload.get("enabled")) if for_enable is None else for_enable
        definition, result = build_definition(payload)
        available = await self.action_index(tenant_id, agent_id)
        if definition is None:
            return None, result, available

        resolve_references(definition, available)
        findings = validate_definition(
            definition, available=available, for_enable=wants_enabled
        )
        for issue in findings.errors:
            result.add(issue)
        for issue in findings.warnings:
            result.add(issue)
        return definition, result, available

    @staticmethod
    def _refuse(result: ValidationResult) -> None:
        """Turn a failed validation into the error the endpoint returns.

        The findings travel in `details` so the editor can address each one to its
        step, and the message names the first so a log line or a curl still says
        something useful.
        """
        if result.ok:
            return
        first = result.errors[0]
        raise ValidationError(
            f"{first.step_path + ': ' if first.step_path else ''}{first.message}",
            errors=[issue.model_dump(mode="json") for issue in result.errors],
            warnings=[issue.model_dump(mode="json") for issue in result.warnings],
        )

    # ------------------------------------------------------------------ writes

    async def create(
        self,
        tenant_id: UUID,
        agent_id: UUID,
        payload: dict[str, Any],
        *,
        created_by: UUID | None = None,
        library_key: str | None = None,
        dedupe_name: bool = False,
    ) -> dict[str, Any]:
        """Validate and store one new procedure.

        `dedupe_name` is set by the template installer, whose documented status
        codes have no 409: installing the same gallery entry twice appends a
        suffix rather than refusing.
        """
        definition, result, _ = await self.prepare(tenant_id, agent_id, payload)
        self._refuse(result)
        if definition is None:  # pragma: no cover - _refuse raises first
            raise ValidationError("the procedure is not valid")

        name = definition.name
        for attempt in range(1, MAX_NAME_ATTEMPTS + 1):
            row = await self._repo.create(
                tenant_id,
                agent_id,
                name=name,
                trigger_text=definition.trigger_text,
                steps=[step.model_dump(mode="json") for step in definition.steps],
                required_actions=definition.resolved_action_ids(),
                redact_transcript_on_escalation=(
                    definition.redact_transcript_on_escalation
                ),
                enabled=bool(payload.get("enabled")),
                library_key=library_key,
                created_by=created_by,
            )
            if row is not None:
                row["triggered_count"] = 0
                row["resolved_count"] = 0
                return row
            if not dedupe_name:
                raise ConflictError(f"a procedure named {name!r} already exists")
            name = f"{definition.name} {attempt + 1}"
        raise ConflictError(f"a procedure named {definition.name!r} already exists")

    async def replace(
        self, tenant_id: UUID, procedure_id: UUID, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Full replacement of a definition, bumping `version`.

        The agent is read from the stored row rather than taken from the request:
        a procedure cannot move between agents through an edit, because its
        references were resolved against one agent's action set. Moving is
        `duplicate`, which copies.
        """
        existing = await self._repo.get(tenant_id, procedure_id)
        if existing is None:
            raise NotFoundError("no such procedure")
        agent_id = UUID(str(existing["agent_id"]))

        definition, result, _ = await self.prepare(tenant_id, agent_id, payload)
        self._refuse(result)
        if definition is None:  # pragma: no cover - _refuse raises first
            raise ValidationError("the procedure is not valid")

        row = await self._repo.replace(
            tenant_id,
            procedure_id,
            name=definition.name,
            trigger_text=definition.trigger_text,
            steps=[step.model_dump(mode="json") for step in definition.steps],
            required_actions=definition.resolved_action_ids(),
            redact_transcript_on_escalation=definition.redact_transcript_on_escalation,
            enabled=bool(payload.get("enabled")),
        )
        if row is None:
            raise NotFoundError("no such procedure")
        return row

    async def delete(self, tenant_id: UUID, procedure_id: UUID) -> list[dict[str, Any]]:
        """Delete a definition, closing anything still running against it.

        A live run cannot continue without its definition and its visitor is
        mid-flow, so every active run closes `failed` / `definition_deleted` with
        `outcome='handed_off'`. The returned rows name the conversations that were
        interrupted, which is what the endpoint audits and what a support lead
        needs in order to follow up.
        """
        existing = await self._repo.get(tenant_id, procedure_id)
        if existing is None:
            raise NotFoundError("no such procedure")

        closed = await self._repo.fail_active_runs(
            tenant_id,
            procedure_id,
            status="failed",
            outcome="handed_off",
            ended_reason="definition_deleted",
        )
        if not await self._repo.delete(tenant_id, procedure_id):
            raise NotFoundError("no such procedure")
        if closed:
            logger.info(
                "procedure_deleted_with_active_runs",
                tenant_id=str(tenant_id),
                procedure_id=str(procedure_id),
                runs=len(closed),
            )
        return closed

    async def duplicate(
        self,
        tenant_id: UUID,
        procedure_id: UUID,
        target_agent_id: UUID,
        *,
        created_by: UUID | None = None,
    ) -> dict[str, Any]:
        """Copy a procedure onto another agent, references re-resolved there.

        Refused when the target agent cannot run something the procedure needs:
        copying it anyway would produce a row that looks fine in the list and
        dead-ends in front of a visitor.
        """
        source = await self._repo.get(tenant_id, procedure_id)
        if source is None:
            raise NotFoundError("no such procedure")

        payload = {
            "name": source["name"],
            "trigger_text": source["trigger_text"],
            "steps": source["steps"],
            "redact_transcript_on_escalation": bool(
                source["redact_transcript_on_escalation"]
            ),
            "enabled": False,
        }
        definition, result, available = await self.prepare(
            tenant_id, target_agent_id, payload, for_enable=True
        )
        if definition is None:
            self._refuse(result)
        missing = [
            name
            for name in (definition.action_names if definition else [])
            if not (
                available.get(name)
                and available[name].get("linked")
                and available[name].get("enabled")
            )
        ]
        if missing:
            raise ValidationError(
                "the target agent cannot run "
                + ", ".join(f"@{name}" for name in missing)
                + "; enable those actions on it first"
            )
        self._refuse(result)

        payload["enabled"] = bool(source["enabled"])
        return await self.create(
            tenant_id,
            target_agent_id,
            payload,
            created_by=created_by,
            library_key=source.get("library_key"),
            dedupe_name=True,
        )

    # ------------------------------------------------------- template installs

    @staticmethod
    def templates() -> tuple[procedure_library.ProcedureTemplate, ...]:
        """The authored gallery entries, in gallery order."""
        return procedure_library.gallery()

    @staticmethod
    def presets() -> tuple[procedure_library.ProcedurePreset, ...]:
        """The wording variants. Each installs as one procedure off its base."""
        return procedure_library.PRESETS

    async def install(
        self,
        tenant_id: UUID,
        agent_id: UUID,
        *,
        template_key: str | None = None,
        preset_key: str | None = None,
        created_by: UUID | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Install one gallery entry onto an agent, with the actions it needs.

        Returns the stored procedure and the names of any actions created along
        the way, which the endpoint reports so the UI can say *"connect these
        two before turning it on"* instead of leaving the tenant to discover it.

        The installed procedure is always disabled. Most gallery entries reference
        an action that needs a credential, and a disabled row that a person turns
        on deliberately is the difference between a template and a surprise.
        """
        if bool(template_key) == bool(preset_key):
            raise ValidationError("send exactly one of template_key or preset_key")
        key = template_key or preset_key or ""
        record = procedure_library.installable(key)
        if record is None:
            raise NotFoundError(f"no procedure template named {key!r}")

        installed = await self._ensure_actions(
            tenant_id, agent_id, record.action_names, created_by=created_by
        )
        payload: dict[str, Any] = {
            "name": record.name,
            "trigger_text": record.trigger_text,
            "steps": [dict(step) for step in record.steps],
            "redact_transcript_on_escalation": record.redact_transcript_on_escalation,
            "enabled": False,
        }
        row = await self.create(
            tenant_id,
            agent_id,
            payload,
            created_by=created_by,
            library_key=record.library_key,
            dedupe_name=True,
        )
        return row, installed

    async def _ensure_actions(
        self,
        tenant_id: UUID,
        agent_id: UUID,
        names: tuple[str, ...],
        *,
        created_by: UUID | None = None,
    ) -> list[str]:
        """Make every action a template names resolvable from this agent.

        Three cases. Already linked: nothing to do. Owned by the tenant but not
        connected to this agent: link it, because the tenant already decided they
        want it. Absent: create it from the shipped action catalogue, disabled and
        flagged `setup_required`, then link it.

        A name the catalogue does not know is refused. It means the template
        references something no deployment can build, which is a defect in the
        template rather than a condition to work around, and a procedure saved
        around it could never be enabled anyway.
        """
        if not names:
            return []
        index = await self.action_index(tenant_id, agent_id)
        repo = getattr(self._actions, "repo", None)
        installed: list[str] = []
        for name in names:
            present = index.get(name)
            if present is not None:
                if not present.get("linked"):
                    if repo is None:
                        raise ValidationError(
                            f"@{name} exists but is not connected to this agent, "
                            "and actions cannot be connected on this deployment"
                        )
                    await repo.link_agent(
                        tenant_id, agent_id, UUID(str(present["id"]))
                    )
                continue

            catalogue = action_library.entry(name)
            if catalogue is None:
                raise ValidationError(
                    f"this template references @{name}, which is not an action "
                    "this deployment knows how to create"
                )
            if repo is None:
                raise ValidationError(
                    f"@{name} is not installed and actions are unavailable on "
                    "this deployment"
                )
            row = await repo.get_by_name(tenant_id, name)
            if row is None:
                row = await repo.create(
                    tenant_id,
                    catalogue.definition,
                    enabled=catalogue.enabled_by_default,
                    library_key=catalogue.key,
                    setup_required=catalogue.setup_required,
                    created_by=created_by,
                )
                installed.append(name)
            await repo.link_agent(tenant_id, agent_id, UUID(str(row["id"])))
        return installed

    # ------------------------------------------------------------------- reads

    async def list_for_agent(
        self, tenant_id: UUID, agent_id: UUID
    ) -> list[dict[str, Any]]:
        """The agent's procedures with their lifetime counts, for the list page."""
        return await self._repo.list_for_agent(tenant_id, agent_id)

    async def get(self, tenant_id: UUID, procedure_id: UUID) -> dict[str, Any]:
        """One procedure, or a 404. Every write path starts here."""
        row = await self._repo.get(tenant_id, procedure_id)
        if row is None:
            raise NotFoundError("no such procedure")
        return row

    async def performance(
        self, tenant_id: UUID, procedure_id: UUID, *, days: int = 30
    ) -> dict[str, Any]:
        """The Performance tab in one response: cards, daily bars, recent runs.

        `resolved` is renamed to `completed` on the way out because that is what
        the tab's card is labelled, and a status name leaking into a UI label is
        how a rename in the enum becomes a copy change.
        """
        await self.get(tenant_id, procedure_id)
        window = max(1, min(int(days), MAX_TREND_DAYS))
        totals = await self._repo.performance(tenant_id, procedure_id, days=window)
        points = await self._repo.trend(tenant_id, procedure_id, days=window)
        recent = await self._repo.recent_runs(tenant_id, procedure_id)
        return {
            "days": window,
            "triggered": totals["triggered"],
            "completed": totals["resolved"],
            "handed_off": totals["handed_off"],
            "pending": totals["pending"],
            "points": [
                {
                    "day": point["day"],
                    "triggered": point["triggered"],
                    "completed": point["resolved"],
                    "handed_off": point["handed_off"],
                }
                for point in points
            ],
            "recent_runs": recent,
        }

    async def runs(
        self,
        tenant_id: UUID,
        procedure_id: UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of this procedure's runs, newest first, plus the total."""
        await self.get(tenant_id, procedure_id)
        return await self._repo.list_runs(
            tenant_id, procedure_id, limit=limit, offset=offset
        )

    async def run_detail(self, tenant_id: UUID, run_id: UUID) -> dict[str, Any]:
        """One run with its timeline attached, for the support drawer."""
        row = await self._repo.get_run(tenant_id, run_id)
        if row is None:
            raise NotFoundError("no such procedure run")
        row["timeline"] = await self._repo.run_steps(tenant_id, run_id)
        return row

    async def close_run(
        self, tenant_id: UUID, run_id: UUID, *, reason: str = "force_closed"
    ) -> dict[str, Any]:
        """Close a run a person has taken over.

        Refused with a conflict rather than a 404 when the run is already closed:
        the row exists, the operation does not apply to it, and saying so is what
        stops two leads from both thinking they intervened.
        """
        if reason not in {"superseded", "force_closed"}:
            raise ValidationError(
                "reason must be 'superseded' or 'force_closed'"
            )
        row = await self._repo.force_close(tenant_id, run_id, reason=reason)
        if row is not None:
            return row
        if await self._repo.get_run(tenant_id, run_id) is None:
            raise NotFoundError("no such procedure run")
        raise ConflictError("that run is already closed")
