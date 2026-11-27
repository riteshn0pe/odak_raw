"""Audit logging.

One writer, called by every privileged action, so what counts as auditable is
decided in one place instead of scattered across routes. The write takes an
optional session: when the caller passes its own transaction, the audit row
commits or rolls back with the action it records, which is the point — an audit
trail you can forget to write is worthless.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import admin_scope
from app.core.logging import get_logger

logger = get_logger(__name__)


_INSERT = text(
    """
    INSERT INTO audit_log (
        actor_type, actor_id, actor_email, action,
        target_tenant, target_type, target_id, detail
    ) VALUES (
        :actor_type, :actor_id, :actor_email, :action,
        :target_tenant, :target_type, :target_id, CAST(:detail AS jsonb)
    )
    """
)


async def record(
    action: str,
    *,
    actor_type: str = "staff",
    actor_id: UUID | None = None,
    actor_email: str | None = None,
    target_tenant: UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Write one audit row.

    Pass `session` to bind the audit write to the caller's transaction — the row
    then shares the action's atomicity. Without it, a standalone admin_scope
    transaction is opened, which is correct for reads-that-log but leaves a race
    where the action commits and the log does not; prefer passing the session for
    any mutation.
    """
    params = {
        "actor_type": actor_type,
        "actor_id": actor_id,
        "actor_email": actor_email,
        "action": action,
        "target_tenant": target_tenant,
        "target_type": target_type,
        "target_id": target_id,
        "detail": json.dumps(detail or {}),
    }

    if session is not None:
        await session.execute(_INSERT, params)
        return

    async with admin_scope() as own:
        await own.execute(_INSERT, params)


