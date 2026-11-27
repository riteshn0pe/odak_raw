"""Tenant-facing actions surface: CRUD, library install, test, and
agent linking — the endpoints behind the Actions screen.

Three things here are deliberate and easy to get wrong later.

**Referential integrity for procedure steps is enforced in this module, not by a
database constraint.** A procedure references an action from inside its `steps`
JSONB, so no foreign key can see it; `procedures.required_actions` is the array
the publisher resolves those tokens into, and a delete or an unlink checks that
array first. A missing check here is a procedure that fails at runtime in front
of a visitor instead of at edit time in front of an admin.

**The list endpoint returns the catalogue as well as the installed rows.** The
screen shows "install this" tiles beside live actions, so serving them from two
endpoints would render it in two stages with a visible reflow. The library is
platform data in code, so there is nothing to join and no RLS involved.

**No read path returns a credential.** `PUT /actions/{id}/secret` takes a
plaintext value, encrypts it into `tenant_secrets`, and returns the reference
only. Every other response carries `has_secret` or a list of names.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticError
from sqlalchemy import text

from app.api.deps import ContainerDep, TenantDep, require_role
from app.core.constants import (
    AUDIT_ACTION_CREATED,
    AUDIT_ACTION_DELETED,
    AUDIT_ACTION_DISABLED,
    AUDIT_ACTION_ENABLED,
    AUDIT_ACTION_LINKED_TO_AGENT,
    AUDIT_ACTION_SECRET_DELETED,
    AUDIT_ACTION_SECRET_ROTATED,
    AUDIT_ACTION_SECRET_SET,
    AUDIT_ACTION_TESTED,
    AUDIT_ACTION_TRANSCRIPT_EGRESS,
    AUDIT_ACTION_UNLINKED_FROM_AGENT,
    AUDIT_ACTION_UPDATED,
    RL_SCOPE_ACTION_ONE,
    Channel,
)
from app.core.db import tenant_scope
from app.core.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.core.security import encrypt_secret
from app.domain.actions import (
    SECRET_TEMPLATE_RE,
    ActionContext,
    ActionDefinition,
)
from app.domain.entities import TenantContext
from app.services import audit
from app.services.actions import library

router = APIRouter(tags=["actions"])

# The window the Actions list summarises. Seven days is what makes "used 84 times
# this week" true; the per-action activity endpoint takes ?days= for the rest.
LIST_WINDOW_DAYS = 7

# The test endpoint is the one place a tenant can make us originate an outbound
# request on demand, so it gets its own tight window on top of the runtime limits.
TEST_CALLS_PER_MINUTE = 10


# --------------------------------------------------------------------- helpers


async def _record(
    session: Any,
    tenant: TenantContext,
    action: str,
    *,
    target_id: str,
    target_type: str = "action",
    detail: dict[str, Any] | None = None,
) -> None:
    """Write an audit row inside the caller's transaction.

    The acting user's email is resolved from the same session so the tenant's own
    trail names who created, tested or deleted an action rather than only its id.
    """
    actor = (
        await session.execute(
            text("SELECT email FROM users WHERE id = :id"),
            {"id": tenant.user_id},
        )
    ).mappings().first()
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
    target_type: str = "action",
    detail: dict[str, Any] | None = None,
) -> None:
    """Audit a mutation whose write happened in the repository's own transaction.

    The repository owns its session, so this cannot share it. That is a knowingly
    accepted race — the row is written immediately after a committed change — and
    it is confined to the paths where no session is already open.
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


def _first_error(exc: PydanticError) -> str:
    """One readable line out of a Pydantic failure.

    The UI shows this next to the field, so the location matters as much as the
    message; a bare "validation error" sends the author hunting.
    """
    for item in exc.errors():
        where = ".".join(str(part) for part in item.get("loc", ()))
        message = str(item.get("msg", "is invalid"))
        return f"{where}: {message}" if where else message
    return "the action definition is invalid"


def _definition(raw: Any) -> ActionDefinition:
    """Validate an authored or stored definition into the domain model."""
    payload: Any = raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValidationError("definition is not valid JSON") from exc
    try:
        return ActionDefinition.model_validate(payload)
    except PydanticError as exc:
        raise ValidationError(_first_error(exc)) from exc


def _owes_transcript_audit(definition: ActionDefinition) -> bool:
    """`include_transcript` is an egress of conversation content, audited when
    enabled. Returns whether the audit row is owed, so the caller writes it
    once, on the same path that persisted the change."""
    return definition.include_transcript


def _channels(raw: Any) -> list[str]:
    if isinstance(raw, list | tuple):
        return [str(item) for item in raw]
    return []


