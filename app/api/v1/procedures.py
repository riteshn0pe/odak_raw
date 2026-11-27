"""Procedures: the authoring surface behind the Procedures screen.

Five things here are deliberate.

**The step tree is validated by the domain model, not by this module's request
schemas.** `steps` arrives as JSON and goes straight to `ProcedureDefinition`,
which owns the limits — fifteen instruction steps across the whole tree, five arms
per decision, one level of branching — and the lint rules that address a finding to
a step. A second, weaker copy of those rules in a FastAPI schema would produce a
raw 422 with a Pydantic location string instead of the per-step findings the editor
renders, and the two copies would drift.

**An update is a full replacement.** A step tree cannot be merged field by field
without ambiguity about reordering, so `PUT` takes the whole definition, bumps
`version`, and recomputes `required_actions` from the `@name` tokens in the text.
`enabled` is the one field that survives being omitted, because the editor's Save
should not silently turn a live procedure off.

**`/procedures/templates` is declared before `/procedures/{procedure_id}`.** Route
order decides which pattern matches, and the gallery would otherwise be read as a
procedure id and answer 422 for every tenant.

**Everything a procedure needs is installed with it.** `from-template` creates any
action the entry references that the tenant does not have — disabled, flagged
`setup_required`, linked to the agent — and reports what it created, so the UI can
say which two credentials to connect rather than leaving an SOP that dead-ends.

**Force-closing a run is an owner or admin action and is audited.** It is the only
way a run reaches `superseded`, which is what makes that status readable as "a
person intervened here" rather than something the runtime did.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.api.deps import ContainerDep, TenantDep, require_role
from app.core.db import tenant_scope
from app.core.errors import AuthorizationError
from app.domain.entities import TenantContext
from app.services import audit
from app.services.audit import (
    AUDIT_PROCEDURE_CREATED,
    AUDIT_PROCEDURE_DELETED,
    AUDIT_PROCEDURE_DISABLED,
    AUDIT_PROCEDURE_DUPLICATED,
    AUDIT_PROCEDURE_ENABLED,
    AUDIT_PROCEDURE_INSTALLED_FROM_TEMPLATE,
    AUDIT_PROCEDURE_RUN_FORCE_CLOSED,
    AUDIT_PROCEDURE_UPDATED,
)

router = APIRouter(tags=["procedures"])

# The Performance tab's window. Ninety days is the repository's own clamp; this is
# the same number stated where the query string is documented.
MAX_PERFORMANCE_DAYS = 90
DEFAULT_PERFORMANCE_DAYS = 30

# One page of runs. The list renders a table, not a feed, so the page is small and
# the pager is real.
MAX_RUNS_PAGE = 200
DEFAULT_RUNS_PAGE = 50


# ---------------------------------------------------------------------- schemas


class ProcedureOut(BaseModel):
    """One list row.

    `triggered_count` and `resolved_count` are lifetime counters computed by the
    list query. They default to zero so a freshly created row serialises without a
    second read whose only purpose would be to return two zeroes.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    name: str
    trigger_text: str
    required_actions: list[UUID] = Field(default_factory=list)
    redact_transcript_on_escalation: bool = False
    enabled: bool = False
    version: int = 1
    library_key: str | None = None
    last_run_at: datetime | None = None
    triggered_count: int = 0
    resolved_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProcedureDetail(ProcedureOut):
    """A list row plus the step tree.

    `steps` is passed through as stored rather than re-modelled here: the domain
    model is the authority on its shape, and a second schema in the transport
    layer would quietly drop any field it had not been taught about.
    """

    steps: list[dict[str, Any]] = Field(default_factory=list)


class ProcedureInstalled(ProcedureDetail):
    """What `from-template` returns.

    `installed_actions` names the actions created during the install. They are
    disabled and need a credential, and telling the tenant which ones up front is
    the difference between "connect these two" and an SOP that fails on step 4.
    """

    installed_actions: list[str] = Field(default_factory=list)


class ProcedureIn(BaseModel):
    """An authored definition. Full replacement on `PUT`, not a patch.

    `steps` is deliberately untyped here. `ProcedureDefinition` validates it and
    reports per-step findings, which is what the editor renders.
    """

    name: str
    trigger_text: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    redact_transcript_on_escalation: bool = False
    enabled: bool | None = None


