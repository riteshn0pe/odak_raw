"""The per-turn half of the procedures engine.

`app/services/procedures/service.py` owns authoring — validation, publish,
enable. This module owns what happens while a visitor is talking: which triggers
are offered, which step the model is on, what it is allowed to call, and what is
written down afterwards.

Three things shape the design.

**There is no checkpointer** (§1.3). A run is not a suspended computation, it is
durable conversation state that has to survive process death on a scale-to-zero
machine and stay readable by a support lead in the dashboard. So every turn
starts with one indexed read of the active run by `conversation_id`, and ends
with one write of the pointer, the variables and the decisions. Nothing is held
in memory between turns.

**The reserved tools ride the existing action loop.** `run_tool_loop` already
owns provider dialects, budgets, injection screening and the wire format, and
none of that should be duplicated. `procedure_dispatcher()` returns an object
that satisfies the `ActionDispatcher` protocol, answers the three reserved names
itself, and delegates everything else to the real dispatcher. The loop needs no
knowledge of procedures at all.

**Bookkeeping must not starve the work** (§1.2). `advance_procedure` makes no
external call, so charging it against `ACTIONS_MAX_ITERATIONS` would let the
model spend its whole turn narrating. The loop counts iterations, not tool
kinds, so `widen_budget()` grants the turn `PROCEDURE_MAX_BOOKKEEPING_CALLS`
extra iterations and calls when a run is live, and the runtime caps the
bookkeeping calls separately at exactly that number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text

from app.core.constants import Channel, UsageKind
from app.core.db import tenant_scope
from app.core.logging import get_logger
from app.domain.actions import (
    ActionAuth,
    ActionContext,
    ActionDefinition,
    ActionEffect,
    ActionKind,
    ActionOutcome,
    ActionOutcomeCode,
)
from app.domain.escalation import EscalationTrigger
from app.domain.procedures import ProcedureRunState, flatten_steps
from app.services.actions.service import ToolSet

logger = get_logger(__name__)

# ------------------------------------------------------------------ vocabulary

TOOL_START = "start_procedure"
TOOL_ADVANCE = "advance_procedure"
TOOL_END = "end_procedure"

RESERVED_PROCEDURE_TOOLS = frozenset({TOOL_START, TOOL_ADVANCE, TOOL_END})

# `procedure_runs.status`
STATUS_ACTIVE = "active"
STATUS_AWAITING_USER = "awaiting_user"
STATUS_RESOLVED = "resolved"
STATUS_HANDED_OFF = "handed_off"
STATUS_ABANDONED = "abandoned"
STATUS_FAILED = "failed"

# `procedure_runs.outcome`
OUTCOME_RESOLVED = "resolved"
OUTCOME_HANDED_OFF = "handed_off"
OUTCOME_ABANDONED = "abandoned_by_user"
OUTCOME_NOT_APPLICABLE = "not_applicable"

# `procedure_runs.ended_reason`, mirroring the CHECK in
# migrations/010_procedures_contacts.sql:125-131
REASON_COMPLETED = "completed"
REASON_ESCALATED = "escalated"
REASON_USER_ABANDONED = "user_abandoned"
REASON_TTL_EXPIRED = "ttl_expired"
REASON_TURN_BUDGET = "turn_budget_exhausted"
REASON_ACTION_BUDGET = "action_budget_exhausted"
REASON_ACTION_UNAVAILABLE = "required_action_unavailable"
REASON_DEFINITION_DELETED = "definition_deleted"
REASON_INJECTION = "injection_flagged"

# `procedure_run_steps.outcome`
STEP_DONE = "done"
STEP_SKIPPED = "skipped_unavailable"
STEP_ASKED = "asked_instead"
STEP_BRANCH = "branch_taken"
STEP_ESCALATED = "escalated"
STEP_ABORTED = "aborted"

# Why the turn stopped, recorded on the trace rather than on the run: it is a
# property of the turn, not of the SOP.
STOPPED_ALREADY_ACTIVE = "procedure_already_active"
STOPPED_BOOKKEEPING_CAP = "procedure_bookkeeping_cap"

# Per-step degradation when a referenced action is not available on this channel
# (§1.5). The blanket "drop the whole procedure" rule is deliberately rejected:
# eight useful steps beat none.
ON_UNAVAILABLE_SKIP = "skip"
ON_UNAVAILABLE_ASK = "ask"
ON_UNAVAILABLE_ESCALATE = "escalate"
ON_UNAVAILABLE_ABORT = "abort"

_VARIABLE_HOLE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}")
_NOT_YET_KNOWN = "(not yet known)"

SOURCE_CUSTOMER = "customer"
SOURCE_SESSION = "session"


# --------------------------------------------------------------- reserved tools


def _reserved_definition(
    name: str, display_name: str, description: str
) -> ActionDefinition:
    """A synthetic definition for a reserved tool.

    `model_construct` rather than the constructor on purpose: the name validator
    refuses anything in `RESERVED_TOOL_NAMES`, which is exactly what these are.
    That guard protects tenant-authored rows from shadowing a built-in and must
    stay; this object is never persisted, never validated and never dispatched
    to a third party, so bypassing validation here is the narrow correct move.

    `effect=LOCAL` keeps the loop from batching these concurrently with reads and
    from counting them as writes, and `kind=SYSTEM` is what the evidence block
    labels them as.
    """
    return ActionDefinition.model_construct(
        name=name,
        display_name=display_name,
        description=description,
        kind=ActionKind.SYSTEM,
        effect=ActionEffect.LOCAL,
        parameters=[],
        http=None,
        # a reserved tool makes no outbound call, so there is nothing to
        # authenticate; the default `none` type says exactly that
        auth=ActionAuth(),
        render=None,
        system_handler=None,
        only_in_procedures=False,
        requires_confirmation=False,
        requires_verified_identity=False,
        channels=list(Channel),
        rate_limit_per_minute=600,
        include_transcript=False,
        version=1,
    )


START_DEFINITION = _reserved_definition(
    TOOL_START,
    "Start procedure",
    "Start one of the listed procedures. Call this the moment the visitor's "
    "message matches a procedure's 'when to use' description. Pass the exact id "
    "shown in the list and do not paraphrase or invent the steps.",
)

ADVANCE_DEFINITION = _reserved_definition(
    TOOL_ADVANCE,
    "Advance procedure",
    "Record that you have completed the current procedure step and get the next "
    "one. Supply branch_taken at a decision point, variables for anything you "
    "have learned, and a note explaining any judgement you made.",
)

END_DEFINITION = _reserved_definition(
    TOOL_END,
    "End procedure",
    "Close the active procedure. Use resolved when the visitor's problem is "
    "handled, handed_off when a person must take over, abandoned_by_user when "
    "they have moved on to something else, not_applicable when the procedure "
    "turned out not to fit.",
)

START_SPEC: dict[str, Any] = {
    "name": TOOL_START,
    "description": START_DEFINITION.description,
    "input_schema": {
        "type": "object",
        "properties": {
            "procedure_id": {
                "type": "string",
                "description": "The id of the procedure to start, exactly as listed.",
            }
        },
        "required": ["procedure_id"],
    },
}

ADVANCE_SPEC: dict[str, Any] = {
    "name": TOOL_ADVANCE,
    "description": ADVANCE_DEFINITION.description,
    "input_schema": {
        "type": "object",
        "properties": {
            "step_sid": {
                "type": "string",
                "description": "The id of the step you have just completed.",
            },
            "branch_taken": {
                "type": "string",
                "description": "At a decision point only: the label of the branch "
                "that applies. One of the labels shown with the step.",
            },
            "variables": {
                "type": "object",
                "description": "Anything you learned during this step, as flat "
                "name/value string pairs. Only what the visitor told you or an "
                "action returned — never a guess.",
                "additionalProperties": {"type": "string"},
            },
            "note": {
                "type": "string",
                "description": "One sentence. Required at a decision point: why "
                "this branch and not another.",
            },
        },
        "required": ["step_sid"],
    },
}

END_SPEC: dict[str, Any] = {
    "name": TOOL_END,
    "description": END_DEFINITION.description,
    "input_schema": {
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": [
                    OUTCOME_RESOLVED,
                    OUTCOME_HANDED_OFF,
                    OUTCOME_ABANDONED,
                    OUTCOME_NOT_APPLICABLE,
                ],
            },
            "note": {"type": "string", "description": "One sentence: why it ended."},
        },
        "required": ["outcome"],
    },
}

RESERVED_DEFINITIONS: dict[str, ActionDefinition] = {
    TOOL_START: START_DEFINITION,
    TOOL_ADVANCE: ADVANCE_DEFINITION,
    TOOL_END: END_DEFINITION,
}


# ------------------------------------------------------------------ guard DSL


def _coerce_number(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _coerce_date(raw: str) -> datetime | None:
    """ISO-8601, or `now-30d` / `now+5d` against the server clock.

    Dates are never taken from a value the model produced: `now` is our clock,
    which is the whole reason relative literals are in the grammar. Relative date
    arithmetic across a month boundary is the canonical model failure.
    """
    candidate = raw.strip().strip("'\"")
    relative = re.fullmatch(r"now\s*([+-])\s*(\d{1,5})([dhm])", candidate, re.IGNORECASE)
    if relative is not None:
        sign = -1 if relative.group(1) == "-" else 1
        amount = int(relative.group(2))
        unit = relative.group(3).lower()
        delta = {
            "d": timedelta(days=amount),
            "h": timedelta(hours=amount),
            "m": timedelta(minutes=amount),
        }[unit]
        return datetime.now(UTC) + (delta * sign)
    if candidate.lower() == "now":
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


_TERM = re.compile(
    r"^\s*(?:"
    r"(?P<unary>exists|missing)\s+\{\{\s*(?P<uref>[a-zA-Z_][\w.]*)\s*\}\}"
    r"|\{\{\s*(?P<ref>[a-zA-Z_][\w.]*)\s*\}\}\s*"
    r"(?P<op>==|!=|<=|>=|<|>|contains|in)\s*(?P<literal>.+?)"
    r")\s*$",
    re.IGNORECASE,
)


def _lookup(variables: dict[str, Any], ref: str) -> str | None:
    """Resolve a `{{a.b}}` reference against the run's collected variables.

    The dotted form is tried whole first, then by its last segment, because a
    tenant writing `{{order.placed_at}}` and an action returning `placed_at` mean
    the same thing and neither should have to know about the other.
    """
    for key in (ref, ref.split(".")[-1]):
        entry = variables.get(key)
        if entry is None:
            continue
        # a bare value is tolerated on read so a hand-edited run row still
        # renders, even though nothing in this module ever writes one
        value = entry.get("value") if isinstance(entry, dict) else entry
        if value is None:
            continue
        rendered = str(value).strip()
        if rendered:
            return rendered
    return None


def _compare(left: str, op: str, right: str) -> bool | None:
    op = op.lower()
    literal = right.strip()
    if op == "contains":
        return literal.strip("'\"").lower() in left.lower()
    if op == "in":
        options = [
            piece.strip().strip("'\"").lower()
            for piece in literal.strip("[]()").split(",")
        ]
        return left.lower() in [option for option in options if option]

    left_number = _coerce_number(left)
    right_number = _coerce_number(literal)
    if left_number is not None and right_number is not None:
        pair: tuple[float, float] = (left_number, right_number)
    else:
        left_date = _coerce_date(left)
        right_date = _coerce_date(literal)
        if left_date is not None and right_date is not None:
            pair = (left_date.timestamp(), right_date.timestamp())
        elif op in ("==", "!="):
            same = left.strip().strip("'\"").lower() == literal.strip("'\"").lower()
            return same if op == "==" else not same
        else:
            # an ordering comparison between two things that are neither numbers
            # nor dates is not false, it is unanswerable
            return None

    first, second = pair
    if op == "==":
        return first == second
    if op == "!=":
        return first != second
    if op == "<":
        return first < second
    if op == "<=":
        return first <= second
    if op == ">":
        return first > second
    return first >= second


def evaluate_guard(guard: str, variables: dict[str, Any]) -> tuple[bool | None, str]:
    """Evaluate a branch guard in Python. Returns `(result, reason)`.

    `result is None` means unevaluable — a missing variable, an unparseable term,
    too many terms — and the caller must then take the `otherwise` branch (§1.6
    rule 3). It never silently evaluates to true.

    Deliberately tiny: at most four terms joined by `and` / `or`, no parentheses,
    no `eval`, no new dependency. The reason string is what lands in
    `branch_log.reason`, so it names the variable that was missing rather than
    saying "false".
    """
    expression = " ".join((guard or "").split())
    if not expression:
        return None, "guard_unevaluable: empty guard"

    parts = re.split(r"\s+(and|or)\s+", expression, flags=re.IGNORECASE)
    terms = parts[0::2]
    joiners = [piece.lower() for piece in parts[1::2]]
    if len(terms) > 4:
        return None, "guard_unevaluable: more than four terms"

    values: list[bool] = []
    notes: list[str] = []
    for term in terms:
        match = _TERM.match(term)
        if match is None:
            return None, f"guard_unevaluable: cannot parse {term.strip()!r}"

        unary = match.group("unary")
        if unary is not None:
            ref = match.group("uref")
            present = _lookup(variables, ref) is not None
            outcome = present if unary.lower() == "exists" else not present
            values.append(outcome)
            notes.append(f"{unary.lower()} {ref} = {outcome}")
            continue

        ref = match.group("ref")
        left = _lookup(variables, ref)
        if left is None:
            return None, f"guard_unevaluable: {ref} missing"
        op = match.group("op")
        literal = match.group("literal")
        outcome_or_none = _compare(left, op, literal)
        if outcome_or_none is None:
            return None, f"guard_unevaluable: cannot compare {ref} with {literal!r}"
        values.append(outcome_or_none)
        notes.append(f"{ref} = {left!r} {op} {literal.strip()} is {outcome_or_none}")

    result = values[0]
    for joiner, value in zip(joiners, values[1:], strict=False):
        result = (result and value) if joiner == "and" else (result or value)
    return result, "; ".join(notes)


# ------------------------------------------------------------- step normalising


def _attr(source: Any, *names: str) -> Any:
    """First present attribute or key, over an object or a mapping.

    `flatten_steps` returns `FlatStep`, whose field names are owned by
    `app/domain/procedures.py`; `steps_snapshot` comes back from JSONB as plain
    dicts. Reading both through one accessor keeps the runtime from breaking on
    either shape, and a snapshot written by an older release still renders.
    """
    for name in names:
        if isinstance(source, dict):
            if name in source and source[name] is not None:
                return source[name]
            continue
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def _branch_to_dict(branch: Any) -> dict[str, Any]:
    return {
        "label": str(_attr(branch, "label") or ""),
        "condition": str(_attr(branch, "condition", "condition_text", "text") or ""),
        "guard": str(_attr(branch, "guard") or ""),
    }


def _flat_to_dict(flat: Any) -> dict[str, Any]:
    """One flattened step as the snapshot stores it.

    The nested `step` fallback is deliberate: a `FlatStep` may either copy the
    step's fields or carry the `ProcedureStep` itself, and both are read here so
    the snapshot is the same shape whichever it turns out to be.
    """
    inner = _attr(flat, "step") or flat
    branches = _attr(flat, "branches") or _attr(inner, "branches") or []
    return {
        "sid": str(_attr(flat, "sid") or _attr(inner, "sid") or ""),
        "kind": str(_attr(flat, "kind") or _attr(inner, "kind") or "instruction"),
        "text": str(_attr(flat, "text") or _attr(inner, "text") or ""),
        "step_path": str(_attr(flat, "step_path", "path", "label") or ""),
        "action_name": (
            str(_attr(flat, "action_name") or _attr(inner, "action_name") or "") or None
        ),
        "on_unavailable": (
            str(_attr(flat, "on_unavailable") or _attr(inner, "on_unavailable") or "")
            or None
        ),
        "required": bool(_attr(flat, "required") or _attr(inner, "required") or False),
        "branches": [_branch_to_dict(branch) for branch in branches],
    }


def _root_label(step_path: str) -> str:
    return step_path.split(">")[0].strip()


def _branch_label(step_path: str) -> str:
    """The branch a step sits inside, or "" for a top-level step.

    Depth is capped at 1 (§1 limits), so a path is either `'4'` or
    `'3 > otherwise > 3.3'` and the middle segment is the whole answer.
    """
    parts = [piece.strip() for piece in step_path.split(">")]
    return parts[1] if len(parts) >= 3 else ""


def _default_on_unavailable(action: ActionDefinition | None, step: dict[str, Any]) -> str:
    """The per-step degradation to apply, from §1.5's default table.

    An explicit `on_unavailable` on the step always wins. Otherwise a widget
    render that cannot render is skipped, a write is aborted rather than skipped
    — a visitor who leaves reassured while nothing happened is the genuinely
    worse outcome — and a read is asked for instead.
    """
    explicit = step.get("on_unavailable")
    if explicit in (
        ON_UNAVAILABLE_SKIP,
        ON_UNAVAILABLE_ASK,
        ON_UNAVAILABLE_ESCALATE,
        ON_UNAVAILABLE_ABORT,
    ):
        return str(explicit)
    if step.get("required"):
        return ON_UNAVAILABLE_ABORT
    if action is None:
        # the definition is not in this turn's toolset at all, so its kind and
        # effect are unknown. Asking the visitor is the only degradation that is
        # never destructive and never silently drops a promised side effect.
        return ON_UNAVAILABLE_ASK
    if action.kind is ActionKind.WIDGET:
        return ON_UNAVAILABLE_SKIP
    if action.effect is ActionEffect.WRITE:
        return ON_UNAVAILABLE_ABORT
    return ON_UNAVAILABLE_ASK


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------- turn state


@dataclass
class ProcedureTurn:
    """Everything one turn knows about procedures. Rebuilt from Postgres per turn.

    Not a cache and not a checkpoint: it is the row, plus the deltas this turn
    produced, held only until `finish_turn` writes them back. A process death
    between the two loses the turn's bookkeeping and nothing else — the run row
    still points at the last step the model was told about.
    """

    tenant_id: UUID
    agent_id: UUID | None = None
    conversation_id: UUID | None = None
    channel: str = str(Channel.API)

    # trigger catalogue, already filtered by required-action availability
    catalogue: list[dict[str, Any]] = field(default_factory=list)

    # the active run, when there is one
    run_id: UUID | None = None
    procedure_id: UUID | None = None
    procedure_name: str = ""
    procedure_version: int = 1
    steps: list[dict[str, Any]] = field(default_factory=list)
    variables: dict[str, Any] = field(default_factory=dict)
    branch_log: list[dict[str, Any]] = field(default_factory=list)
    step_index: int = 0
    step_path: str = "1"
    current_sid: str = ""
    turn_count: int = 0
    action_calls: int = 0
    bookkeeping_calls: int = 0

    # per-turn counters and latches
    bookkeeping_this_turn: int = 0
    started_this_turn: bool = False
    step_ordinal: int = 1
    stopped_for: str = ""
    dirty: bool = False

    # ending
    ended: bool = False
    ended_status: str = ""
    ended_outcome: str = ""
    ended_reason: str = ""
    escalation_id: UUID | None = None
    escalate_as: str = ""
    escalate_note: str = ""
    redact_transcript: bool = False

    # context for the prompt and for an escalation payload
    session_values: dict[str, str] = field(default_factory=dict)
    question: str = ""

    @property
    def live(self) -> bool:
        return self.run_id is not None and not self.ended

    @property
    def current(self) -> dict[str, Any] | None:
        if not self.live or not (0 <= self.step_index < len(self.steps)):
            return None
        return self.steps[self.step_index]

    def next_index(self, from_index: int, branch: str | None = None) -> int:
        """The flat index the run moves to, or -1 when the procedure is finished.

        Depth 1 makes this three cases. Into a chosen branch: its first child.
        Inside a branch: the next sibling with the same label, else out to the
        next top-level step. At top level: the next top-level step. Children of a
        branch that was not taken are never entered, which is the whole reason
        this is not `index + 1`.
        """
        if from_index < 0 or from_index >= len(self.steps):
            return -1
        here = self.steps[from_index]
        root = _root_label(str(here.get("step_path") or ""))

        if branch:
            for index in range(from_index + 1, len(self.steps)):
                path = str(self.steps[index].get("step_path") or "")
                if _root_label(path) == root and _branch_label(path) == branch:
                    return index
            # an empty branch is legal authoring: fall through to the step after
            # the whole decision point
            return self._next_top_level(from_index)

        label = _branch_label(str(here.get("step_path") or ""))
        if label:
            for index in range(from_index + 1, len(self.steps)):
                path = str(self.steps[index].get("step_path") or "")
                if _root_label(path) == root and _branch_label(path) == label:
                    return index
        return self._next_top_level(from_index)

    def _next_top_level(self, from_index: int) -> int:
        for index in range(from_index + 1, len(self.steps)):
            if not _branch_label(str(self.steps[index].get("step_path") or "")):
                return index
        return -1

    def collected(self) -> dict[str, str]:
        """Variable name to rendered value, provenance dropped."""
        flat: dict[str, str] = {}
        for name, entry in self.variables.items():
            value = entry.get("value") if isinstance(entry, dict) else entry
            if value is not None:
                flat[str(name)] = str(value)
        flat.update(self.session_values)
        return flat

    def remember(self, name: str, value: str, source: str) -> None:
        """Record a variable with where it came from.

        Provenance is the point of this column: a support lead has to be able to
        see that `order_total` came from the action and not from the customer's
        claim about it. Never flattened to a bare value.
        """
        clean = str(name).strip()
        if not clean or clean in self.session_values:
            return
        self.variables[clean] = {
            "value": str(value)[:500],
            "source": source,
            "at": _now(),
        }
        self.dirty = True

    def log_branch(
        self,
        *,
        step_path: str,
        label: str,
        condition_text: str,
        decided_by: str,
        reason: str,
    ) -> None:
        self.branch_log.append(
            {
                "step_path": step_path,
                "condition_label": label,
                "condition_text": condition_text,
                "decided_by": decided_by,
                "reason": reason,
                "at": _now(),
            }
        )
        self.dirty = True

    def latched(self, step_path: str) -> dict[str, Any] | None:
        """The decision already recorded for this decision point, if any.

        A branch is latched (§1.6): the run resumes inside the branch it already
        chose rather than letting a later turn re-decide and pick differently.
        """
        for entry in reversed(self.branch_log):
            if str(entry.get("step_path") or "") == step_path:
                return entry
        return None


def render_text(template: str, values: dict[str, str]) -> str:
    """Fill `{{variable}}` holes server-side before the model ever sees the step.

    An uncollected hole renders as `(not yet known)` rather than being left as
    `{{order_id}}`: the placeholder is what tells the model to ask for it, and a
    raw mustache in the prompt reads as a templating bug the model then explains
    to the visitor.
    """

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        for key in (name, name.split(".")[-1]):
            found = values.get(key)
            if found:
                return found
        return _NOT_YET_KNOWN

    return _VARIABLE_HOLE.sub(substitute, template or "")


def missing_holes(template: str, values: dict[str, str]) -> list[str]:
    names: list[str] = []
    for match in _VARIABLE_HOLE.finditer(template or ""):
        name = match.group(1)
        if not (values.get(name) or values.get(name.split(".")[-1])) and name not in names:
            names.append(name)
    return names


# ------------------------------------------------------------------- the runtime


class ProcedureRuntime:
    """Drives procedures for one turn, and writes down what happened.

    Constructed per runner, not per turn: it holds no turn state at all. Every
    method takes the `ProcedureTurn` it operates on, so two concurrent turns on
    one process cannot see each other's run.
    """

    def __init__(
        self,
        procedures: Any,
        *,
        settings: Any = None,
        escalation: Any = None,
        telemetry: Any = None,
    ) -> None:
        # `procedures` is the ProcedureService; the repo is reached the same way
        # the runner reaches the actions repo (`self._actions.repo`). Accepting a
        # bare repo too means a caller that wires the adapter directly still works.
        self._repo = getattr(procedures, "repo", None) or procedures
        self._service = procedures
        self._escalation = escalation
        self._telemetry = telemetry
        if settings is None:
            from app.core.config import get_settings

            settings = get_settings()
        self._settings = settings

    # ------------------------------------------------------------ turn start

    async def begin_turn(
        self,
        tenant_id: UUID,
        *,
        agent_id: UUID | None,
        conversation_id: UUID | None,
        channel: Channel | str,
        config: Any,
        toolset: ToolSet,
        question: str = "",
        contact_id: UUID | None = None,
        identity_verified: bool = False,
        external_user_ref: str | None = None,
    ) -> ProcedureTurn | None:
        """Load the run, or the trigger catalogue, for this turn.

        Returns `None` whenever procedures are not in play, which is the common
        case: the platform switch is off, the agent's switch is off, or there is
        no conversation to hang a run on. A `None` return leaves the answer path
        byte-for-byte what it was before procedures existed, exactly as an empty
        toolset does for actions.
        """
        if not getattr(self._settings, "PROCEDURES_ENABLED", False):
            return None
        tools = getattr(config, "tools", None)
        if tools is None or not getattr(tools, "procedures_enabled", False):
            return None
        if agent_id is None or conversation_id is None:
            # a run is keyed by conversation; without one there is nothing to
            # resume and nothing worth starting
            return None

        turn = ProcedureTurn(
            tenant_id=tenant_id,
            agent_id=agent_id,
            conversation_id=conversation_id,
            channel=str(channel),
            question=question,
            session_values={
                "conversation.id": str(conversation_id),
                "channel": str(channel),
            },
        )
        if contact_id is not None and identity_verified:
            turn.session_values["contact.id"] = str(contact_id)
        if external_user_ref:
            turn.session_values["user.ref"] = external_user_ref

        row: dict[str, Any] | None = None
        try:
            row = await self._repo.active_run(tenant_id, conversation_id)
        except Exception:
            # a procedures outage must not fail an answer. The turn degrades to
            # "no procedures this message", which is the pre-procedures behaviour
            logger.warning(
                "procedure_active_run_failed",
                tenant_id=str(tenant_id),
                conversation_id=str(conversation_id),
                exc_info=True,
            )
            return None

        if row is not None:
            self._hydrate(turn, row)
            if await self._expired(turn, row):
                return turn
            if not turn.steps:
                # the snapshot is empty, so the definition vanished before this
                # run recorded one. Nothing can be resumed from it.
                await self._mark_end(
                    turn,
                    status=STATUS_FAILED,
                    outcome=OUTCOME_HANDED_OFF,
                    reason=REASON_DEFINITION_DELETED,
                    escalate=True,
                    note="the procedure definition is no longer available",
                )
                return turn
            self._check_run_budgets(turn)
            return turn

        turn.catalogue = await self._catalogue(tenant_id, agent_id, toolset)
        return turn

    def _hydrate(self, turn: ProcedureTurn, row: dict[str, Any]) -> None:
        """Rebuild the run's state from the row. This replaces the checkpointer.

        Fields are lifted key by key rather than by validating the whole row into
        `ProcedureRunState`: the row carries columns the state model does not
        declare, and a strict model would reject the read that every turn depends
        on. The model is still built, so a field the domain renames fails here in
        one place rather than in five.
        """
        state = ProcedureRunState.model_construct(
            id=_as_uuid(row.get("id")),
            # cast, not validation: a status the domain does not know is a
            # database row this code must still be able to close, and refusing to
            # build the state would strand the run instead of ending it
            status=cast("Any", str(row.get("status") or STATUS_ACTIVE)),
            step_index=int(row.get("step_index") or 0),
            step_path=str(row.get("step_path") or "1"),
            current_sid=str(row.get("current_sid") or "") or None,
            variables=dict(row.get("variables") or {}),
            branch_log=cast("Any", list(row.get("branch_log") or [])),
            turn_count=int(row.get("turn_count") or 0),
            action_calls=int(row.get("action_calls") or 0),
            bookkeeping_calls=int(row.get("bookkeeping_calls") or 0),
            steps_snapshot=list(row.get("steps_snapshot") or []),
        )
        turn.run_id = state.id
        turn.procedure_id = (
            _as_uuid(row.get("procedure_id")) if row.get("procedure_id") else None
        )
        turn.procedure_name = str(row.get("procedure_name") or "")
        turn.procedure_version = int(row.get("procedure_version") or 1)
        turn.steps = [_flat_to_dict(step) for step in state.steps_snapshot]
        turn.variables = dict(state.variables)
        turn.branch_log = [dict(entry) for entry in list(row.get("branch_log") or [])]
        turn.step_index = state.step_index
        turn.step_path = state.step_path
        turn.current_sid = state.current_sid or ""
        # counted here rather than in `finish_turn`: a turn that dies mid-flight
        # must still have spent its turn, or a crash loop is free
        turn.turn_count = state.turn_count + 1
        turn.action_calls = state.action_calls
        turn.bookkeeping_calls = state.bookkeeping_calls
        turn.dirty = True

    async def _expired(self, turn: ProcedureTurn, row: dict[str, Any]) -> bool:
        """Close a run whose TTL has passed rather than resuming it.

        `checkpoint_expiry` sweeps these, but a swept-late run would otherwise
        resume two days of stale context onto a visitor who has moved on, and the
        unique index would keep a fresh run from starting.
        """
        expires = row.get("expires_at")
        if not isinstance(expires, datetime):
            return False
        deadline = expires if expires.tzinfo is not None else expires.replace(tzinfo=UTC)
        if deadline > datetime.now(UTC):
            return False
        await self._mark_end(
            turn,
            status=STATUS_ABANDONED,
            outcome=OUTCOME_ABANDONED,
            reason=REASON_TTL_EXPIRED,
            escalate=False,
            note="",
        )
        return True

    def _check_run_budgets(self, turn: ProcedureTurn) -> None:
        """§6.1's per-run ceilings, evaluated before the model is called.

        These are the ones the rest of the system cannot catch: every other limit
        is per turn or per minute, and a procedure that loops "ask, look up, ask
        again" over forty turns passes all of them while burning forty prefills.
        """
        max_turns = int(getattr(self._settings, "PROCEDURE_MAX_TURNS", 25) or 25)
        max_calls = int(getattr(self._settings, "PROCEDURE_MAX_ACTION_CALLS", 12) or 12)
        if turn.turn_count > max_turns:
            turn.ended = True
            turn.ended_status = STATUS_FAILED
            turn.ended_outcome = OUTCOME_HANDED_OFF
            turn.ended_reason = REASON_TURN_BUDGET
            turn.escalate_as = str(EscalationTrigger.MODEL_REQUESTED)
            turn.escalate_note = (
                f"procedure '{turn.procedure_name}' ran for {turn.turn_count} turns "
                f"without resolving (ceiling {max_turns})"
            )
            logger.warning(
                "procedure_turn_budget_exhausted",
                run_id=str(turn.run_id),
                turns=turn.turn_count,
            )
            return
        if turn.action_calls >= max_calls:
            turn.ended = True
            turn.ended_status = STATUS_FAILED
            turn.ended_outcome = OUTCOME_HANDED_OFF
            turn.ended_reason = REASON_ACTION_BUDGET
            turn.escalate_as = str(EscalationTrigger.MODEL_REQUESTED)
            turn.escalate_note = (
                f"procedure '{turn.procedure_name}' spent its {max_calls} action "
                "calls without resolving"
            )
            logger.warning(
                "procedure_action_budget_exhausted",
                run_id=str(turn.run_id),
                calls=turn.action_calls,
            )

    async def _catalogue(
        self, tenant_id: UUID, agent_id: UUID, toolset: ToolSet
    ) -> list[dict[str, Any]]:
        """The trigger lines this turn may offer.

        Two filters, in this order. A procedure whose required actions are not
        available on this channel is **never offered** (§1.5): preventing the
        model from starting an SOP it cannot finish is strictly better than
        dropping it at step 7, after the visitor was already promised a refund.
        Then the list is capped at `PROCEDURE_TRIGGER_MAX_INJECTED`, most recently
        triggered first, so a large tenant's per-turn prompt cost stays flat and
        the model's attention lands on what they actually use.
        """
        try:
            rows = await self._repo.list_enabled(tenant_id, agent_id)
        except Exception:
            logger.warning(
                "procedure_catalogue_failed", tenant_id=str(tenant_id), exc_info=True
            )
            return []

        mode = str(getattr(self._settings, "PROCEDURE_SHORTLIST_MODE", "none") or "none")
        if mode == "embedding":
            # §1.1 keeps option C as an opt-in pre-filter, and it needs the query
            # vector that `search.py` computes for retrieval to be exposed on the
            # retrieval result. It is not, so this mode falls through to the
            # default ordering rather than making up a ranking.
            logger.info(
                "procedure_shortlist_embedding_unavailable", tenant_id=str(tenant_id)
            )

        available = set(toolset.action_ids.values())
        cap = int(getattr(self._settings, "PROCEDURE_TRIGGER_MAX_INJECTED", 12) or 12)

        offered: list[dict[str, Any]] = []
        for row in rows:
            required = row.get("required_actions") or []
            missing = [
                _as_uuid(item)
                for item in required
                if _as_uuid(item) not in available
            ]
            if missing:
                logger.info(
                    "procedure_trigger_withheld",
                    tenant_id=str(tenant_id),
                    procedure=str(row.get("name") or ""),
                    channel_missing=len(missing),
                )
                continue
            offered.append(
                {
                    "id": _as_uuid(row.get("id")),
                    "name": str(row.get("name") or ""),
                    "trigger_text": str(row.get("trigger_text") or ""),
                    "last_run_at": row.get("last_run_at"),
                }
            )

        offered.sort(
            key=lambda item: (
                item["last_run_at"] is None,
                -(
                    item["last_run_at"].timestamp()
                    if isinstance(item["last_run_at"], datetime)
                    else 0.0
                ),
                item["name"],
            )
        )
        return offered[: max(cap, 1)]

    # -------------------------------------------------------------- the prompt

    def prompt_block(self, turn: ProcedureTurn | None) -> str:
        """The one procedures block this turn's system prompt carries.

        While a run is live the trigger catalogue is *replaced* by the current
        step, so prompt size does not grow with a run's length, and only the
        current step is shown. With all nine steps in view the model reliably
        skips ahead and fires step 7's write before step 4's question has been
        answered; token cost is the secondary benefit and sequencing is the reason.
        """
        if turn is None:
            return ""
        if turn.live:
            return self._current_block(turn)
        return self._catalogue_block(turn)

    def _catalogue_block(self, turn: ProcedureTurn) -> str:
        if not turn.catalogue:
            return ""
        lines = [
            "Procedures you may start (call start_procedure with the id shown, do "
            "not paraphrase the steps):"
        ]
        for entry in turn.catalogue:
            trigger = " ".join(str(entry["trigger_text"]).split())
            lines.append(f"- {entry['name']} (id: {entry['id']}): {trigger}")
        return "\n".join(lines)

    def _current_block(self, turn: ProcedureTurn) -> str:
        step = turn.current
        if step is None:
            return ""
        values = turn.collected()
        rendered = render_text(str(step.get("text") or ""), values)
        total = len(turn.steps)
        position = turn.step_index + 1
        lines = [
            f"Current procedure: {turn.procedure_name} (step {position} of {total})",
            f"Step {step.get('step_path') or position}: {rendered}",
        ]

        if values:
            shown = []
            for name, entry in turn.variables.items():
                if not isinstance(entry, dict):
                    continue
                source = str(entry.get("source") or "")
                origin = "the customer" if source == SOURCE_CUSTOMER else source
                at = str(entry.get("at") or "")[11:16]
                suffix = f" (from {origin}{f', {at}' if at else ''})" if origin else ""
                shown.append(f"{name} = \"{entry.get('value')}\"{suffix}")
            if shown:
                lines.append("Collected so far: " + ", ".join(shown))

        still_needed = missing_holes(str(step.get("text") or ""), values)
        if still_needed:
            lines.append("Still needed: " + ", ".join(still_needed))

        branches = step.get("branches") or []
        if str(step.get("kind") or "") == "branch" and branches:
            lines.append(
                "This is a decision point. Judge which of these applies, then call "
                f"advance_procedure(step_sid=\"{step.get('sid')}\", branch_taken=..., "
                "note=\"why\"). You must supply a note."
            )
            for branch in branches:
                condition = render_text(str(branch.get("condition") or ""), values)
                lines.append(f"- {branch.get('label')}: {condition}")
        else:
            action_name = step.get("action_name")
            if action_name:
                lines.append(
                    f"This step uses the {action_name} action. Call it before you "
                    "advance."
                )
            lines.append(
                "When you have done this step, call "
                f"advance_procedure(step_sid=\"{step.get('sid')}\")."
            )
        return "\n".join(lines)

    # ------------------------------------------------- toolset, budget, dispatch

    def augment_toolset(self, toolset: ToolSet, turn: ProcedureTurn | None) -> ToolSet:
        """Add the reserved tools this turn is allowed to call.

        `start_procedure` only when no run is active and something can be
        started; `advance_procedure` and `end_procedure` only while a run is live.
        Declaring all three always would let the model close a run that does not
        exist and cost a tool declaration on every turn of every tenant.
        """
        if turn is None:
            return toolset
        extra: list[dict[str, Any]] = []
        if turn.live:
            extra.extend((ADVANCE_SPEC, END_SPEC))
        elif turn.catalogue:
            extra.append(START_SPEC)
        if not extra:
            return toolset

        names = {str(spec["name"]) for spec in extra}
        definitions = dict(toolset.actions)
        definitions.update({name: RESERVED_DEFINITIONS[name] for name in names})
        return ToolSet(
            specs=(*toolset.specs, *extra),
            actions=definitions,
            action_ids=toolset.action_ids,
            notes=toolset.notes,
            web_search=toolset.web_search,
        )

    def widen_budget(self, budget: Any, turn: ProcedureTurn | None) -> Any:
        """Give bookkeeping its own allowance instead of taking it from the work.

        §1.2: `advance_procedure` makes no external call and returns no new
        model-visible data beyond the next step, so charging it against
        `max_iterations` would let bookkeeping starve the real work. The loop
        counts iterations and calls, not tool kinds, so the honest way to exempt
        them is to widen the ceilings by exactly the separate bookkeeping cap the
        runtime then enforces itself.
        """
        if turn is None or not turn.live:
            return budget
        from dataclasses import replace as _replace

        allowance = self.max_bookkeeping
        return _replace(
            budget,
            max_iterations=budget.max_iterations + allowance,
            max_calls_total=budget.max_calls_total + allowance,
            max_calls_per_action=max(budget.max_calls_per_action, allowance),
        )

    @property
    def max_bookkeeping(self) -> int:
        return max(
            int(getattr(self._settings, "PROCEDURE_MAX_BOOKKEEPING_CALLS", 4) or 4), 1
        )

    def dispatcher(
        self, inner: Any, turn: ProcedureTurn | None, toolset: ToolSet | None = None
    ) -> Any:
        """Wrap the action dispatcher so the reserved names never reach it.

        The loop depends on the `ActionDispatcher` protocol and not on the
        executor, which is what makes this possible without touching the loop:
        one object, three names answered here, everything else delegated
        unchanged.
        """
        if turn is None:
            return inner
        wrapped = _ProcedureDispatcher(inner, self, turn)
        if toolset is not None:
            wrapped.bind_toolset(toolset)
        return wrapped

    # -------------------------------------------------------- reserved handlers

    async def handle(
        self,
        turn: ProcedureTurn,
        name: str,
        arguments: dict[str, Any],
        ctx: ActionContext,
        toolset: ToolSet | None = None,
    ) -> ActionOutcome:
        """Answer one reserved tool call. Never raises.

        A failure comes back as an `ActionOutcome` the model can read and explain,
        the same contract every other dispatch honours: raising here would abort a
        turn over bookkeeping and lose an answer the model had already written.
        """
        try:
            if name == TOOL_START:
                return await self._start(turn, arguments, toolset)
            if name == TOOL_ADVANCE:
                return await self._advance(turn, arguments, toolset)
            if name == TOOL_END:
                return await self._end(turn, arguments)
        except Exception:
            logger.warning(
                "procedure_tool_failed", tool=name, run_id=str(turn.run_id), exc_info=True
            )
            return ActionOutcome.failure(
                ActionOutcomeCode.TRANSPORT_ERROR,
                "the procedure could not be updated. Continue helping the visitor "
                "and do not claim a step was recorded.",
            )
        return ActionOutcome.failure(
            ActionOutcomeCode.DISABLED, f"'{name}' is not available."
        )

    async def _start(
        self, turn: ProcedureTurn, arguments: dict[str, Any], toolset: ToolSet | None
    ) -> ActionOutcome:
        """`start_procedure`. One per turn, and one active run per conversation.

        The per-turn latch below is §1.4's enforcement point. The database's
        partial unique index enforces one *active run per conversation*; this
        enforces one *start per turn*, which the index cannot see. A visitor who
        genuinely changes subject gets `end_procedure(abandoned_by_user)` now and
        a different procedure on the next turn.
        """
        if turn.started_this_turn or turn.live:
            turn.stopped_for = STOPPED_ALREADY_ACTIVE
            logger.info(
                "procedure_start_refused",
                tenant_id=str(turn.tenant_id),
                reason=STOPPED_ALREADY_ACTIVE,
            )
            return ActionOutcome.failure(
                ActionOutcomeCode.CONVERSATION_LIMIT,
                "a procedure is already running on this conversation. Finish it, or "
                "call end_procedure first. Only one procedure may start per message.",
            )

        wanted = str(arguments.get("procedure_id") or "").strip()
        entry = self._resolve_catalogue_entry(turn, wanted)
        if entry is None:
            offered = ", ".join(str(item["name"]) for item in turn.catalogue) or "none"
            return ActionOutcome.failure(
                ActionOutcomeCode.INVALID_ARGUMENTS,
                f"'{wanted}' is not a procedure you may start. Available: {offered}.",
            )

        procedure_id = _as_uuid(entry["id"])
        max_restarts = int(getattr(self._settings, "PROCEDURE_MAX_RESTARTS", 3) or 3)
        restarts = await self._restart_count(turn, procedure_id)
        if restarts >= max_restarts:
            logger.info(
                "procedure_restart_capped",
                tenant_id=str(turn.tenant_id),
                procedure_id=str(procedure_id),
                restarts=restarts,
            )
            return ActionOutcome.failure(
                ActionOutcomeCode.CONVERSATION_LIMIT,
                f"this procedure has already been run {restarts} times in this "
                "conversation. Help the visitor directly, or hand them to a person.",
            )

        row = await self._repo.get(turn.tenant_id, procedure_id)
        if row is None:
            return ActionOutcome.failure(
                ActionOutcomeCode.NOT_CONFIGURED,
                "that procedure is no longer available. Answer without it.",
            )

        steps = self._snapshot(row)
        if not steps:
            return ActionOutcome.failure(
                ActionOutcomeCode.NOT_CONFIGURED,
                "that procedure has no steps. Answer without it.",
            )

        ttl = int(getattr(self._settings, "PROCEDURE_RUN_TTL_SECONDS", 172_800) or 172_800)
        run_id = await self._repo.start_run(
            turn.tenant_id,
            agent_id=turn.agent_id,
            conversation_id=turn.conversation_id,
            procedure_id=procedure_id,
            procedure_name=str(row.get("name") or entry["name"]),
            procedure_version=int(row.get("version") or 1),
            steps_snapshot=steps,
            # NOT NULL, and the column the TTL sweep reads. A run with no
            # deadline is a zombie the Pending count would carry forever.
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        )

        turn.run_id = run_id
        turn.procedure_id = procedure_id
        turn.procedure_name = str(row.get("name") or entry["name"])
        turn.procedure_version = int(row.get("version") or 1)
        turn.steps = [_flat_to_dict(step) for step in steps]
        turn.step_index = 0
        turn.step_path = str(turn.steps[0].get("step_path") or "1")
        turn.current_sid = str(turn.steps[0].get("sid") or "")
        turn.turn_count = 1
        turn.started_this_turn = True
        turn.step_ordinal = 1
        turn.dirty = True
        turn.redact_transcript = bool(row.get("redact_transcript_on_escalation") or False)

        await self._touch(turn.tenant_id, procedure_id)
        await self._meter(turn.tenant_id, turn.procedure_name)
        logger.info(
            "procedure_started",
            tenant_id=str(turn.tenant_id),
            run_id=str(run_id),
            procedure=turn.procedure_name,
            steps=len(turn.steps),
        )

        settled = await self._settle(turn, 0, toolset)
        if settled is None:
            return ActionOutcome(ok=True, data=self._payload(turn, "the procedure is complete"))
        return ActionOutcome(ok=True, data=self._payload(turn, "started"))

    def _resolve_catalogue_entry(
        self, turn: ProcedureTurn, wanted: str
    ) -> dict[str, Any] | None:
        """Accept the id or the name.

        The catalogue renders both, and a model that echoes the human-readable
        name instead of the uuid is right about intent and wrong about format —
        refusing that is a self-inflicted failure.
        """
        if not wanted:
            return None
        lowered = wanted.lower()
        for entry in turn.catalogue:
            if str(entry["id"]).lower() == lowered or str(entry["name"]).lower() == lowered:
                return entry
        return None

    def _snapshot(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """The flattened step list as it is *right now*, frozen onto the run.

        Taken at run start so an edit to the procedure mid-run does not rewrite
        history and a run whose procedure was later deleted still renders in the
        dashboard.
        """
        raw = row.get("steps") or []
        try:
            flattened = flatten_steps(raw)
        except Exception:
            logger.warning(
                "procedure_flatten_failed", procedure=str(row.get("name") or ""), exc_info=True
            )
            flattened = []
        steps = [_flat_to_dict(step) for step in flattened]
        if not steps and isinstance(raw, list):
            # a stored snapshot that is already flat needs no flattening
            steps = [_flat_to_dict(step) for step in raw]
        for position, step in enumerate(steps, start=1):
            if not step.get("step_path"):
                step["step_path"] = str(position)
            if not step.get("sid"):
                step["sid"] = f"s{position}"
        return steps

    async def _advance(
        self, turn: ProcedureTurn, arguments: dict[str, Any], toolset: ToolSet | None
    ) -> ActionOutcome:
        """`advance_procedure`. Bookkeeping, capped separately from the work."""
        if not turn.live:
            return ActionOutcome.failure(
                ActionOutcomeCode.DISABLED,
                "no procedure is running. Answer the visitor directly.",
            )
        if turn.bookkeeping_this_turn >= self.max_bookkeeping:
            turn.stopped_for = STOPPED_BOOKKEEPING_CAP
            logger.info(
                "procedure_bookkeeping_capped",
                run_id=str(turn.run_id),
                calls=turn.bookkeeping_this_turn,
            )
            return ActionOutcome.failure(
                ActionOutcomeCode.RATE_LIMITED,
                "you have recorded enough steps for this message. Reply to the "
                "visitor now and continue the procedure when they answer.",
            )
        turn.bookkeeping_this_turn += 1
        turn.bookkeeping_calls += 1
        turn.dirty = True

        step = turn.current
        if step is None:
            return ActionOutcome.failure(
                ActionOutcomeCode.DISABLED, "this procedure has no current step."
            )

        given = str(arguments.get("step_sid") or "").strip()
        if not self._sid_matches(turn, step, given):
            return ActionOutcome.failure(
                ActionOutcomeCode.INVALID_ARGUMENTS,
                f"the current step is '{step.get('sid')}'. Complete that one before "
                "moving on, and pass its id.",
            )

        raw_variables = arguments.get("variables")
        if isinstance(raw_variables, dict):
            source = str(step.get("action_name") or "") or SOURCE_CUSTOMER
            for key, value in list(raw_variables.items())[:20]:
                if value is None:
                    continue
                turn.remember(str(key), str(value), source)

        note = str(arguments.get("note") or "").strip() or None
        values = turn.collected()

        if str(step.get("kind") or "") == "branch" and (step.get("branches") or []):
            return await self._take_branch(turn, step, arguments, note, values, toolset)

        rendered = render_text(str(step.get("text") or ""), values)
        await self._record_step(
            turn,
            step_path=str(step.get("step_path") or ""),
            step_text=rendered,
            action_name=step.get("action_name"),
            outcome=STEP_DONE,
            note=note,
        )
        moved = turn.next_index(turn.step_index)
        settled = await self._settle(turn, moved, toolset)
        if settled is None:
            return ActionOutcome(
                ok=True, data=self._payload(turn, "the procedure is complete")
            )
        return ActionOutcome(ok=True, data=self._payload(turn, "advanced"))

    @staticmethod
    def _sid_matches(turn: ProcedureTurn, step: dict[str, Any], given: str) -> bool:
        """The sid must be the current step, or the next one by exactly one.

        The tolerance exists because a model that has just been told step 4 and
        then narrates step 5 is off by one in its bookkeeping and not wrong about
        the work; anything further ahead is the skip this rule exists to catch.
        """
        if not given:
            return False
        if given == str(step.get("sid") or ""):
            return True
        ahead = turn.next_index(turn.step_index)
        if 0 <= ahead < len(turn.steps):
            return given == str(turn.steps[ahead].get("sid") or "")
        return False

    async def _take_branch(
        self,
        turn: ProcedureTurn,
        step: dict[str, Any],
        arguments: dict[str, Any],
        note: str | None,
        values: dict[str, str],
        toolset: ToolSet | None,
    ) -> ActionOutcome:
        """Decide a decision point: code when code can, the model otherwise.

        Order matters. A latched decision wins, so a later turn cannot re-decide
        and pick differently. Then a guard, evaluated in Python, because relative
        date arithmetic across a month boundary is the canonical model failure.
        Only with no guard does the model choose, and then it owes a note — with
        no note `branch_log` would be a list of unexplained turns and a lead
        investigating a wrong refund would have nothing to read.
        """
        branches: list[dict[str, Any]] = list(step.get("branches") or [])
        labels = [str(branch.get("label") or "") for branch in branches]
        step_path = str(step.get("step_path") or "")

        latched = turn.latched(step_path)
        if latched is not None:
            label = str(latched.get("condition_label") or "")
            decided_by = str(latched.get("decided_by") or "code")
            reason = str(latched.get("reason") or "latched")
        else:
            guarded = [branch for branch in branches if branch.get("guard")]
            if guarded:
                label, reason = self._decide_by_code(guarded, labels, values)
                decided_by = "code"
            else:
                label = str(arguments.get("branch_taken") or "").strip()
                if label not in labels:
                    return ActionOutcome.failure(
                        ActionOutcomeCode.INVALID_ARGUMENTS,
                        f"branch_taken must be one of: {', '.join(labels)}.",
                    )
                if note is None:
                    return ActionOutcome.failure(
                        ActionOutcomeCode.INVALID_ARGUMENTS,
                        "a decision point needs a note saying why this branch "
                        "applies. Add one and call advance_procedure again.",
                    )
                chosen = next(
                    (b for b in branches if str(b.get("label") or "") == label), {}
                )
                unmet = missing_holes(str(chosen.get("condition") or ""), values)
                if unmet:
                    # the model is about to decide on evidence it never collected,
                    # which is how a refund gets issued off a customer's claimed date
                    return ActionOutcome.failure(
                        ActionOutcomeCode.INVALID_ARGUMENTS,
                        f"you have not collected {', '.join(unmet)} yet. Ask the "
                        "visitor or run the step's action first.",
                    )
                decided_by = "model"
                reason = note
                logger.info(
                    "procedure_branch_model_decided",
                    run_id=str(turn.run_id),
                    step_path=step_path,
                    label=label,
                )
            turn.log_branch(
                step_path=step_path,
                label=label,
                condition_text=next(
                    (
                        str(b.get("condition") or "")
                        for b in branches
                        if str(b.get("label") or "") == label
                    ),
                    "",
                ),
                decided_by=decided_by,
                reason=reason,
            )

        await self._record_step(
            turn,
            step_path=step_path,
            step_text=render_text(str(step.get("text") or ""), values),
            action_name=None,
            outcome=STEP_BRANCH,
            note=f"{label} ({decided_by}): {reason}"[:500],
        )
        moved = turn.next_index(turn.step_index, branch=label)
        settled = await self._settle(turn, moved, toolset)
        if settled is None:
            return ActionOutcome(
                ok=True, data=self._payload(turn, "the procedure is complete")
            )
        return ActionOutcome(ok=True, data=self._payload(turn, f"branch {label}"))

    @staticmethod
    def _decide_by_code(
        guarded: list[dict[str, Any]], labels: list[str], values: dict[str, str]
    ) -> tuple[str, str]:
        """First guard that evaluates true wins; unevaluable falls to `otherwise`.

        A guard that cannot be evaluated never silently evaluates to true — the
        run takes `otherwise` and records which variable was missing.
        """
        fallback = "otherwise" if "otherwise" in labels else labels[-1]
        reasons: list[str] = []
        for branch in guarded:
            label = str(branch.get("label") or "")
            result, reason = evaluate_guard(str(branch.get("guard") or ""), values)
            if result is None:
                return fallback, reason
            reasons.append(f"{label}: {reason}")
            if result:
                return label, reason
        return fallback, "; ".join(reasons) or "no guard matched"

    async def _settle(
        self, turn: ProcedureTurn, index: int, toolset: ToolSet | None
    ) -> dict[str, Any] | None:
        """Land the run on a step it can actually do, or end it.

        Availability is resolved per step against this turn's `ToolSet`, which has
        already filtered by channel, `enabled`, `only_in_procedures` and
        `requires_verified_identity`. A widget-only action on the email channel is
        `skipped_unavailable` and the run continues: a nine-step refund flow whose
        step 6 renders a button is still eight useful steps, and dropping the whole
        procedure means the visitor gets nothing at all. A write is the exception —
        skipping it leaves the visitor reassured while nothing happened.
        """
        guard_rail = 0
        while 0 <= index < len(turn.steps) and guard_rail <= len(turn.steps):
            guard_rail += 1
            step = turn.steps[index]
            turn.step_index = index
            turn.step_path = str(step.get("step_path") or "")
            turn.current_sid = str(step.get("sid") or "")
            turn.dirty = True

            action_name = str(step.get("action_name") or "")
            if not action_name:
                return step
            action = toolset.get(action_name) if toolset is not None else None
            if action is not None:
                return step

            policy = _default_on_unavailable(action, step)
            values = turn.collected()
            rendered = render_text(str(step.get("text") or ""), values)
            logger.info(
                "procedure_step_action_unavailable",
                run_id=str(turn.run_id),
                step_path=turn.step_path,
                action=action_name,
                policy=policy,
            )
            if policy == ON_UNAVAILABLE_SKIP:
                await self._record_step(
                    turn,
                    step_path=turn.step_path,
                    step_text=rendered,
                    action_name=action_name,
                    outcome=STEP_SKIPPED,
                    note=f"{action_name} is not available on {turn.channel}",
                )
                index = turn.next_index(index)
                continue
            if policy == ON_UNAVAILABLE_ASK:
                await self._record_step(
                    turn,
                    step_path=turn.step_path,
                    step_text=rendered,
                    action_name=action_name,
                    outcome=STEP_ASKED,
                    note=f"{action_name} is not available on {turn.channel}; asking "
                    "the visitor instead",
                )
                step = dict(step)
                step["action_name"] = None
                step["text"] = (
                    f"{rendered}\n(The {action_name} action is not available here. "
                    "Ask the visitor for what it would have told you.)"
                )
                turn.steps[turn.step_index] = step
                return step
            await self._record_step(
                turn,
                step_path=turn.step_path,
                step_text=rendered,
                action_name=action_name,
                outcome=(
                    STEP_ESCALATED if policy == ON_UNAVAILABLE_ESCALATE else STEP_ABORTED
                ),
                note=f"{action_name} is not available on {turn.channel}",
            )
            await self._mark_end(
                turn,
                status=(
                    STATUS_HANDED_OFF
                    if policy == ON_UNAVAILABLE_ESCALATE
                    else STATUS_FAILED
                ),
                outcome=OUTCOME_HANDED_OFF,
                reason=(
                    REASON_ESCALATED
                    if policy == ON_UNAVAILABLE_ESCALATE
                    else REASON_ACTION_UNAVAILABLE
                ),
                escalate=True,
                note=f"'{turn.procedure_name}' needs {action_name}, which is not "
                f"available on {turn.channel}",
            )
            return None

        await self._mark_end(
            turn,
            status=STATUS_RESOLVED,
            outcome=OUTCOME_RESOLVED,
            reason=REASON_COMPLETED,
            escalate=False,
            note="",
        )
        return None

    async def _end(self, turn: ProcedureTurn, arguments: dict[str, Any]) -> ActionOutcome:
        """`end_procedure`. The model's own exit, and the one that hands off."""
        if not turn.live:
            return ActionOutcome.failure(
                ActionOutcomeCode.DISABLED, "no procedure is running."
            )
        if turn.bookkeeping_this_turn >= self.max_bookkeeping:
            turn.stopped_for = STOPPED_BOOKKEEPING_CAP
            return ActionOutcome.failure(
                ActionOutcomeCode.RATE_LIMITED,
                "you have recorded enough procedure steps for this message.",
            )
        turn.bookkeeping_this_turn += 1
        turn.bookkeeping_calls += 1

        outcome = str(arguments.get("outcome") or "").strip()
        note = str(arguments.get("note") or "").strip() or None
        mapping = {
            OUTCOME_RESOLVED: (STATUS_RESOLVED, REASON_COMPLETED, False),
            OUTCOME_HANDED_OFF: (STATUS_HANDED_OFF, REASON_ESCALATED, True),
            OUTCOME_ABANDONED: (STATUS_ABANDONED, REASON_USER_ABANDONED, False),
            OUTCOME_NOT_APPLICABLE: (STATUS_ABANDONED, REASON_USER_ABANDONED, False),
        }
        if outcome not in mapping:
            return ActionOutcome.failure(
                ActionOutcomeCode.INVALID_ARGUMENTS,
                "outcome must be one of: " + ", ".join(mapping),
            )
        status, reason, escalate = mapping[outcome]
        await self._mark_end(
            turn,
            status=status,
            outcome=outcome,
            reason=reason,
            escalate=escalate,
            note=note or f"'{turn.procedure_name}' was handed off by the assistant",
        )
        return ActionOutcome(
            ok=True,
            data={
                "procedure": turn.procedure_name,
                "closed_as": outcome,
                "next_step": "The procedure is closed. Reply to the visitor in your "
                "own words; do not start another procedure in this message.",
            },
        )

    def _payload(self, turn: ProcedureTurn, status: str) -> dict[str, Any]:
        """What the model gets back: the next step, and nothing else.

        The current-procedure block in the system prompt is built once, before the
        turn, so a step reached mid-turn has to arrive through the tool result or
        the model is working from a stale pointer.
        """
        return {
            "procedure": turn.procedure_name,
            "status": status,
            "step_path": turn.step_path,
            "next_step": self._current_block(turn) or "The procedure is complete.",
        }

    async def _mark_end(
        self,
        turn: ProcedureTurn,
        *,
        status: str,
        outcome: str,
        reason: str,
        escalate: bool,
        note: str,
    ) -> None:
        """Record how the run ends. The write itself happens in `finish_turn`.

        Split so an escalation is never attempted from inside the tool loop, where
        it would spend the loop's wall clock on a notification the visitor is not
        waiting for.
        """
        turn.ended = True
        turn.ended_status = status
        turn.ended_outcome = outcome
        turn.ended_reason = reason
        turn.escalate_note = note
        if escalate:
            turn.escalate_as = str(EscalationTrigger.MODEL_REQUESTED)
        logger.info(
            "procedure_run_ending",
            run_id=str(turn.run_id),
            status=status,
            outcome=outcome,
            reason=reason,
        )

    # ------------------------------------------------------------- persistence

    async def _record_step(
        self,
        turn: ProcedureTurn,
        *,
        step_path: str,
        step_text: str,
        action_name: str | None,
        outcome: str,
        note: str | None = None,
    ) -> None:
        """One row per step actually executed. This is the dashboard's timeline."""
        if turn.run_id is None:
            return
        if turn.step_ordinal <= 0:
            turn.step_ordinal = await self._next_ordinal(turn.tenant_id, turn.run_id)
        try:
            await self._repo.append_step(
                turn.tenant_id,
                turn.run_id,
                ordinal=turn.step_ordinal,
                step_path=step_path or str(turn.step_ordinal),
                step_text=step_text[:4000],
                action_name=action_name,
                outcome=outcome,
                note=note,
            )
        except Exception:
            # the timeline is an audit aid, not the run pointer. Losing a row
            # costs a lead some context; failing the turn costs the visitor
            logger.warning(
                "procedure_step_not_recorded",
                run_id=str(turn.run_id),
                step_path=step_path,
                exc_info=True,
            )
            return
        turn.step_ordinal += 1

    async def finish_turn(
        self,
        turn: ProcedureTurn | None,
        *,
        config: Any = None,
        answer: str | None = None,
        confidence: float = 0.0,
        external_user_ref: str | None = None,
        message_id: UUID | None = None,
        stopped_for: str = "",
    ) -> None:
        """Write the turn's deltas back. Never raises.

        This is the other half of "no checkpointer": the pointer, the variables
        and the decision trail go to Postgres now, and the next turn reads them
        back with one indexed query. Nothing survives in memory.
        """
        if turn is None or turn.run_id is None:
            return
        try:
            if stopped_for == "injection_flagged" and not turn.ended:
                # §1.6: a poisoned action result must not be allowed to steer a
                # branch. The loop has already emptied the toolset; the run ends
                # and a person takes over.
                await self._mark_end(
                    turn,
                    status=STATUS_FAILED,
                    outcome=OUTCOME_HANDED_OFF,
                    reason=REASON_INJECTION,
                    escalate=True,
                    note=f"'{turn.procedure_name}' was stopped: an action result "
                    "contained text resembling instructions",
                )

            if turn.ended:
                if turn.escalate_as and config is not None:
                    turn.escalation_id = await self._escalate(
                        turn,
                        config=config,
                        answer=answer,
                        confidence=confidence,
                        external_user_ref=external_user_ref,
                        message_id=message_id,
                    )
                await self._repo.end_run(
                    turn.tenant_id,
                    turn.run_id,
                    status=turn.ended_status or STATUS_FAILED,
                    outcome=turn.ended_outcome or OUTCOME_NOT_APPLICABLE,
                    ended_reason=turn.ended_reason or REASON_COMPLETED,
                    escalation_id=turn.escalation_id,
                )
                return

            if not turn.dirty:
                return
            await self._repo.save_run(
                turn.tenant_id,
                turn.run_id,
                # the visitor's next message is what moves this on, so the run is
                # parked rather than left `active`. Pending is both, bounded by
                # last_activity_at, which is why a zombie cannot accumulate here
                status=STATUS_AWAITING_USER,
                step_index=turn.step_index,
                step_path=turn.step_path,
                current_sid=turn.current_sid,
                variables=turn.variables,
                branch_log=turn.branch_log,
                turn_count=turn.turn_count,
                action_calls=turn.action_calls,
                bookkeeping_calls=turn.bookkeeping_calls,
            )
        except Exception:
            logger.warning(
                "procedure_run_not_saved", run_id=str(turn.run_id), exc_info=True
            )

    async def _escalate(
        self,
        turn: ProcedureTurn,
        *,
        config: Any,
        answer: str | None,
        confidence: float,
        external_user_ref: str | None,
        message_id: UUID | None,
    ) -> UUID | None:
        """Hand a dead run to a person, with the run state attached.

        Never raises and never blocks the ending: a broken notification is a lost
        metric, and the run still closes with the reason that killed it.
        """
        if self._escalation is None:
            return None
        note = turn.escalate_note or f"procedure '{turn.procedure_name}' ended"
        if turn.redact_transcript:
            # the definition asks for the transcript to be withheld. The
            # escalation service assembles the transcript itself from
            # `EscalationConfig.transcript_turns` and takes no per-call override,
            # so this is recorded for the reviewer rather than enforced here.
            note = f"{note} [procedure policy: transcript redaction requested]"
            logger.info(
                "procedure_transcript_redaction_requested", run_id=str(turn.run_id)
            )
        try:
            decision = await self._escalation.maybe_escalate(
                tenant_id=turn.tenant_id,
                config=config,
                trigger=turn.escalate_as or str(EscalationTrigger.MODEL_REQUESTED),
                question=turn.question,
                channel=turn.channel,
                agent_id=turn.agent_id,
                conversation_id=turn.conversation_id,
                message_id=message_id,
                answer=answer,
                confidence=confidence,
                external_user_ref=external_user_ref,
                note=f"{note} (step {turn.step_path})"[:1000],
            )
        except Exception:
            logger.warning(
                "procedure_escalation_failed", run_id=str(turn.run_id), exc_info=True
            )
            return None
        return getattr(decision, "escalation_id", None)

    async def _touch(self, tenant_id: UUID, procedure_id: UUID) -> None:
        try:
            await self._repo.touch_last_run(tenant_id, procedure_id)
        except Exception:
            logger.debug("procedure_touch_failed", exc_info=True)

    async def _meter(self, tenant_id: UUID, procedure_name: str) -> None:
        """Metered, never billed: visible in the usage breakdown, absent from bills."""
        if self._telemetry is None:
            return
        try:
            await self._telemetry.record_usage(
                tenant_id, UsageKind.PROCEDURE_RUN, 1, procedure=procedure_name
            )
        except Exception:
            logger.debug("procedure_usage_not_recorded", exc_info=True)

    # Two reads the frozen repo surface has no method for. Kept here, in raw
    # text() SQL inside tenant_scope so RLS still owns the isolation, rather than
    # widening someone else's adapter.
    async def _restart_count(self, turn: ProcedureTurn, procedure_id: UUID) -> int:
        """How many times this procedure has already run in this conversation."""
        try:
            async with tenant_scope(turn.tenant_id) as session:
                result = await session.execute(
                    text(
                        "SELECT count(*) FROM procedure_runs "
                        " WHERE tenant_id = :tenant_id "
                        "   AND conversation_id = :conversation_id "
                        "   AND procedure_id = :procedure_id"
                    ),
                    {
                        "tenant_id": turn.tenant_id,
                        "conversation_id": turn.conversation_id,
                        "procedure_id": procedure_id,
                    },
                )
                return int(result.scalar() or 0)
        except Exception:
            logger.warning("procedure_restart_count_failed", exc_info=True)
            return 0

    async def _next_ordinal(self, tenant_id: UUID, run_id: UUID) -> int:
        """The next `procedure_run_steps.ordinal`, which is UNIQUE per run."""
        try:
            async with tenant_scope(tenant_id) as session:
                result = await session.execute(
                    text(
                        "SELECT coalesce(max(ordinal), 0) + 1 FROM procedure_run_steps "
                        " WHERE tenant_id = :tenant_id AND run_id = :run_id"
                    ),
                    {"tenant_id": tenant_id, "run_id": run_id},
                )
                return int(result.scalar() or 1)
        except Exception:
            logger.warning("procedure_ordinal_lookup_failed", exc_info=True)
            return 1