async def query(
    *,
    target_tenant: UUID | None = None,
    actor_id: UUID | None = None,
    action: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Read the trail for the console. Filters are optional and compose."""
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if target_tenant is not None:
        clauses.append("target_tenant = :target_tenant")
        params["target_tenant"] = target_tenant
    if actor_id is not None:
        clauses.append("actor_id = :actor_id")
        params["actor_id"] = actor_id
    if action is not None:
        clauses.append("action = :action")
        params["action"] = action

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with admin_scope() as session:
        rows = (
            await session.execute(
                text(
                    f"SELECT id, ts, actor_type, actor_id, actor_email, action, "
                    f"target_tenant, target_type, target_id, detail FROM audit_log "
                    f"{where} ORDER BY ts DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            )
        ).mappings().all()
    return [dict(row) for row in rows]


# --------------------------------------------------------------- event names
#
# The `action.*` names live in `app/core/constants.py` because three layers have
# to agree on them — the dispatch loop, the endpoints and the executor all write
# them. The names below are written from one layer only, the endpoints, so they
# live with the writer instead of in the shared vocabulary.
#
# Two omissions in this list are deliberate. **`procedure.triggered` is not
# audited**: it is per-conversation volume and `procedure_runs` already records
# it, so auditing it would bury every other event in the trail.
# **`identity.verification_failed` is sampled** rather than written once per
# rejection, because a tenant with broken token minting would otherwise write
# thousands of identical rows an hour; the per-verifier `reject_count` carries the
# real signal.

AUDIT_PROCEDURE_CREATED = "procedure.created"
AUDIT_PROCEDURE_UPDATED = "procedure.updated"
AUDIT_PROCEDURE_DELETED = "procedure.deleted"
AUDIT_PROCEDURE_ENABLED = "procedure.enabled"
AUDIT_PROCEDURE_DISABLED = "procedure.disabled"
AUDIT_PROCEDURE_DUPLICATED = "procedure.duplicated"
AUDIT_PROCEDURE_INSTALLED_FROM_TEMPLATE = "procedure.installed_from_template"
AUDIT_PROCEDURE_RUN_FORCE_CLOSED = "procedure_run.force_closed"

AUDIT_IDENTITY_VERIFIER_CREATED = "identity.verifier_created"
AUDIT_IDENTITY_VERIFIER_UPDATED = "identity.verifier_updated"
AUDIT_IDENTITY_VERIFIER_DELETED = "identity.verifier_deleted"
AUDIT_IDENTITY_VERIFICATION_FAILED = "identity.verification_failed"

AUDIT_CONTACT_CREATED = "contact.created"
AUDIT_CONTACT_UPDATED = "contact.updated"
AUDIT_CONTACT_DELETED = "contact.deleted"
AUDIT_CONTACT_EXPORTED = "contact.exported"
AUDIT_CONTACT_MERGE_DECLINED = "contact.merge_declined"

# Escalation. `escalation.suppressed` is written only for the reasons a tenant
# can act on (no destination, rate limited); `cooldown` is the feature working as
# designed and writing a row per suppressed duplicate would bury the trail.
AUDIT_ESCALATION_CREATED = "escalation.created"
AUDIT_ESCALATION_NOTIFIED = "escalation.notified"
AUDIT_ESCALATION_NOTIFY_PARTIAL = "escalation.notify_partial"
AUDIT_ESCALATION_FAILED = "escalation.notify_failed"
AUDIT_ESCALATION_SUPPRESSED = "escalation.suppressed"
AUDIT_ESCALATION_CONTACT_ADDED = "escalation.contact_added"
AUDIT_ESCALATION_SLACK_BLOCKED_SSRF = "escalation.slack_blocked_ssrf"
AUDIT_ESCALATION_SETTINGS_UPDATED = "escalation.settings_updated"
AUDIT_ESCALATION_TEST_SENT = "escalation.test_sent"

# Email channel. `email.auto_replied` is the compliance-critical row: it is the
# only immutable record of what was sent on the tenant's behalf, so it carries
# the SHA-256 of the exact body along with the model and the confidence.
AUDIT_EMAIL_CHANNEL_CREATED = "email.channel_created"
AUDIT_EMAIL_CHANNEL_UPDATED = "email.channel_updated"
AUDIT_EMAIL_CHANNEL_DISCONNECTED = "email.channel_disconnected"
AUDIT_EMAIL_CHANNEL_SUSPENDED = "email.channel_suspended"
AUDIT_EMAIL_ADDRESS_ROTATED = "email.address_rotated"
AUDIT_EMAIL_ADDRESS_REVEALED = "email.address_revealed"
AUDIT_EMAIL_DOMAIN_VERIFIED = "email.domain_verified"
AUDIT_EMAIL_RECEIVED = "email.received"
AUDIT_EMAIL_IGNORED = "email.ignored"
AUDIT_EMAIL_AUTO_REPLIED = "email.auto_replied"
AUDIT_EMAIL_DRAFTED = "email.drafted"
AUDIT_EMAIL_BOUNCED = "email.bounced"
AUDIT_EMAIL_COMPLAINED = "email.complained"
AUDIT_EMAIL_ATTACHMENT_INGESTED = "email.attachment_ingested"
AUDIT_EMAIL_ATTACHMENT_REJECTED = "email.attachment_rejected"

PROCEDURE_AUDIT_EVENTS = frozenset(
    {
        AUDIT_PROCEDURE_CREATED,
        AUDIT_PROCEDURE_UPDATED,
        AUDIT_PROCEDURE_DELETED,
        AUDIT_PROCEDURE_ENABLED,
        AUDIT_PROCEDURE_DISABLED,
        AUDIT_PROCEDURE_DUPLICATED,
        AUDIT_PROCEDURE_INSTALLED_FROM_TEMPLATE,
        AUDIT_PROCEDURE_RUN_FORCE_CLOSED,
    }
)

IDENTITY_AUDIT_EVENTS = frozenset(
    {
        AUDIT_IDENTITY_VERIFIER_CREATED,
        AUDIT_IDENTITY_VERIFIER_UPDATED,
        AUDIT_IDENTITY_VERIFIER_DELETED,
        AUDIT_IDENTITY_VERIFICATION_FAILED,
    }
)

CONTACT_AUDIT_EVENTS = frozenset(
    {
        AUDIT_CONTACT_CREATED,
        AUDIT_CONTACT_UPDATED,
        AUDIT_CONTACT_DELETED,
        AUDIT_CONTACT_EXPORTED,
        AUDIT_CONTACT_MERGE_DECLINED,
    }
)

# Platform settings console (admin-control). Values are redacted for secrets;
# only the before/after source is recorded in the audit detail.
AUDIT_SETTINGS_UPDATED = "settings.updated"
AUDIT_SETTINGS_RESET = "settings.reset"
# A provider key shown in clear on an operator's screen is the one settings event
# with no DB change behind it, so it is the one that has to be recorded here or it
# leaves no trace at all.
AUDIT_SETTINGS_REVEALED = "settings.revealed"
AUDIT_SETTINGS_LOCKED = "settings.locked"