class ProcedureLint(BaseModel):
    """One validator error or advisory warning, addressed to a step."""

    sid: str | None = None
    code: str
    message: str
    step_path: str | None = None
    severity: str = "error"


class ProcedureValidateResult(BaseModel):
    """The dry run. Warnings do not block a save; errors do."""

    valid: bool
    errors: list[ProcedureLint] = Field(default_factory=list)
    warnings: list[ProcedureLint] = Field(default_factory=list)


class ProcedureTemplateOut(BaseModel):
    """One gallery entry. Platform data from code, identical for every tenant."""

    key: str
    name: str
    description: str
    trigger_text: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    required_action_keys: list[str] = Field(default_factory=list)
    agent_templates: list[str] = Field(default_factory=list)


class ProcedurePresetOut(BaseModel):
    """A wording variant of a gallery entry, installed the same way."""

    key: str
    name: str
    description: str
    procedure_keys: list[str] = Field(default_factory=list)


class ProcedureTemplateGallery(BaseModel):
    """The whole gallery in one response, so the picker renders in one pass."""

    templates: list[ProcedureTemplateOut] = Field(default_factory=list)
    presets: list[ProcedurePresetOut] = Field(default_factory=list)


class ProcedureFromTemplateIn(BaseModel):
    """One key, not both."""

    template_key: str | None = None
    preset_key: str | None = None


class ProcedureDuplicateIn(BaseModel):
    """The agent to copy onto. A procedure is copied, never shared."""

    agent_id: UUID


class ProcedureRunOut(BaseModel):
    """One run.

    `procedure_name` and `procedure_version` are denormalised onto the run so it
    stays readable after a rename or a delete, which is also why `procedure_id`
    and `agent_id` are nullable.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID | None = None
    conversation_id: UUID
    procedure_id: UUID | None = None
    procedure_name: str
    procedure_version: int = 1
    status: str
    outcome: str | None = None
    ended_reason: str | None = None
    step_index: int = 0
    step_path: str = ""
    turn_count: int = 0
    action_calls: int = 0
    escalation_id: UUID | None = None
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    ended_at: datetime | None = None


class ProcedureRunPage(BaseModel):
    """One page of runs plus the total, for the pager."""

    items: list[ProcedureRunOut] = Field(default_factory=list)
    total: int = 0


class ProcedureVariableOut(BaseModel):
    """A collected value with its provenance.

    The provenance is the point: a lead has to be able to see that a total came
    from the action rather than from what the customer claimed, so `source` is
    never dropped.
    """

    value: Any = None
    source: str = ""
    at: datetime | None = None


class ProcedureBranchLogEntryOut(BaseModel):
    """One decision, recording whether code or the model decided, and why."""

    sid: str
    step_path: str = ""
    branch_taken: str | None = None
    decided_by: str = "model"
    why: str | None = None
    at: datetime | None = None


class ProcedureRunStepOut(BaseModel):
    """One row of the run timeline, ordered by `ordinal`."""

    ordinal: int
    sid: str
    step_path: str = ""
    status: str
    note: str | None = None
    at: datetime | None = None


class ProcedureRunDetail(ProcedureRunOut):
    """Everything a support conversation needs about one run.

    `steps_snapshot` is the tree as it was when the run started, which is what
    makes a run immune to a mid-run edit and still readable after a delete.
    """

    steps_snapshot: list[dict[str, Any]] = Field(default_factory=list)
    variables: dict[str, ProcedureVariableOut] = Field(default_factory=dict)
    branch_log: list[ProcedureBranchLogEntryOut] = Field(default_factory=list)
    timeline: list[ProcedureRunStepOut] = Field(default_factory=list)
    bookkeeping_calls: int = 0
    current_sid: str | None = None
    expires_at: datetime | None = None


class ProcedureRunCloseIn(BaseModel):
    """Close a run a person has taken over."""

    reason: Literal["superseded", "force_closed"] = "force_closed"


class ProcedureTrendPoint(BaseModel):
    """One day's bar. Gap-filled by the query, so no day is missing."""

    day: str | None = None
    triggered: int = 0
    completed: int = 0
    handed_off: int = 0