class _ProcedureDispatcher:
    """An `ActionDispatcher` that answers the three reserved names itself.

    Everything else is delegated untouched, including the attributes the loop
    reaches for by name (`suspend_for_confirmation`), so wrapping is invisible to
    both sides. This is what keeps `run_tool_loop` free of any knowledge of
    procedures.
    """

    def __init__(self, inner: Any, runtime: ProcedureRuntime, turn: ProcedureTurn) -> None:
        self._inner = inner
        self._runtime = runtime
        self._turn = turn
        self._toolset: ToolSet | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def bind_toolset(self, toolset: ToolSet) -> None:
        self._toolset = toolset

    async def dispatch(
        self, *, action: ActionDefinition, arguments: dict[str, Any], ctx: ActionContext
    ) -> ActionOutcome:
        if action.name in RESERVED_PROCEDURE_TOOLS:
            return await self._runtime.handle(
                self._turn, action.name, arguments, ctx, self._toolset
            )
        if self._inner is None:
            return ActionOutcome.failure(
                ActionOutcomeCode.DISABLED,
                f"{action.display_name} is not available right now.",
            )

        # §6.1's per-run action ceiling, applied here because here is the only
        # place inside the loop that sees every dispatch a run makes. A procedure
        # spans turns, so every per-turn and per-minute limit above this one is
        # evadable by simply taking another turn.
        turn = self._turn
        if turn.live:
            ceiling = int(
                getattr(self._runtime._settings, "PROCEDURE_MAX_ACTION_CALLS", 12) or 12
            )
            if turn.action_calls >= ceiling:
                logger.warning(
                    "procedure_action_ceiling_refused",
                    run_id=str(turn.run_id),
                    action=action.name,
                    calls=turn.action_calls,
                )
                return ActionOutcome.failure(
                    ActionOutcomeCode.QUOTA_EXHAUSTED,
                    "this procedure has used all the actions it is allowed. Tell the "
                    "visitor plainly what you could not complete and offer a person.",
                )
            turn.action_calls += 1
            turn.dirty = True
            ctx = replace_context(ctx, turn)

        outcome: ActionOutcome = await self._inner.dispatch(
            action=action, arguments=arguments, ctx=ctx
        )
        return outcome


def replace_context(ctx: ActionContext, turn: ProcedureTurn) -> ActionContext:
    """Stamp the run onto the dispatch context.

    `in_procedure` is what makes an `only_in_procedures` action dispatchable —
    that flag is the entire point of the field — and `procedure_run_id` is what
    joins the `action_calls` ledger row to the run a lead is reading.
    """
    from dataclasses import replace as _replace

    return _replace(ctx, procedure_run_id=turn.run_id, in_procedure=True)


def _as_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return UUID(int=0)