def _secret_name(name: str) -> str:
    """Named secrets are upper-case by construction so a `{{secret.X}}` hole can
    never be satisfied by conversation data. Validated against the same pattern
    the renderer matches, not a looser one."""
    if not SECRET_TEMPLATE_RE.match(f"{{{{secret.{name}}}}}"):
        raise ValidationError(
            "a secret name must be UPPER_SNAKE_CASE, 1-40 characters"
        )
    return name


async def _agent_exists(session: Any, agent_id: UUID) -> bool:
    row = (
        await session.execute(
            text("SELECT 1 FROM agents WHERE id = :id"), {"id": agent_id}
        )
    ).first()
    return row is not None


async def _procedures_using(
    session: Any, action_id: UUID, *, agent_id: UUID | None = None
) -> list[dict[str, Any]]:
    """Procedures whose `required_actions` names this action.

    The array is the publisher's resolution of the `@name` tokens inside `steps`,
    which is why this is a query and not a foreign key.
    """
    params: dict[str, Any] = {"action_id": action_id}
    sql = (
        "SELECT id, name FROM procedures WHERE :action_id = ANY (required_actions)"
    )
    if agent_id is not None:
        sql += " AND agent_id = :agent_id"
        params["agent_id"] = agent_id
    rows = (await session.execute(text(sql + " ORDER BY name"), params)).mappings().all()
    return [{"id": str(row["id"]), "name": row["name"]} for row in rows]


# ---------------------------------------------------------------------- models


class ActionListItem(BaseModel):
    """One row of the Actions screen — installed or offered."""

    id: UUID | None = None
    library_key: str | None = None
    name: str
    display_name: str
    description: str
    kind: str
    effect: str
    installed: bool = True
    enabled: bool = False
    setup_required: bool = False
    only_in_procedures: bool = False
    requires_confirmation: bool = False
    requires_verified_identity: bool = False
    channels: list[str] = Field(default_factory=list)
    system_handler: str | None = None
    target_host: str | None = None
    has_secret: bool = False
    requires: str = ""
    version: int = 1
    last_test_status: str | None = None
    last_tested_at: datetime | None = None
    calls_window: int = 0
    errors_window: int = 0
    last_called_at: datetime | None = None


class ActionDetail(BaseModel):
    id: UUID
    library_key: str | None = None
    definition: ActionDefinition
    enabled: bool
    setup_required: bool
    version: int
    secret_names: list[str] = Field(default_factory=list)
    agent_ids: list[UUID] = Field(default_factory=list)
    last_test_status: str | None = None
    last_tested_at: datetime | None = None
    last_called_at: datetime | None = None
    last_error: str | None = None
    calls_window: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ActionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # the request layer guards the nested shape here, so OpenAPI emits the real
    # ActionDefinition schema instead of a bare object. `ActionUpdate.definition`
    # deliberately stays a loose dict because it merges partials over the stored
    # definition, and half a definition is not a valid one
    definition: ActionDefinition
    enabled: bool = False


class ActionFromLibrary(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    library_key: str = Field(min_length=1, max_length=64)


class ActionUpdate(BaseModel):
    """Partial at the top level, whole-object at the definition level.

    `definition` is merged key-by-key over the stored one so the field picker can
    send only what it changed, but the merged result is re-validated as a whole —
    a definition is a security envelope, and half of one is not a valid state.
    """

    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any] | None = None
    enabled: bool | None = None
    setup_required: bool | None = None


class SecretPut(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=8192)


class SecretRef(BaseModel):
    name: str
    ref: str
    rotated: bool = False


class TestResult(BaseModel):
    """What the test panel and the field picker read. Never a credential."""

    ok: bool
    outcome: str
    http_status: int | None = None
    duration_ms: int = 0
    error: str | None = None
    sample: dict[str, Any] | None = None
    render: dict[str, Any] | None = None
    request: dict[str, Any] = Field(default_factory=dict)


class ActivityDay(BaseModel):
    day: date
    calls: int
    errors: int
    p50_ms: int | None = None
    p95_ms: int | None = None


class ActivityReport(BaseModel):
    days: int
    calls: int
    errors: int
    error_rate: float
    p50_ms: int | None = None
    p95_ms: int | None = None
    last_called_at: datetime | None = None
    last_error: str | None = None
    daily: list[ActivityDay] = Field(default_factory=list)
    recent: list[dict[str, Any]] = Field(default_factory=list)


class AgentActionLinks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_ids: list[UUID] = Field(default_factory=list, max_length=300)