class ProcedurePerformance(BaseModel):
    """The Performance tab: three cards, the daily bars, and the last runs."""

    days: int = DEFAULT_PERFORMANCE_DAYS
    triggered: int = 0
    completed: int = 0
    handed_off: int = 0
    pending: int = 0
    points: list[ProcedureTrendPoint] = Field(default_factory=list)
    recent_runs: list[ProcedureRunOut] = Field(default_factory=list)


# ---------------------------------------------------------------------- helpers


def _service(container: Any) -> Any:
    """The procedures service off the container.

    Reached through one helper rather than by attribute access at sixteen call
    sites: the container names its dependencies as cached properties, and keeping
    the name in one place here means the wiring is stated once.
    """
    return container.procedure_service


async def _record(
    session: Any,
    tenant: TenantContext,
    action: str,
    *,
    target_id: str,
    target_type: str = "procedure",
    detail: dict[str, Any] | None = None,
) -> None:
    """Write an audit row inside the caller's transaction.

    The acting user's email is resolved from the same session so the trail names
    who published or force-closed something rather than only their id.
    """
    actor = (
        (
            await session.execute(
                text("SELECT email FROM users WHERE id = :id"),
                {"id": tenant.user_id},
            )
        )
        .mappings()
        .first()
    )
    await audit.record(
        action,
        actor_type="user",
        actor_id=tenant.user_id,
        actor_email=actor["email"] if actor else None,
        target_tenant=tenant.tenant_id,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
        session=session,
    )


