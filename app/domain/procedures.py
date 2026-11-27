"""The procedure domain: the step tree, the run state, and the validator's result.

A procedure is a named SOP owned by one agent — a trigger, an ordered list of
instruction steps, `@action` references inside the step text, `{{variable}}`
holes, and if / else-if / otherwise decision points. This module is the single
definition of that shape. Three consumers depend on it and none of them may hold
a second copy: the authoring API validates a submitted definition into
`ProcedureDefinition`, the repository stores `steps` as the JSON these models
serialise to, and the runtime reads `ProcedureRunState` back out of a
`procedure_runs` row.

**Field names here are the frontend's field names.** `dashboard/src/lib/types.ts`
is a frozen contract — the authoring screen ships against it — so a branch arm's
prose is `condition` and a decision point's `kind` is `"branch"`. The template
gallery in `app/services/procedures/library.py` predates that contract and writes
`condition_text` and `kind: "decision"`; both are accepted as input aliases and
normalised on the way in, so an installed template and an authored procedure are
the same shape in the database. Anything read back always serialises the frontend
spelling.

**The tree is capped at depth 1 and 15 instruction steps.** A branch's children
are instruction-only, because the authoring UI cannot render depth 3 legibly and
the model reliably loses the thread past one level of nesting. The 15-step cap
counts every instruction in the tree, including the ones inside branches; the
`jsonb_array_length(steps) <= 15` CHECK in
`migrations/010_procedures_contacts.sql` is only the cheap top-level backstop, so
the real enforcement is here.

**`flatten_steps` is the bridge to the runtime.** It produces the ordered
depth-first walk that becomes `procedure_runs.steps_snapshot`, with the
human-readable `step_path` labels the dashboard renders (`3 > otherwise > 3.3`,
joined with U+203A). `step_index` on a run is an ordinal into exactly that list,
which is why the snapshot is taken at run start and never recomputed: a mid-run
edit to the definition must not move the pointer.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------- literals

StepKind = Literal["instruction", "branch"]

# `procedure_runs.status`, mirroring the procedure_runs_status_valid CHECK.
RunStatus = Literal[
    "active",
    "awaiting_user",
    "resolved",
    "handed_off",
    "abandoned",
    "failed",
    "superseded",
]

# `procedure_runs.outcome` — what `end_procedure` was called with.
RunOutcome = Literal["resolved", "handed_off", "abandoned_by_user", "not_applicable"]

# `procedure_runs.ended_reason`, verbatim from the CHECK at
# migrations/010_procedures_contacts.sql:125-128. The outcome says what the
# visitor got; the reason says what closed the run, and they are not the same
# question — a run can be `handed_off` because a required action vanished.
EndedReason = Literal[
    "completed",
    "escalated",
    "user_abandoned",
    "ttl_expired",
    "turn_budget_exhausted",
    "action_budget_exhausted",
    "required_action_unavailable",
    "definition_deleted",
    "injection_flagged",
    "force_closed",
]

# `procedure_run_steps.outcome` — one row per step actually executed.
StepOutcome = Literal[
    "done",
    "skipped_unavailable",
    "asked_instead",
    "action_ok",
    "action_failed",
    "branch_taken",
    "escalated",
    "aborted",
]

# Per-step behaviour when a referenced action is unavailable on this channel
# (§1.5). A `write` step can never be `skip`: skipping a write leaves the visitor
# reassured and nothing done, which is worse than refusing.
OnUnavailable = Literal["skip", "ask", "escalate", "abort"]

# Who decided a branch. Code overrules the model wherever code can.
DecidedBy = Literal["code", "model"]

# -------------------------------------------------------------------- constants

# §1.6: the arm label is an enum so the model cannot invent a branch. `otherwise`
# is the fallback and is never one of the four conditions.
OTHERWISE_LABEL = "otherwise"
CONDITION_LABELS = ("b1", "b2", "b3", "b4")
BRANCH_LABELS = (*CONDITION_LABELS, OTHERWISE_LABEL)

# Authoring caps. Mirrored by PROCEDURE_MAX_STEPS / PROCEDURE_MAX_BRANCHES /
# PROCEDURE_MAX_BRANCH_DEPTH in app/core/config.py, which is what an operator
# tunes; these are the structural limits the models refuse to exceed regardless.
MAX_INSTRUCTION_STEPS = 15
MAX_BRANCHES_PER_DECISION = 5
MAX_BRANCH_DEPTH = 1
MAX_STEP_TEXT = 2000
MAX_TRIGGER_TEXT = 400
MIN_TRIGGER_TEXT = 10
MAX_PROCEDURE_NAME = 80

# The separator in a human step path: '3 > otherwise > 3.3'. U+203A, matching
# what `dashboard/src/lib/types.ts` documents the dashboard renders.
STEP_PATH_SEPARATOR = " › "  # noqa: RUF001

# §1.5's `@action` token, with the negation guard the naive pattern needs.
#
# The pattern the spec quotes — `@([a-z][a-z0-9_]{1,47})` — matches in two places
# it must not: inside a word (`support@lookup`, `x@y`) and inside an email
# address, where `ops@example.com` yields a phantom reference to `example`. Both
# are ordinary things to type into step prose, and a phantom reference becomes a
# `required_actions` entry that can never resolve, which silently makes the
# procedure impossible to enable.
#
# The lookbehind refuses an `@` preceded by anything that could be the local part
# of an address or the tail of a word. The lookahead refuses a token cut off
# mid-word (`@lookupOrder` resolves to nothing rather than to `@lookup`) and a
# token followed by a dot and a letter, which is a domain rather than a sentence
# ending.
ACTION_REF_RE = re.compile(
    r"(?<![0-9A-Za-z_.+%\-])@([a-z][a-z0-9_]{1,47})(?![0-9A-Za-z_]|\.[A-Za-z])"
)

# `{{variable}}` holes, interpolated server-side before a step is injected.
VARIABLE_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)\s*\}\}")


def action_references(text: str) -> list[str]:
    """Every `@action` name in this step text, in first-appearance order.

    Order matters: `required_actions` is resolved from this and the UI shows the
    references in the order the author wrote them.
    """
    found: list[str] = []
    for match in ACTION_REF_RE.finditer(text or ""):
        name = match.group(1)
        if name not in found:
            found.append(name)
    return found


def variable_references(text: str) -> list[str]:
    """Every `{{variable}}` hole in this text, in first-appearance order."""
    found: list[str] = []
    for match in VARIABLE_RE.finditer(text or ""):
        name = match.group(1)
        if name not in found:
            found.append(name)
    return found


# ----------------------------------------------------------------------- models


class ActionRef(BaseModel):
    """One `@name` reference, with the id resolved at publish time.

    Both halves are stored: the name is what the author wrote and what the UI
    renders, the id is the stable reference that survives a rename. `action_id`
    is None on an unresolved reference — a normal state in a builder, which is
    why it warns rather than rejecting, and why such a procedure cannot be
    enabled.
    """

    model_config = ConfigDict(populate_by_name=True)

    action_name: str
    action_id: UUID | None = None


class ProcedureStep(BaseModel):
    """One node of `procedures.steps`: an instruction, or a decision point.

    `sid` is the stable id the runtime passes back as `advance_procedure`'s
    `step_sid`, so a reorder in the editor must preserve it rather than renumber.
    `text` keeps `@action` and `{{variable}}` tokens as plain text; the editor
    renders them as chips without changing what is stored.

    `actions` is the parsed reference list. When a caller does not supply it — the
    authoring screen sends `{sid, kind, text}` and nothing more — it is derived
    from the step text here, which is the single place §1.5's parse happens. When
    a caller does supply it, it is trusted verbatim: the template gallery names
    `@collect_data` in one step's prose precisely in order to forbid it, and that
    mention is not a reference.
    """

    model_config = ConfigDict(populate_by_name=True)

    sid: str = Field(min_length=1, max_length=32)
    kind: StepKind = "instruction"
    text: str = Field(default="", max_length=MAX_STEP_TEXT)

    # Convenience mirror of the first reference, which is what the runtime needs
    # when it records `procedure_run_steps.action_name`.
    action_name: str | None = None
    actions: list[ActionRef] = Field(default_factory=list)

    # Set on a decision point: the condition of its first arm, so a collapsed
    # card can show what is being decided without expanding the branches.
    condition: str | None = Field(
        default=None, validation_alias=AliasChoices("condition", "condition_text")
    )
    branches: list[ProcedureBranch] = Field(default_factory=list)

    # §1.5. `required` is implicit on any step that fires a write action; the
    # availability of a required step is checked at trigger-injection time, before
    # the run exists, so the model never starts an SOP it cannot finish.
    required: bool = False
    requires_confirmation: bool = False
    on_unavailable: OnUnavailable | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _normalise_kind(cls, value: Any) -> Any:
        """`library.py` writes `decision`; the frozen frontend contract says
        `branch`. The contract wins and the alias is accepted forever, because
        stored rows written by an earlier installer still say `decision`."""
        if value == "decision":
            return "branch"
        return value

    @field_validator("sid")
    @classmethod
    def _check_sid(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,32}", value):
            raise ValueError("a step id may hold letters, digits, '_', '.' and '-' only")
        return value

    @model_validator(mode="after")
    def _resolve_shape(self) -> ProcedureStep:
        if "actions" not in self.model_fields_set:
            names = action_references(self.text)
            keep = {ref.action_name: ref.action_id for ref in self.actions}
            self.actions = [
                ActionRef(action_name=name, action_id=keep.get(name)) for name in names
            ]
        if self.action_name is None and self.actions:
            self.action_name = self.actions[0].action_name

        if self.kind == "branch":
            if self.condition is None and self.branches:
                self.condition = self.branches[0].condition
            labels = [branch.label for branch in self.branches]
            if len(labels) != len(set(labels)):
                raise ValueError("a decision point cannot repeat a branch label")
            if len(self.branches) > MAX_BRANCHES_PER_DECISION:
                raise ValueError(
                    f"a decision point may hold at most {MAX_BRANCHES_PER_DECISION} "
                    "branches, including 'otherwise'"
                )
        elif self.branches:
            raise ValueError("only a decision point may carry branches")
        return self

    @property
    def action_names(self) -> list[str]:
        """The referenced action names on this step, in author order."""
        return [ref.action_name for ref in self.actions]


class ProcedureBranch(BaseModel):
    """One arm of a decision point.

    `condition` is the prose the tenant typed and is None on the fallback arm,
    whose `label` is `otherwise`. `guard` is the optional parsed expression code
    evaluates: present and evaluable, code decides and the model is told the
    outcome; absent, the model decides and must justify it. Children are
    instruction-only — a branch inside a branch is the thing the editor refuses
    with *"Procedures work best flat."*
    """

    model_config = ConfigDict(populate_by_name=True)

    label: str
    condition: str | None = Field(
        default=None, validation_alias=AliasChoices("condition", "condition_text")
    )
    guard: str | None = Field(default=None, max_length=400)
    steps: list[ProcedureStep] = Field(default_factory=list)

    @field_validator("label")
    @classmethod
    def _check_label(cls, value: str) -> str:
        if value not in BRANCH_LABELS:
            raise ValueError(f"a branch label must be one of {', '.join(BRANCH_LABELS)}")
        return value

    @model_validator(mode="after")
    def _check_depth(self) -> ProcedureBranch:
        """Depth is capped at 1, enforced where the nesting would happen."""
        for child in self.steps:
            if child.kind == "branch":
                raise ValueError(
                    "a branch cannot contain another decision point; procedures "
                    "work best flat"
                )
        return self

    @property
    def is_fallback(self) -> bool:
        return self.label == OTHERWISE_LABEL


ProcedureStep.model_rebuild()


class ProcedureDefinition(BaseModel):
    """A whole procedure as the author submitted it, before it has an id.

    The aggregate limits live here rather than only in the validator service so
    that no caller can write a tree the runtime cannot walk. The service's
    validator reports the same limits as a readable list for the editor; this is
    the backstop that holds even when a future caller skips it.
    """

    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=MAX_PROCEDURE_NAME)
    trigger_text: str = Field(min_length=MIN_TRIGGER_TEXT, max_length=MAX_TRIGGER_TEXT)
    steps: list[ProcedureStep] = Field(default_factory=list)
    redact_transcript_on_escalation: bool = False

    @model_validator(mode="after")
    def _check_tree(self) -> ProcedureDefinition:
        if len(self.steps) > MAX_INSTRUCTION_STEPS:
            raise ValueError(
                f"a procedure may hold at most {MAX_INSTRUCTION_STEPS} top-level steps"
            )
        instructions = 0
        seen: set[str] = set()
        for step in self.steps:
            if step.sid in seen:
                raise ValueError(f"step id {step.sid!r} is used twice")
            seen.add(step.sid)
            if step.kind == "instruction":
                instructions += 1
            for branch in step.branches:
                for child in branch.steps:
                    if child.sid in seen:
                        raise ValueError(f"step id {child.sid!r} is used twice")
                    seen.add(child.sid)
                    instructions += 1
        if instructions > MAX_INSTRUCTION_STEPS:
            raise ValueError(
                f"a procedure may hold at most {MAX_INSTRUCTION_STEPS} instruction "
                f"steps across the whole tree; this one has {instructions}"
            )
        return self

    @property
    def action_names(self) -> list[str]:
        """Every referenced action name in the tree, in first-appearance order."""
        found: list[str] = []
        for step in self.steps:
            for name in step.action_names:
                if name not in found:
                    found.append(name)
            for branch in step.branches:
                for child in branch.steps:
                    for name in child.action_names:
                        if name not in found:
                            found.append(name)
        return found

    def resolved_action_ids(self) -> list[UUID]:
        """The union of resolved reference ids — `procedures.required_actions`.

        Denormalised at publish time so the per-turn availability check is one
        array comparison rather than a JSONB walk.
        """
        found: list[UUID] = []
        for step in self.steps:
            for ref in step.actions:
                if ref.action_id is not None and ref.action_id not in found:
                    found.append(ref.action_id)
            for branch in step.branches:
                for child in branch.steps:
                    for ref in child.actions:
                        if ref.action_id is not None and ref.action_id not in found:
                            found.append(ref.action_id)
        return found


# ------------------------------------------------------------- flattened walk


class FlatStep(BaseModel):
    """One entry of the ordered depth-first walk of a step tree.

    This is what `procedure_runs.steps_snapshot` holds and what `step_index`
    indexes. It is deliberately flat and self-describing: the runtime advances by
    ordinal without re-walking the tree, and the dashboard renders a run whose
    definition has since been edited or deleted straight out of the snapshot.

    A decision point appears in the walk in its own right, immediately before the
    children of its arms, because the run genuinely stops there to decide.
    """

    index: int
    sid: str
    kind: StepKind
    text: str
    step_path: str
    action_names: list[str] = Field(default_factory=list)
    action_ids: list[UUID] = Field(default_factory=list)
    required: bool = False
    requires_confirmation: bool = False
    on_unavailable: OnUnavailable | None = None

    # Set on a child step: which arm it sits under, and the decision point above
    # it. The runtime skips a whole arm by ordinal using these.
    parent_sid: str | None = None
    branch_label: str | None = None

    # Set on a decision point: the arms available, and their conditions and
    # guards, so the branch decision needs nothing but this row.
    condition: str | None = None
    guard: str | None = None
    branch_labels: list[str] = Field(default_factory=list)
    branch_conditions: dict[str, str | None] = Field(default_factory=dict)
    branch_guards: dict[str, str | None] = Field(default_factory=dict)


def _flat(
    step: ProcedureStep,
    *,
    index: int,
    step_path: str,
    parent_sid: str | None = None,
    branch_label: str | None = None,
) -> FlatStep:
    return FlatStep(
        index=index,
        sid=step.sid,
        kind=step.kind,
        text=step.text,
        step_path=step_path,
        action_names=step.action_names,
        action_ids=[ref.action_id for ref in step.actions if ref.action_id is not None],
        required=step.required,
        requires_confirmation=step.requires_confirmation,
        on_unavailable=step.on_unavailable,
        parent_sid=parent_sid,
        branch_label=branch_label,
        condition=step.condition,
        branch_labels=[branch.label for branch in step.branches],
        branch_conditions={branch.label: branch.condition for branch in step.branches},
        branch_guards={branch.label: branch.guard for branch in step.branches},
    )


def flatten_steps(steps: list[ProcedureStep]) -> list[FlatStep]:
    """The ordered depth-first walk, with the human step-path labels.

    Top-level steps are numbered from 1. A decision point's children are labelled
    `<n>`, then the arm label, then `<n>.<m>`, joined by `STEP_PATH_SEPARATOR`.
    That is the numbering the authoring UI shows and the label a support lead reads
    in the run timeline, which renders as `3`, `otherwise`, `3.3`.

    The walk is stable for a given tree, so a snapshot taken at run start stays
    comparable with the pointer stored on the run even after the definition moves
    on. `index` is contiguous across the whole walk, decision points included.
    """
    out: list[FlatStep] = []
    for position, step in enumerate(steps, start=1):
        top = str(position)
        out.append(_flat(step, index=len(out), step_path=top))
        for branch in step.branches:
            for child_position, child in enumerate(branch.steps, start=1):
                out.append(
                    _flat(
                        child,
                        index=len(out),
                        step_path=STEP_PATH_SEPARATOR.join(
                            (top, branch.label, f"{top}.{child_position}")
                        ),
                        parent_sid=step.sid,
                        branch_label=branch.label,
                    )
                )
    return out


# -------------------------------------------------------------------- run state


class RunVariable(BaseModel):
    """One collected variable, with its provenance.

    The provenance is the point: a support lead must be able to see that
    `order_total` came from the lookup and not from what the customer claimed, so
    `source` is never dropped and never defaulted to something vague.
    """

    value: str = ""
    source: str = "customer"
    at: datetime | None = None


class BranchLogEntry(BaseModel):
    """One decision point, recording who decided and why.

    Append-only. The branch is latched off this log — the run resumes inside the
    arm already chosen rather than re-deciding on a later turn — so an entry is
    never rewritten, only added.
    """

    sid: str
    step_path: str
    branch_taken: str | None = None
    decided_by: DecidedBy = "model"
    why: str | None = None
    at: datetime | None = None


class ProcedureRunState(BaseModel):
    """A `procedure_runs` row as the runtime works with it.

    There is no checkpointer and this is not a suspended computation: it is
    durable conversation state that has to survive process death on a
    scale-to-zero machine and, just as importantly, be readable by a support lead
    in the dashboard. That is why every field is a named column rather than an
    opaque serialised graph.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    status: RunStatus = "active"
    step_index: int = 0
    step_path: str = "1"
    current_sid: str | None = None
    variables: dict[str, RunVariable] = Field(default_factory=dict)
    branch_log: list[BranchLogEntry] = Field(default_factory=list)
    turn_count: int = 0
    action_calls: int = 0
    bookkeeping_calls: int = 0
    steps_snapshot: list[FlatStep] = Field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ProcedureRunState:
        """Build from a repository row, tolerating JSONB that arrived as text.

        The driver hands back a decoded object for a `jsonb` column in most
        configurations and a string in some, and a run lookup happens on every
        turn of every conversation — so this normalises once, here, rather than at
        each of the runtime's call sites.
        """
        return cls.model_validate(
            {
                "id": row["id"],
                "status": row.get("status") or "active",
                "step_index": int(row.get("step_index") or 0),
                "step_path": row.get("step_path") or "1",
                "current_sid": row.get("current_sid"),
                "variables": _as_json(row.get("variables"), {}),
                "branch_log": _as_json(row.get("branch_log"), []),
                "turn_count": int(row.get("turn_count") or 0),
                "action_calls": int(row.get("action_calls") or 0),
                "bookkeeping_calls": int(row.get("bookkeeping_calls") or 0),
                "steps_snapshot": _as_json(row.get("steps_snapshot"), []),
            }
        )

    def current(self) -> FlatStep | None:
        """The step the run is pointing at, or None once it has run off the end."""
        if 0 <= self.step_index < len(self.steps_snapshot):
            return self.steps_snapshot[self.step_index]
        return None

    def step_by_sid(self, sid: str) -> FlatStep | None:
        for step in self.steps_snapshot:
            if step.sid == sid:
                return step
        return None

    def latched_branch(self, sid: str) -> str | None:
        """The arm already chosen at this decision point, if any.

        Consulted before a branch is re-decided, so a model that changes its mind
        on a later turn does not send the run down a second path.
        """
        for entry in reversed(self.branch_log):
            if entry.sid == sid and entry.branch_taken:
                return entry.branch_taken
        return None

    def missing(self, names: list[str]) -> list[str]:
        """Which of these variables have not been collected yet."""
        return [name for name in names if name not in self.variables]


def _as_json(value: Any, fallback: Any) -> Any:
    """Decode a JSONB column that may arrive as text, a container, or None."""
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


# --------------------------------------------------------------- validation

Severity = Literal["error", "warning"]


class ValidationIssue(BaseModel):
    """One validator finding, addressed to a step wherever it has one.

    `sid` is what the editor highlights and `step_path` is what a human reads;
    both are carried because the two audiences are different and neither can be
    derived from the other once a tree has been reordered.
    """

    code: str
    message: str
    step_path: str | None = None
    sid: str | None = None
    severity: Severity = "error"


class ValidationResult(BaseModel):
    """The whole outcome of validating one definition.

    Warnings never block a save — half-authored procedures are a normal state in
    a builder — but an unresolved reference does block *enabling*, which is the
    line between "not finished" and "will fail in front of a visitor".
    """

    ok: bool = True
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)

    def add(self, issue: ValidationIssue) -> None:
        if issue.severity == "error":
            self.errors.append(issue)
            self.ok = False
        else:
            self.warnings.append(issue)