def _detail(
    row: dict[str, Any],
    *,
    secret_names: list[str] | None = None,
    agent_ids: list[UUID] | None = None,
    metrics: dict[str, Any] | None = None,
) -> ActionDetail:
    stats = metrics or {}
    return ActionDetail(
        id=row["id"],
        library_key=row.get("library_key"),
        definition=_definition(row["definition"]),
        enabled=bool(row.get("enabled")),
        setup_required=bool(row.get("setup_required")),
        version=int(row.get("version") or 1),
        secret_names=secret_names or [],
        agent_ids=agent_ids or [],
        last_test_status=row.get("last_test_status"),
        last_tested_at=row.get("last_tested_at"),
        last_called_at=stats.get("last_called_at"),
        last_error=stats.get("last_error"),
        calls_window=int(stats.get("calls") or 0),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


_WINDOWED_CALLS = text(
    """
    SELECT action_id,
           count(*) AS calls,
           count(*) FILTER (WHERE status <> 'ok') AS errors,
           max(ts) AS last_called_at
      FROM action_calls
     WHERE action_id IS NOT NULL
       AND ts >= now() - make_interval(days => :days)
     GROUP BY action_id
    """
)

_SECRET_COUNTS = text(
    "SELECT action_id, count(*) AS secrets FROM action_secrets GROUP BY action_id"
)


@router.get("/actions", response_model=list[ActionListItem])
async def list_actions(
    tenant: TenantDep, container: ContainerDep
) -> list[ActionListItem]:
    """Installed actions and the rest of the catalogue, in one request.

    Windowed call counts come from `action_calls` in a single grouped query rather
    than one metrics read per action: the enterprise ceiling is 300 actions, and
    300 round trips to paint one screen is the kind of thing that only shows up in
    production.
    """
    rows: list[dict[str, Any]] = await container.actions_repo.list_actions(
        tenant.tenant_id
    )

    async with tenant_scope(tenant.tenant_id) as session:
        metric_rows = (
            await session.execute(_WINDOWED_CALLS, {"days": LIST_WINDOW_DAYS})
        ).mappings().all()
        secret_rows = (await session.execute(_SECRET_COUNTS)).mappings().all()

    metrics = {row["action_id"]: row for row in metric_rows}
    secrets = {row["action_id"]: int(row["secrets"] or 0) for row in secret_rows}

    items: list[ActionListItem] = []
    installed_keys: set[str] = set()
    installed_names: set[str] = set()

    for row in rows:
        key = row.get("library_key")
        if key:
            installed_keys.add(str(key))
        installed_names.add(str(row["name"]))
        stats = metrics.get(row["id"])
        entry = library.entry(str(key)) if key else None
        items.append(
            ActionListItem(
                id=row["id"],
                library_key=key,
                name=row["name"],
                display_name=row["display_name"],
                description=row["description"],
                kind=str(row["kind"]),
                effect=str(row["effect"]),
                installed=True,
                enabled=bool(row.get("enabled")),
                setup_required=bool(row.get("setup_required")),
                only_in_procedures=bool(row.get("only_in_procedures")),
                requires_confirmation=bool(row.get("requires_confirmation")),
                requires_verified_identity=bool(row.get("requires_verified_identity")),
                channels=_channels(row.get("channels")),
                system_handler=row.get("system_handler"),
                target_host=row.get("target_host"),
                has_secret=bool(row.get("secret_ref")) or secrets.get(row["id"], 0) > 0,
                requires=entry.requires if entry else "",
                version=int(row.get("version") or 1),
                last_test_status=row.get("last_test_status"),
                last_tested_at=row.get("last_tested_at"),
                calls_window=int(stats["calls"]) if stats else 0,
                errors_window=int(stats["errors"]) if stats else 0,
                last_called_at=stats["last_called_at"] if stats else None,
            )
        )

    for entry in library.LIBRARY:
        if entry.key in installed_keys or entry.definition.name in installed_names:
            continue
        definition = entry.definition
        items.append(
            ActionListItem(
                library_key=entry.key,
                name=definition.name,
                display_name=definition.display_name,
                description=definition.description,
                kind=str(definition.kind),
                effect=str(definition.effect),
                installed=False,
                enabled=False,
                setup_required=entry.setup_required,
                only_in_procedures=definition.only_in_procedures,
                requires_confirmation=definition.requires_confirmation,
                requires_verified_identity=definition.requires_verified_identity,
                channels=[str(channel) for channel in definition.channels],
                system_handler=definition.system_handler,
                target_host=definition.http.host if definition.http else None,
                requires=entry.requires,
                version=definition.version,
            )
        )

    return items


@router.post(
    "/actions",
    response_model=ActionDetail,
    status_code=201,
    dependencies=[require_role("owner", "admin")],
)
async def create_action(
    payload: ActionCreate, tenant: TenantDep, container: ContainerDep
) -> ActionDetail:
    """Author a new action.

    The whole §4 security envelope is `ActionDefinition`'s validators — the write
    gate, the template namespaces, the denied headers, the kind coherence rules —
    so validation is one `model_validate` and there is no second, weaker copy of
    those rules on this path.
    """
    definition = _definition(payload.definition)
    egress = _owes_transcript_audit(definition)

    existing = await container.actions_repo.get_by_name(
        tenant.tenant_id, definition.name
    )
    if existing is not None:
        raise ConflictError(f"an action named {definition.name!r} already exists")

    row: dict[str, Any] = await container.actions_repo.create(
        tenant.tenant_id,
        definition,
        enabled=payload.enabled,
        created_by=tenant.user_id,
    )
    if not row:
        raise ConflictError(f"an action named {definition.name!r} already exists")

    async with tenant_scope(tenant.tenant_id) as session:
        await _record(
            session,
            tenant,
            AUDIT_ACTION_CREATED,
            target_id=str(row["id"]),
            detail={
                "name": definition.name,
                "kind": str(definition.kind),
                "effect": str(definition.effect),
                "enabled": payload.enabled,
            },
        )
        if egress:
            await _record(
                session,
                tenant,
                AUDIT_ACTION_TRANSCRIPT_EGRESS,
                target_id=str(row["id"]),
                detail={"name": definition.name, "enabled": True},
            )
    return _detail(row)


@router.post(
    "/actions/from-library",
    response_model=ActionDetail,
    status_code=201,
    dependencies=[require_role("owner", "admin")],
)
async def install_from_library(
    payload: ActionFromLibrary, tenant: TenantDep, container: ContainerDep
) -> ActionDetail:
    """Install a catalogue entry.

    The install policy travels with the entry rather than being decided here: a
    prebuilt handler installs enabled, and a scaffold installs disabled with
    `setup_required` set, so the card can badge "Finish setup" deterministically
    instead of inferring it from the URL.
    """
    entry = library.entry(payload.library_key)
    if entry is None:
        raise ValidationError(f"no library action named {payload.library_key!r}")

    definition = entry.definition
    existing = await container.actions_repo.get_by_name(
        tenant.tenant_id, definition.name
    )
    if existing is not None:
        raise ConflictError(f"{payload.library_key!r} is already installed")

    row: dict[str, Any] = await container.actions_repo.create(
        tenant.tenant_id,
        definition,
        enabled=entry.enabled_by_default,
        library_key=entry.key,
        setup_required=entry.setup_required,
        created_by=tenant.user_id,
    )
    if not row:
        raise ConflictError(f"{payload.library_key!r} is already installed")

    await _audit(
        tenant,
        AUDIT_ACTION_CREATED,
        target_id=str(row["id"]),
        detail={
            "name": definition.name,
            "library_key": entry.key,
            "enabled": entry.enabled_by_default,
            "setup_required": entry.setup_required,
        },
    )
    return _detail(row)


@router.get("/actions/{action_id}", response_model=ActionDetail)
async def get_action(
    action_id: UUID, tenant: TenantDep, container: ContainerDep
) -> ActionDetail:
    row: dict[str, Any] | None = await container.actions_repo.get(
        tenant.tenant_id, action_id
    )
    if row is None:
        raise NotFoundError("no such action")
    metrics: dict[str, Any] = await container.actions_repo.call_metrics(
        tenant.tenant_id, action_id, days=LIST_WINDOW_DAYS
    )
    names: list[str] = await container.actions_repo.secret_names(
        tenant.tenant_id, action_id
    )
    agent_ids: list[UUID] = await container.actions_repo.agent_ids_for_action(
        tenant.tenant_id, action_id
    )
    return _detail(row, secret_names=names, agent_ids=agent_ids, metrics=metrics)


@router.patch(
    "/actions/{action_id}",
    response_model=ActionDetail,
    dependencies=[require_role("owner", "admin")],
)
async def update_action(
    action_id: UUID,
    payload: ActionUpdate,
    tenant: TenantDep,
    container: ContainerDep,
) -> ActionDetail:
    """Edit an action.

    `definition` is merged key-by-key over the stored one and then re-validated as
    a whole, because a definition is a security envelope and half of one is not a
    valid state. `version` is bumped inside the merged definition rather than in
    SQL, so the stored JSONB and the promoted column can never disagree about
    which version this is.
    """
    row: dict[str, Any] | None = await container.actions_repo.get(
        tenant.tenant_id, action_id
    )
    if row is None:
        raise NotFoundError("no such action")

    stored = _definition(row["definition"])
    definition = stored
    if payload.definition is not None:
        merged = {**stored.model_dump(mode="json"), **payload.definition}
        merged["version"] = int(row.get("version") or 1) + 1
        definition = _definition(merged)

    egress = _owes_transcript_audit(definition)

    if definition.name != stored.name:
        clash = await container.actions_repo.get_by_name(
            tenant.tenant_id, definition.name
        )
        if clash is not None and clash.get("id") != action_id:
            raise ConflictError(f"an action named {definition.name!r} already exists")

    updated: dict[str, Any] | None = await container.actions_repo.update(
        tenant.tenant_id,
        action_id,
        definition,
        setup_required=payload.setup_required,
    )
    if updated is None:
        raise NotFoundError("no such action")

    was_enabled = bool(row.get("enabled"))
    toggled = payload.enabled is not None and payload.enabled != was_enabled
    if toggled and payload.enabled is not None:
        await container.actions_repo.set_enabled(
            tenant.tenant_id, action_id, payload.enabled
        )
        updated["enabled"] = payload.enabled

    async with tenant_scope(tenant.tenant_id) as session:
        await _record(
            session,
            tenant,
            AUDIT_ACTION_UPDATED,
            target_id=str(action_id),
            detail={"name": definition.name, "version": definition.version},
        )
        if toggled:
            await _record(
                session,
                tenant,
                AUDIT_ACTION_ENABLED if payload.enabled else AUDIT_ACTION_DISABLED,
                target_id=str(action_id),
                detail={"name": definition.name},
            )
        if egress and not stored.include_transcript:
            await _record(
                session,
                tenant,
                AUDIT_ACTION_TRANSCRIPT_EGRESS,
                target_id=str(action_id),
                detail={"name": definition.name, "enabled": True},
            )
    return _detail(updated)


@router.delete(
    "/actions/{action_id}",
    status_code=204,
    dependencies=[require_role("owner", "admin")],
)
async def delete_action(
    action_id: UUID, tenant: TenantDep, container: ContainerDep
) -> None:
    """Delete an action, unless a procedure still needs it.

    The conflict lists the procedures by name: "in use" without saying by what
    leaves an admin clicking through every procedure to find the one reference.
    """
    row: dict[str, Any] | None = await container.actions_repo.get(
        tenant.tenant_id, action_id
    )
    if row is None:
        raise NotFoundError("no such action")

    async with tenant_scope(tenant.tenant_id) as session:
        blockers = await _procedures_using(session, action_id)
        if blockers:
            names = ", ".join(item["name"] for item in blockers)
            raise ConflictError(f"in use by procedure(s): {names}")
        await _record(
            session,
            tenant,
            AUDIT_ACTION_DELETED,
            target_id=str(action_id),
            detail={"name": row["name"], "library_key": row.get("library_key")},
        )

    deleted: bool = await container.actions_repo.delete(tenant.tenant_id, action_id)
    if not deleted:
        raise NotFoundError("no such action")


class TestRequest(BaseModel):
    """Arguments for one trial call. The tester supplies what the model would."""

    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(default_factory=dict)
    # Optional, and the only way a test run gets a verified identity. An action
    # that interpolates `{{contact.*}}` is untestable without a real contacts row,
    # and inventing one would make the test pass on something runtime refuses.
    contact_id: UUID | None = None


def _echo_request(definition: ActionDefinition) -> dict[str, Any]:
    """The request as authored, never as rendered.

    Echoing the template instead of the wire request is what makes the redaction
    total rather than best-effort: an unresolved `{{secret.KEY}}` cannot leak a
    credential, and no code path here is ever handed the decrypted one. Header
    values are dropped entirely, because a static header value a tenant typed in
    is as likely to be a credential as a referenced secret is.
    """
    http = definition.http
    if http is None:
        return {"kind": str(definition.kind)}
    return {
        "kind": str(definition.kind),
        "method": http.method,
        "url": http.url,
        "query": sorted(http.query),
        "headers": dict.fromkeys(http.headers, "[redacted]"),
        "body_fields": sorted(http.body) if http.body else [],
        "timeout_seconds": http.timeout_seconds,
    }


@router.post(
    "/actions/{action_id}/test",
    response_model=TestResult,
    dependencies=[require_role("owner", "admin")],
)
async def test_action(
    action_id: UUID,
    payload: TestRequest,
    tenant: TenantDep,
    container: ContainerDep,
) -> TestResult:
    """Run the action once, through the same dispatch path a turn uses.

    Deliberately the service and not the executor: a test that took a shortcut
    around the envelope could pass on something runtime would refuse, which is the
    one thing a test must never do. `confirmed` and `in_procedure` are set so a
    gated write is testable at all — the visitor consent those flags stand in for
    is the tenant admin pressing the button. Identity is the exception: consent can
    be stood in for, a contacts row cannot, so an identity-gated action is testable
    only by naming a real `contact_id`.
    """
    row: dict[str, Any] | None = await container.actions_repo.get(
        tenant.tenant_id, action_id
    )
    if row is None:
        raise NotFoundError("no such action")

    await container.rate_limiter.check(
        f"{RL_SCOPE_ACTION_ONE}:test",
        f"{tenant.tenant_id}:{action_id}",
        TEST_CALLS_PER_MINUTE,
        60,
    )

    definition = _definition(row["definition"])
    # §4.6 is a security control, not a UX nicety: `identity_verified` and
    # `contact_id` travel together or not at all. A test runner that could set the
    # flag with a null contact would manufacture a caller state no real visitor can
    # reach — verified, yet resolving `{{contact.*}}` against nothing — and the
    # refusal path this endpoint exists to exercise would never fire. So identity
    # is verified only when the tester named a contact row that really exists in
    # this tenant, and is false otherwise.
    contact_id: UUID | None = payload.contact_id
    if contact_id is not None:
        contact = await container.contacts_repo.get(tenant.tenant_id, contact_id)
        if contact is None:
            raise NotFoundError("no such contact")
    ctx = ActionContext(
        tenant_id=tenant.tenant_id,
        agent_id=tenant.agent_id,
        channel=Channel.API,
        identity_verified=contact_id is not None,
        contact_id=contact_id,
        confirmed=True,
        in_procedure=True,
    )

    outcome = await container.action_service.dispatch(
        action=definition, arguments=payload.arguments, ctx=ctx
    )
    status = "ok" if outcome.ok else str(outcome.outcome)
    await container.actions_repo.record_test(
        tenant.tenant_id, action_id, status=status
    )
    await _audit(
        tenant,
        AUDIT_ACTION_TESTED,
        target_id=str(action_id),
        detail={"name": definition.name, "outcome": str(outcome.outcome)},
    )
    return TestResult(
        ok=outcome.ok,
        outcome=str(outcome.outcome),
        http_status=outcome.http_status,
        duration_ms=outcome.duration_ms,
        error=outcome.error,
        sample=outcome.data,
        render=outcome.render,
        request=_echo_request(definition),
    )


_SECRET_REF_FOR_NAME = text(
    "SELECT secret_ref FROM action_secrets "
    "WHERE action_id = :action_id AND name = :name"
)

_STORE_CIPHERTEXT = text(
    """
    INSERT INTO tenant_secrets (tenant_id, ref, provider, ciphertext)
    VALUES (:tenant_id, :ref, 'action', :ciphertext)
    """
)

_DROP_CIPHERTEXT = text(
    "DELETE FROM tenant_secrets "
    "WHERE tenant_id = :tenant_id AND ref = :ref AND provider = 'action'"
)


@router.put(
    "/actions/{action_id}/secret",
    response_model=SecretRef,
    dependencies=[require_role("owner", "admin")],
)
async def put_action_secret(
    action_id: UUID,
    payload: SecretPut,
    tenant: TenantDep,
    container: ContainerDep,
) -> SecretRef:
    """Store a named credential and return its reference only.

    The write is ordered so every intermediate state is safe: the new ciphertext
    lands first (unreferenced, therefore inert), then the reference flips, then the
    superseded ciphertext is dropped. A failure between any two steps leaves an
    orphan row, never an action pointing at a credential that is not there.
    """
    row: dict[str, Any] | None = await container.actions_repo.get(
        tenant.tenant_id, action_id
    )
    if row is None:
        raise NotFoundError("no such action")

    name = _secret_name(payload.name)
    ref = f"action:{uuid4().hex}"
    ciphertext = encrypt_secret(payload.value, container.settings.ENCRYPTION_KEY)

    async with tenant_scope(tenant.tenant_id) as session:
        previous = (
            await session.execute(
                _SECRET_REF_FOR_NAME, {"action_id": action_id, "name": name}
            )
        ).mappings().first()
        old_ref = previous["secret_ref"] if previous else None
        await session.execute(
            _STORE_CIPHERTEXT,
            {"tenant_id": tenant.tenant_id, "ref": ref, "ciphertext": ciphertext},
        )

    await container.actions_repo.set_secret_ref(
        tenant.tenant_id, action_id, name, ref
    )

    async with tenant_scope(tenant.tenant_id) as session:
        if old_ref and old_ref != ref:
            await session.execute(
                _DROP_CIPHERTEXT, {"tenant_id": tenant.tenant_id, "ref": old_ref}
            )
        await _record(
            session,
            tenant,
            AUDIT_ACTION_SECRET_ROTATED if old_ref else AUDIT_ACTION_SECRET_SET,
            target_id=str(action_id),
            detail={"name": name, "ref": ref},
        )
    return SecretRef(name=name, ref=ref, rotated=old_ref is not None)


@router.delete(
    "/actions/{action_id}/secret",
    status_code=204,
    dependencies=[require_role("owner", "admin")],
)
async def delete_action_secret(
    action_id: UUID,
    tenant: TenantDep,
    container: ContainerDep,
    name: str = Query(min_length=1, max_length=40),
) -> None:
    """Forget a named credential: the reference and the ciphertext both."""
    async with tenant_scope(tenant.tenant_id) as session:
        previous = (
            await session.execute(
                _SECRET_REF_FOR_NAME, {"action_id": action_id, "name": name}
            )
        ).mappings().first()
    if previous is None:
        raise NotFoundError("no such action secret")

    removed: bool = await container.actions_repo.delete_secret_ref(
        tenant.tenant_id, action_id, name
    )
    if not removed:
        raise NotFoundError("no such action secret")

    async with tenant_scope(tenant.tenant_id) as session:
        await session.execute(
            _DROP_CIPHERTEXT,
            {"tenant_id": tenant.tenant_id, "ref": previous["secret_ref"]},
        )
        await _record(
            session,
            tenant,
            AUDIT_ACTION_SECRET_DELETED,
            target_id=str(action_id),
            detail={"name": name},
        )


_DAILY_CALLS = text(
    """
    SELECT date_trunc('day', ts)::date AS day,
           count(*) AS calls,
           count(*) FILTER (WHERE status <> 'ok') AS errors,
           percentile_disc(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50_ms,
           percentile_disc(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms
      FROM action_calls
     WHERE action_id = :action_id
       AND ts >= now() - make_interval(days => :days)
     GROUP BY 1
     ORDER BY 1
    """
)

_WINDOW_P50 = text(
    """
    SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50_ms
      FROM action_calls
     WHERE action_id = :action_id
       AND ts >= now() - make_interval(days => :days)
    """
)


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


@router.get("/actions/{action_id}/activity", response_model=ActivityReport)
async def action_activity(
    action_id: UUID,
    tenant: TenantDep,
    container: ContainerDep,
    days: int = Query(default=7, ge=1, le=90),
) -> ActivityReport:
    """Call count, error rate, latency percentiles and the daily breakdown.

    The headline numbers come from the repository's own metrics query so this
    endpoint and the Actions card can never disagree; only p50 and the daily series
    are read here, because nothing else needs them.
    """
    metrics: dict[str, Any] = await container.actions_repo.call_metrics(
        tenant.tenant_id, action_id, days=days
    )
    params = {"action_id": action_id, "days": days}
    async with tenant_scope(tenant.tenant_id) as session:
        daily_rows = (await session.execute(_DAILY_CALLS, params)).mappings().all()
        p50 = (await session.execute(_WINDOW_P50, params)).scalar_one_or_none()

    calls = int(metrics.get("calls") or 0)
    ok = int(metrics.get("ok") or 0)
    recent: list[dict[str, Any]] = await container.actions_repo.recent_calls(
        tenant.tenant_id, action_id=action_id, limit=20
    )
    return ActivityReport(
        days=days,
        calls=calls,
        errors=calls - ok,
        error_rate=round((calls - ok) / calls, 4) if calls else 0.0,
        p50_ms=_int_or_none(p50),
        p95_ms=_int_or_none(metrics.get("p95_ms")),
        last_called_at=metrics.get("last_called_at"),
        last_error=metrics.get("last_error"),
        daily=[
            ActivityDay(
                day=row["day"],
                calls=int(row["calls"]),
                errors=int(row["errors"]),
                p50_ms=_int_or_none(row["p50_ms"]),
                p95_ms=_int_or_none(row["p95_ms"]),
            )
            for row in daily_rows
        ],
        recent=[dict(item) for item in recent],
    )


# ------------------------------------------------------------- agent links

_AGENT_ACTIONS = text(
    """
    SELECT a.id, a.name, a.display_name, a.description, a.kind, a.effect,
           a.enabled, a.setup_required, a.only_in_procedures,
           a.requires_confirmation, a.requires_verified_identity, a.channels,
           a.system_handler, a.target_host, a.library_key, a.secret_ref,
           a.version, a.last_test_status, a.last_tested_at
      FROM actions a
      JOIN agent_actions link ON link.action_id = a.id
     WHERE link.agent_id = :agent_id
     ORDER BY a.display_name
    """
)

_LINK_EXISTS = text(
    "SELECT 1 FROM agent_actions "
    "WHERE agent_id = :agent_id AND action_id = :action_id"
)


@router.get("/agents/{agent_id}/actions", response_model=list[ActionListItem])
async def list_agent_actions(
    agent_id: UUID, tenant: TenantDep
) -> list[ActionListItem]:
    async with tenant_scope(tenant.tenant_id) as session:
        if not await _agent_exists(session, agent_id):
            raise NotFoundError("no such agent")
        rows = (
            await session.execute(_AGENT_ACTIONS, {"agent_id": agent_id})
        ).mappings().all()
    return [
        ActionListItem(
            id=row["id"],
            library_key=row["library_key"],
            name=row["name"],
            display_name=row["display_name"],
            description=row["description"],
            kind=str(row["kind"]),
            effect=str(row["effect"]),
            installed=True,
            enabled=bool(row["enabled"]),
            setup_required=bool(row["setup_required"]),
            only_in_procedures=bool(row["only_in_procedures"]),
            requires_confirmation=bool(row["requires_confirmation"]),
            requires_verified_identity=bool(row["requires_verified_identity"]),
            channels=_channels(row["channels"]),
            system_handler=row["system_handler"],
            target_host=row["target_host"],
            has_secret=bool(row["secret_ref"]),
            version=int(row["version"] or 1),
            last_test_status=row["last_test_status"],
            last_tested_at=row["last_tested_at"],
        )
        for row in rows
    ]


@router.post(
    "/agents/{agent_id}/actions/{action_id}",
    status_code=204,
    dependencies=[require_role("owner", "admin")],
)
async def link_agent_action(
    agent_id: UUID, action_id: UUID, tenant: TenantDep, container: ContainerDep
) -> None:
    action: dict[str, Any] | None = await container.actions_repo.get(
        tenant.tenant_id, action_id
    )
    if action is None:
        raise NotFoundError("no such action")

    async with tenant_scope(tenant.tenant_id) as session:
        if not await _agent_exists(session, agent_id):
            raise NotFoundError("no such agent")
        existing = (
            await session.execute(
                _LINK_EXISTS, {"agent_id": agent_id, "action_id": action_id}
            )
        ).first()
        if existing is not None:
            raise ConflictError("that action is already linked to this agent")

    await container.actions_repo.link_agent(tenant.tenant_id, agent_id, action_id)
    await _audit(
        tenant,
        AUDIT_ACTION_LINKED_TO_AGENT,
        target_id=str(action_id),
        detail={"agent_id": str(agent_id), "name": action["name"]},
    )


@router.delete(
    "/agents/{agent_id}/actions/{action_id}",
    status_code=204,
    dependencies=[require_role("owner", "admin")],
)
async def unlink_agent_action(
    agent_id: UUID, action_id: UUID, tenant: TenantDep, container: ContainerDep
) -> None:
    """Unlink, unless a procedure on this agent still references the action.

    The check is scoped to this agent: the same action linked to a second agent
    whose procedures need it is not this agent's problem.
    """
    async with tenant_scope(tenant.tenant_id) as session:
        blockers = await _procedures_using(session, action_id, agent_id=agent_id)
        if blockers:
            names = ", ".join(item["name"] for item in blockers)
            raise ConflictError(f"in use by procedure(s) on this agent: {names}")

    removed: bool = await container.actions_repo.unlink_agent(
        tenant.tenant_id, agent_id, action_id
    )
    if not removed:
        raise NotFoundError("that action is not linked to this agent")

    await _audit(
        tenant,
        AUDIT_ACTION_UNLINKED_FROM_AGENT,
        target_id=str(action_id),
        detail={"agent_id": str(agent_id)},
    )


@router.put(
    "/agents/{agent_id}/actions",
    response_model=AgentActionLinks,
    dependencies=[require_role("owner", "admin")],
)
async def replace_agent_actions(
    agent_id: UUID,
    payload: AgentActionLinks,
    tenant: TenantDep,
    container: ContainerDep,
) -> AgentActionLinks:
    """Bulk replace, for the create-agent wizard, which sends the whole selection.

    Every id is checked against the tenant's own actions before anything is
    written: RLS would silently drop a foreign id, and an agent quietly missing a
    tool it was told it had is worse than a rejected request.
    """
    wanted = list(dict.fromkeys(payload.action_ids))
    owned: list[dict[str, Any]] = await container.actions_repo.list_actions(
        tenant.tenant_id
    )
    known = {row["id"] for row in owned}
    unknown = [str(item) for item in wanted if item not in known]
    if unknown:
        raise ValidationError(f"unknown action id(s): {', '.join(unknown)}")

    async with tenant_scope(tenant.tenant_id) as session:
        if not await _agent_exists(session, agent_id):
            raise NotFoundError("no such agent")

    await container.actions_repo.set_agent_links(tenant.tenant_id, agent_id, wanted)
    await _audit(
        tenant,
        AUDIT_ACTION_LINKED_TO_AGENT,
        target_id=str(agent_id),
        target_type="agent",
        detail={"action_ids": [str(item) for item in wanted], "bulk": True},
    )
    return AgentActionLinks(action_ids=wanted)