async def _audit(
    tenant: TenantContext,
    action: str,
    *,
    target_id: str,
    target_type: str = "procedure",
    detail: dict[str, Any] | None = None,
) -> None:
    """Audit a mutation whose write happened in the repository's transaction.

    The repository owns its session, so this cannot share it. That is a knowingly
    accepted race — the row is written immediately after a committed change — and
    it matches how the actions surface does it.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        await _record(
            session,
            tenant,
            action,
            target_id=target_id,
            target_type=target_type,
            detail=detail,
        )


async def _guard_enabled(container: Any) -> None:
    """The deployment switch, checked before any write.

    Reads are not gated, because a tenant on a deployment that turns procedures
    off must still be able to see and remove what they built.
    """
    if not bool(getattr(container.settings, "PROCEDURES_ENABLED", False)):
        raise AuthorizationError("procedures are not enabled on this deployment")


def _lints(issues: list[dict[str, Any]]) -> list[ProcedureLint]:
    return [ProcedureLint.model_validate(issue) for issue in issues]


def _description(trigger_text: str) -> str:
    """The gallery card's one-line description.

    The library entries carry no separate description — their trigger text *is*
    the plain-English statement of when the procedure applies, which is exactly
    what the card needs — so it is reused rather than duplicated in code that
    would then have to be kept in step with it.
    """
    text_value = trigger_text.strip()
    return text_value[0].upper() + text_value[1:] if text_value else ""


# --------------------------------------------------------------------- gallery
#
# Declared before /procedures/{procedure_id}: route order decides which pattern
# matches, and "templates" would otherwise be parsed as a procedure id.


@router.get("/procedures/templates", response_model=ProcedureTemplateGallery)
async def list_procedure_templates(
    tenant: TenantDep, container: ContainerDep
) -> ProcedureTemplateGallery:
    """The static gallery: authored templates and their wording variants.

    Platform data served from code, so the response is identical for every tenant
    and there is no RLS involved. It still requires a session, because the picker
    is a dashboard screen and an unauthenticated catalogue is a free content feed.
    """
    service = _service(container)
    templates = [
        ProcedureTemplateOut(
            key=item.library_key,
            name=item.name,
            description=_description(item.trigger_text),
            trigger_text=item.trigger_text,
            steps=[dict(step) for step in item.steps],
            required_action_keys=list(item.action_names),
            agent_templates=list(item.installed_by),
        )
        for item in service.templates()
    ]
    presets = [
        ProcedurePresetOut(
            key=item.library_key,
            name=item.name,
            description=_description(item.trigger_text),
            procedure_keys=[item.library_key],
        )
        for item in service.presets()
    ]
    _ = tenant
    return ProcedureTemplateGallery(templates=templates, presets=presets)


# ------------------------------------------------------------- per-agent surface


@router.get("/agents/{agent_id}/procedures", response_model=list[ProcedureOut])
async def list_procedures(
    agent_id: UUID, tenant: TenantDep, container: ContainerDep
) -> list[ProcedureOut]:
    """Every procedure on one agent, enabled first by recent activity.

    The lifetime counters come from one grouped subquery rather than a counter
    column, so turning a procedure on does not add a write to the hot path.
    """
    rows = await _service(container).list_for_agent(tenant.tenant_id, agent_id)
    return [ProcedureOut.model_validate(row) for row in rows]


@router.post(
    "/agents/{agent_id}/procedures",
    response_model=ProcedureDetail,
    status_code=201,
    dependencies=[require_role("owner", "admin")],
)
async def create_procedure(
    agent_id: UUID,
    payload: ProcedureIn,
    tenant: TenantDep,
    container: ContainerDep,
) -> ProcedureDetail:
    """Publish a new procedure.

    Every `@name` in the step text is resolved against the actions linked to this
    agent and denormalised into `required_actions`. A reference that does not
    resolve is a warning on save and a refusal on enable, because a run that
    collects an order number and then finds step 7 cannot fire is the failure this
    whole design exists to prevent.
    """
    await _guard_enabled(container)

    row = await _service(container).create(
        tenant.tenant_id, agent_id, payload.model_dump(), created_by=tenant.user_id
    )
    detail = {
        "agent_id": str(agent_id),
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "steps": len(row.get("steps") or []),
        "required_actions": len(row.get("required_actions") or []),
    }
    async with tenant_scope(tenant.tenant_id) as session:
        await _record(
            session,
            tenant,
            AUDIT_PROCEDURE_CREATED,
            target_id=str(row["id"]),
            detail=detail,
        )
        if row["enabled"]:
            await _record(
                session,
                tenant,
                AUDIT_PROCEDURE_ENABLED,
                target_id=str(row["id"]),
                detail={"name": row["name"]},
            )
    return ProcedureDetail.model_validate(row)


@router.post(
    "/agents/{agent_id}/procedures/from-template",
    response_model=ProcedureInstalled,
    status_code=201,
    dependencies=[require_role("owner", "admin")],
)
async def install_procedure_template(
    agent_id: UUID,
    payload: ProcedureFromTemplateIn,
    tenant: TenantDep,
    container: ContainerDep,
) -> ProcedureInstalled:
    """Install one gallery entry, with whatever actions it needs.

    The installed procedure is always disabled: most entries reference an action
    that needs a credential first, and a disabled row somebody turns on
    deliberately is the difference between a template and a surprise.
    """
    await _guard_enabled(container)

    row, installed = await _service(container).install(
        tenant.tenant_id,
        agent_id,
        template_key=payload.template_key,
        preset_key=payload.preset_key,
        created_by=tenant.user_id,
    )
    await _audit(
        tenant,
        AUDIT_PROCEDURE_INSTALLED_FROM_TEMPLATE,
        target_id=str(row["id"]),
        detail={
            "agent_id": str(agent_id),
            "key": payload.template_key or payload.preset_key,
            "name": row["name"],
            "installed_actions": installed,
        },
    )
    return ProcedureInstalled.model_validate({**row, "installed_actions": installed})


# ----------------------------------------------------------- one procedure


@router.get("/procedures/{procedure_id}", response_model=ProcedureDetail)
async def get_procedure(
    procedure_id: UUID, tenant: TenantDep, container: ContainerDep
) -> ProcedureDetail:
    """One procedure with its step tree, for the editor."""
    row = await _service(container).get(tenant.tenant_id, procedure_id)
    return ProcedureDetail.model_validate(row)


@router.put(
    "/procedures/{procedure_id}",
    response_model=ProcedureDetail,
    dependencies=[require_role("owner", "admin")],
)
async def replace_procedure(
    procedure_id: UUID,
    payload: ProcedureIn,
    tenant: TenantDep,
    container: ContainerDep,
) -> ProcedureDetail:
    """Full replacement. The server bumps `version` and re-resolves references.

    An omitted `enabled` keeps the stored value rather than defaulting to off: the
    editor's Save must never quietly take a live procedure out of service.
    """
    await _guard_enabled(container)
    existing = await _service(container).get(tenant.tenant_id, procedure_id)

    body = payload.model_dump()
    if body.get("enabled") is None:
        body["enabled"] = bool(existing["enabled"])

    row = await _service(container).replace(
        tenant.tenant_id, procedure_id, body
    )
    async with tenant_scope(tenant.tenant_id) as session:
        await _record(
            session,
            tenant,
            AUDIT_PROCEDURE_UPDATED,
            target_id=str(procedure_id),
            detail={
                "name": row["name"],
                "version": row["version"],
                "steps": len(row.get("steps") or []),
                "required_actions": len(row.get("required_actions") or []),
            },
        )
        if bool(row["enabled"]) != bool(existing["enabled"]):
            await _record(
                session,
                tenant,
                AUDIT_PROCEDURE_ENABLED
                if row["enabled"]
                else AUDIT_PROCEDURE_DISABLED,
                target_id=str(procedure_id),
                detail={"name": row["name"]},
            )
    return ProcedureDetail.model_validate(row)


@router.delete(
    "/procedures/{procedure_id}",
    status_code=204,
    dependencies=[require_role("owner", "admin")],
)
async def delete_procedure(
    procedure_id: UUID, tenant: TenantDep, container: ContainerDep
) -> None:
    """Delete a procedure, closing anything still running against it.

    A live run cannot continue without its definition and its visitor is mid-flow,
    so each one closes `failed` / `definition_deleted` and is handed to a person.
    The count of interrupted runs is audited, because that is the number a support
    lead has to follow up on.
    """
    existing = await _service(container).get(tenant.tenant_id, procedure_id)
    closed = await _service(container).delete(tenant.tenant_id, procedure_id)
    await _audit(
        tenant,
        AUDIT_PROCEDURE_DELETED,
        target_id=str(procedure_id),
        detail={
            "name": existing["name"],
            "agent_id": str(existing["agent_id"]),
            "interrupted_runs": len(closed),
            "interrupted_conversations": [
                str(run["conversation_id"]) for run in closed
            ],
        },
    )


@router.post("/procedures/{procedure_id}/validate", response_model=ProcedureValidateResult)
async def validate_procedure(
    procedure_id: UUID,
    tenant: TenantDep,
    container: ContainerDep,
    payload: ProcedureIn | None = None,
) -> ProcedureValidateResult:
    """Dry run the validator. Saves nothing.

    With no body, the stored definition is re-checked, which is how the editor
    answers "would this still publish?" after an action was disabled underneath
    it. Warnings — the vague-condition lint above all — are advisory: a procedure
    with warnings and no errors saves.
    """
    existing = await _service(container).get(tenant.tenant_id, procedure_id)
    agent_id = UUID(str(existing["agent_id"]))

    if payload is None:
        candidate: dict[str, Any] = {
            "name": existing["name"],
            "trigger_text": existing["trigger_text"],
            "steps": existing.get("steps") or [],
            "redact_transcript_on_escalation": bool(
                existing["redact_transcript_on_escalation"]
            ),
            "enabled": bool(existing["enabled"]),
        }
    else:
        candidate = payload.model_dump()
        if candidate.get("enabled") is None:
            candidate["enabled"] = bool(existing["enabled"])

    _, result, _ = await _service(container).prepare(
        tenant.tenant_id, agent_id, candidate
    )
    return ProcedureValidateResult(
        valid=result.ok,
        errors=_lints([issue.model_dump(mode="json") for issue in result.errors]),
        warnings=_lints([issue.model_dump(mode="json") for issue in result.warnings]),
    )


@router.post(
    "/procedures/{procedure_id}/duplicate",
    response_model=ProcedureDetail,
    status_code=201,
    dependencies=[require_role("owner", "admin")],
)
async def duplicate_procedure(
    procedure_id: UUID,
    payload: ProcedureDuplicateIn,
    tenant: TenantDep,
    container: ContainerDep,
) -> ProcedureDetail:
    """Copy a procedure onto another agent.

    A copy, never a link: the steps name actions that must be enabled on their
    agent, and the trigger competes with that agent's other triggers for the
    model's attention, so one row shared across two agents breaks silently.
    """
    await _guard_enabled(container)

    row = await _service(container).duplicate(
        tenant.tenant_id,
        procedure_id,
        payload.agent_id,
        created_by=tenant.user_id,
    )
    await _audit(
        tenant,
        AUDIT_PROCEDURE_DUPLICATED,
        target_id=str(row["id"]),
        detail={
            "source_procedure_id": str(procedure_id),
            "agent_id": str(payload.agent_id),
            "name": row["name"],
        },
    )
    return ProcedureDetail.model_validate(row)


# --------------------------------------------------------------------- analytics


@router.get(
    "/procedures/{procedure_id}/performance", response_model=ProcedurePerformance
)
async def procedure_performance(
    procedure_id: UUID,
    tenant: TenantDep,
    container: ContainerDep,
    days: int = Query(DEFAULT_PERFORMANCE_DAYS, ge=1, le=MAX_PERFORMANCE_DAYS),
) -> ProcedurePerformance:
    """The three cards, the daily bars and the last twenty runs, in one response.

    Computed over a window rather than read from counters: a counter cannot answer
    "this month", and keeping one would put a write on every run start.
    """
    report = await _service(container).performance(
        tenant.tenant_id, procedure_id, days=days
    )
    return ProcedurePerformance.model_validate(report)


@router.get("/procedures/{procedure_id}/runs", response_model=ProcedureRunPage)
async def list_procedure_runs(
    procedure_id: UUID,
    tenant: TenantDep,
    container: ContainerDep,
    limit: int = Query(DEFAULT_RUNS_PAGE, ge=1, le=MAX_RUNS_PAGE),
    offset: int = Query(0, ge=0),
) -> ProcedureRunPage:
    """One page of runs, newest first.

    `steps_snapshot` is not selected here — a few KB per row that the table does
    not render. The run detail endpoint carries it.
    """
    items, total = await _service(container).runs(
        tenant.tenant_id, procedure_id, limit=limit, offset=offset
    )
    return ProcedureRunPage(
        items=[ProcedureRunOut.model_validate(row) for row in items], total=total
    )


@router.get("/procedure-runs/{run_id}", response_model=ProcedureRunDetail)
async def get_procedure_run(
    run_id: UUID, tenant: TenantDep, container: ContainerDep
) -> ProcedureRunDetail:
    """One run: its state, its collected variables with provenance, its timeline.

    Addressed on its own rather than under its procedure, because `procedure_id`
    is null once the definition is deleted and the run still has to be readable.
    """
    row = await _service(container).run_detail(tenant.tenant_id, run_id)
    return ProcedureRunDetail.model_validate(row)


@router.post(
    "/procedure-runs/{run_id}/close",
    response_model=ProcedureRunOut,
    dependencies=[require_role("owner", "admin")],
)
async def close_procedure_run(
    run_id: UUID,
    payload: ProcedureRunCloseIn,
    tenant: TenantDep,
    container: ContainerDep,
) -> ProcedureRunOut:
    """Close a run a person has taken over.

    This is the only way a run reaches `superseded` — the runtime never writes it —
    which is what makes that status readable as "a person intervened here". An
    already-closed run answers 409 rather than pretending something happened.
    """
    row = await _service(container).close_run(
        tenant.tenant_id, run_id, reason=payload.reason
    )
    await _audit(
        tenant,
        AUDIT_PROCEDURE_RUN_FORCE_CLOSED,
        target_id=str(run_id),
        target_type="procedure_run",
        detail={
            "reason": payload.reason,
            "procedure_id": str(row["procedure_id"]) if row["procedure_id"] else None,
            "procedure_name": row["procedure_name"],
            "conversation_id": str(row["conversation_id"]),
            "step_path": row.get("step_path"),
        },
    )
    return ProcedureRunOut.model_validate(row)
