"""The save-time validator: what a procedure must satisfy before it is stored.

Everything here answers one question — *will this procedure behave predictably in
front of a visitor?* — and it answers it in two registers. **Errors** block the
save. **Warnings** do not: a half-authored procedure is a normal state in a
builder, and refusing to store one would make the editor unusable. The line
between them is the line between "not finished yet" and "will fail in front of a
visitor".

Three rules here are load-bearing and are worth naming.

**A write action under a purely model-decided branch cannot be saved.** This is
the single most important rule in §1.6. A model that picks the wrong arm and then
issues a refund is the failure mode this whole design exists to prevent, so the
mitigation is not a warning badge: either the arm carries a `guard` that code
evaluates, or the action requires confirmation, or the procedure does not save.

**A missing `@action` reference warns; enabling one is refused.** The reference
may be an action the tenant has not installed yet, and rejecting the save would
mean an author cannot write a procedure before wiring its integrations. But a
procedure whose references do not resolve cannot be turned on, because the run
would collect an order number and then discover step 7 cannot run.

**The vague-condition lint is a warning, deliberately.** "If the order is recent"
has no ground truth and nothing can catch it automatically; what the author needs
is to be told the agent will judge it, not to be stopped.

**On guards.** §1.6 specifies a recursive-descent parser in
`app/services/procedures/guards.py` and a comparison type-check against each
variable's declared type. The parser is a separate module, and the declared types
do not exist on this surface at all: the frozen authoring contract
(`dashboard/src/lib/types.ts`) sends `{name, trigger_text, steps, …}` and carries
no variable declarations, so there is no declared type to compare against. What
runs here instead is a strict grammar check — term count, reference shape,
operator set, literal form — which rejects every malformed guard at save time. A
guard that parses but references an uncollected variable is a warning, and at run
time §1.6 step 3 takes the `otherwise` arm rather than guessing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from pydantic import ValidationError as PydanticError

from app.core.constants import RESERVED_TOOL_NAMES
from app.domain.procedures import (
    MAX_BRANCHES_PER_DECISION,
    MAX_INSTRUCTION_STEPS,
    OnUnavailable,
    ProcedureBranch,
    ProcedureDefinition,
    ProcedureStep,
    Severity,
    ValidationIssue,
    ValidationResult,
    flatten_steps,
    variable_references,
)

# The channels an action can be restricted to. A required step whose action is
# missing from one of these is why §1.5 never injects that procedure's trigger on
# that channel — the authoring UI says so at save time rather than making the
# tenant reason about it.
_ALL_CHANNELS = ("widget", "whatsapp", "email", "api")

# Words that promise precision the model cannot deliver without a guard. §1.6's
# lint: "the agent will judge this — add a check if it must be exact."
VAGUE_CONDITION_WORDS = frozenset(
    {
        "recent",
        "recently",
        "soon",
        "old",
        "new",
        "newer",
        "older",
        "large",
        "small",
        "big",
        "high",
        "low",
        "many",
        "few",
        "lots",
        "expensive",
        "cheap",
        "quickly",
        "slow",
        "long",
        "short",
        "significant",
        "substantial",
        "major",
        "minor",
        "reasonable",
        "unreasonable",
        "enough",
        "several",
        "while",
        "lately",
    }
)

# The guard DSL of §1.6, as recognisers rather than as a parser.
_GUARD_REF = r"\{\{\s*[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\s*\}\}"
_GUARD_OPS = ("==", "!=", "<=", ">=", "<", ">", "contains", "in")
_GUARD_LITERAL = (
    r"(?:-?\d+(?:\.\d+)?"  # number
    r"|'[^']{0,200}'"  # quoted text
    r"|\d{4}-\d{2}-\d{2}(?:T[0-9:.]+Z?)?"  # ISO-8601 date or timestamp
    r"|now(?:[+-]\d+[smhdw])?"  # relative to the server clock
    r"|true|false)"
)
_GUARD_TERM_COMPARISON = re.compile(
    rf"^(?P<ref>{_GUARD_REF})\s*(?P<op>==|!=|<=|>=|<|>|contains|in)\s*"
    rf"(?P<literal>{_GUARD_LITERAL})$",
    re.IGNORECASE,
)
_GUARD_TERM_PRESENCE = re.compile(rf"^(?:exists|missing)\s+(?P<ref>{_GUARD_REF})$")
_GUARD_REF_RE = re.compile(_GUARD_REF)
_GUARD_SPLIT = re.compile(r"\s+(?:and|or)\s+", re.IGNORECASE)
MAX_GUARD_TERMS = 4

# What `on_unavailable` defaults to per §1.5's table, decided at save time
# because it depends on the resolved action rather than on the step text.
_DEFAULT_ON_UNAVAILABLE = {"write": "abort", "widget": "skip", "read": "ask"}


def guard_issues(guard: str) -> list[str]:
    """Every reason this guard expression is not a valid one, in reading order.

    Empty means the guard is well-formed. The grammar is deliberately tiny — at
    most four terms joined by `and` / `or`, each a reference compared to a
    literal or an `exists` / `missing` test — so a bad guard is caught in the
    editor rather than at 2 a.m. in production.
    """
    text = (guard or "").strip()
    if not text:
        return ["a guard cannot be empty; leave it unset to let the agent judge"]
    if "(" in text or ")" in text:
        return ["a guard cannot use brackets; it is a flat list of up to four checks"]

    terms = [part.strip() for part in _GUARD_SPLIT.split(text)]
    problems: list[str] = []
    if len(terms) > MAX_GUARD_TERMS:
        problems.append(
            f"a guard may join at most {MAX_GUARD_TERMS} checks with 'and' or 'or'"
        )
    for term in terms:
        if _GUARD_TERM_PRESENCE.fullmatch(term) or _GUARD_TERM_COMPARISON.fullmatch(term):
            continue
        if not _GUARD_REF_RE.search(term):
            problems.append(
                f"{term!r} does not reference a variable; write it as {{{{name}}}}"
            )
            continue
        if not any(op in term for op in _GUARD_OPS):
            problems.append(
                f"{term!r} has no comparison; use one of "
                f"{', '.join(_GUARD_OPS)}, or 'exists'/'missing'"
            )
            continue
        problems.append(
            f"{term!r} is not a valid check; compare a {{{{variable}}}} to a number, "
            "a 'quoted string', a date, or a relative time like now-30d"
        )
    return problems


def guard_variables(guard: str | None) -> list[str]:
    """The variable names a guard reads, in order of first appearance."""
    if not guard:
        return []
    found: list[str] = []
    for match in _GUARD_REF_RE.finditer(guard):
        name = match.group(0).strip("{} ").strip()
        if name and name not in found:
            found.append(name)
    return found


def vague_words(condition: str | None) -> list[str]:
    """The vague temporal or magnitude words in a condition, if any."""
    if not condition:
        return []
    words = re.findall(r"[a-z]+", condition.lower())
    return sorted({word for word in words if word in VAGUE_CONDITION_WORDS})


def build_definition(
    payload: Mapping[str, Any],
) -> tuple[ProcedureDefinition | None, ValidationResult]:
    """Parse a submitted payload into a definition, reporting failures as lint.

    The structural limits live on the Pydantic models so that no caller can write
    a tree the runtime cannot walk, but a raw Pydantic failure is the wrong thing
    to hand an editor: it wants a list of findings addressed to steps. So the
    exception is translated into the same `ValidationIssue` shape every other
    check produces, and the caller renders one list.
    """
    result = ValidationResult()
    try:
        return ProcedureDefinition.model_validate(dict(payload)), result
    except PydanticError as exc:
        for item in exc.errors():
            location = ".".join(str(part) for part in item.get("loc", ()))
            result.add(
                ValidationIssue(
                    code="schema",
                    message=(
                        f"{location}: {item.get('msg', 'is invalid')}"
                        if location
                        else str(item.get("msg", "is invalid"))
                    ),
                    step_path=location or None,
                    severity="error",
                )
            )
        if result.ok:  # pragma: no cover - a PydanticError always yields an error
            result.add(
                ValidationIssue(
                    code="schema", message="the procedure is not valid", severity="error"
                )
            )
        return None, result


def _effect(entry: Mapping[str, Any] | None) -> str:
    return str((entry or {}).get("effect") or "read")


def _kind(entry: Mapping[str, Any] | None) -> str:
    return str((entry or {}).get("kind") or "read")


def _writes(step: ProcedureStep, available: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The write-effect actions this step fires, by name."""
    return [
        name
        for name in step.action_names
        if _effect(available.get(name)) == "write"
    ]


def resolve_references(
    definition: ProcedureDefinition, available: Mapping[str, Mapping[str, Any]]
) -> ProcedureDefinition:
    """Fill in `action_id` on every reference, and apply §1.5's step defaults.

    Mutates the definition in place and returns it, which is safe because it is
    built per request from the submitted payload. Three things are decided here
    rather than by the author: a step that fires a write action is `required`
    implicitly, `on_unavailable` falls back to the default for the resolved
    action's kind, and a write step can never be `skip` — skipping a write leaves
    the visitor reassured and nothing done.
    """
    for step in _walk(definition):
        for ref in step.actions:
            entry = available.get(ref.action_name)
            ref.action_id = entry["id"] if entry else None
        writes = _writes(step, available)
        if writes:
            step.required = True
        if step.on_unavailable is None and step.actions:
            first = available.get(step.actions[0].action_name)
            fallback = (
                "abort" if writes else _DEFAULT_ON_UNAVAILABLE.get(_kind(first), "ask")
            )
            step.on_unavailable = cast(OnUnavailable, fallback)
        if writes and step.on_unavailable == "skip":
            step.on_unavailable = "abort"
        if step.action_name is None and step.actions:
            step.action_name = step.actions[0].action_name
    return definition


def _walk(definition: ProcedureDefinition) -> list[ProcedureStep]:
    """Every step in the tree, decision points included, in document order."""
    out: list[ProcedureStep] = []
    for step in definition.steps:
        out.append(step)
        for branch in step.branches:
            out.extend(branch.steps)
    return out


def validate_definition(
    definition: ProcedureDefinition,
    *,
    available: Mapping[str, Mapping[str, Any]],
    for_enable: bool = False,
) -> ValidationResult:
    """Every finding for this definition, errors first.

    `available` is the agent's action index keyed by `@name`, as
    `ProceduresRepo.agent_action_index` returns it. `for_enable` promotes the
    unresolved-reference warnings to errors, which is the §1.5 rule that a
    procedure with a dangling reference may be saved but never turned on.
    """
    result = ValidationResult()
    paths = {entry.sid: entry.step_path for entry in flatten_steps(definition.steps)}

    if not definition.steps:
        result.add(
            ValidationIssue(
                code="no_steps",
                message="a procedure needs at least one step",
                severity="error",
            )
        )

    instructions = sum(1 for step in _walk(definition) if step.kind == "instruction")
    if instructions > MAX_INSTRUCTION_STEPS:
        result.add(
            ValidationIssue(
                code="too_many_steps",
                message=(
                    f"a procedure may hold at most {MAX_INSTRUCTION_STEPS} instruction "
                    f"steps across the whole tree; this one has {instructions}"
                ),
                severity="error",
            )
        )

    collected = _collected_variables(definition)

    for step in definition.steps:
        _check_step(step, result, paths, available, for_enable=for_enable)
        if step.kind == "branch":
            _check_decision(
                step, result, paths, available, collected, for_enable=for_enable
            )

    return result


def _collected_variables(definition: ProcedureDefinition) -> set[str]:
    """Variables some step interpolates, which is the only evidence at save time
    that a run will ever have them. A guard reading anything else is a guard that
    will fall through to `otherwise` every time."""
    names: set[str] = set()
    for step in _walk(definition):
        names.update(variable_references(step.text))
    return names


def _check_step(
    step: ProcedureStep,
    result: ValidationResult,
    paths: Mapping[str, str],
    available: Mapping[str, Mapping[str, Any]],
    *,
    for_enable: bool,
    inside_branch: ProcedureBranch | None = None,
) -> None:
    """Findings for one step, and for the children of its arms."""
    where = paths.get(step.sid)

    if step.kind == "instruction" and not step.text.strip():
        result.add(
            ValidationIssue(
                code="empty_step",
                message="a step needs some instruction text",
                step_path=where,
                sid=step.sid,
                severity="error",
            )
        )

    for name in step.action_names:
        if name in RESERVED_TOOL_NAMES:
            result.add(
                ValidationIssue(
                    code="reserved_action",
                    message=(
                        f"@{name} is a reserved platform tool and cannot be called "
                        "from a step"
                    ),
                    step_path=where,
                    sid=step.sid,
                    severity="error",
                )
            )
            continue
        entry = available.get(name)
        severity: Severity = "error" if for_enable else "warning"
        if entry is None:
            result.add(
                ValidationIssue(
                    code="action_unknown",
                    message=f"no action named @{name} is installed on this tenant",
                    step_path=where,
                    sid=step.sid,
                    severity=severity,
                )
            )
            continue
        if not entry.get("linked"):
            result.add(
                ValidationIssue(
                    code="action_unlinked",
                    message=f"@{name} is not enabled on this agent",
                    step_path=where,
                    sid=step.sid,
                    severity=severity,
                )
            )
        elif not entry.get("enabled"):
            result.add(
                ValidationIssue(
                    code="action_disabled",
                    message=f"@{name} is switched off",
                    step_path=where,
                    sid=step.sid,
                    severity=severity,
                )
            )
        if entry.get("setup_required"):
            result.add(
                ValidationIssue(
                    code="action_setup_required",
                    message=f"@{name} still needs its connection finished",
                    step_path=where,
                    sid=step.sid,
                    severity="warning",
                )
            )
        channels = [str(item) for item in (entry.get("channels") or [])]
        if step.required and channels and len(channels) < len(_ALL_CHANNELS):
            missing = sorted(set(_ALL_CHANNELS) - set(channels))
            if missing:
                result.add(
                    ValidationIssue(
                        code="unavailable_on_channel",
                        message=(
                            f"on {', '.join(missing)} this procedure will not be "
                            f"offered — @{name} is unavailable on that channel"
                        ),
                        step_path=where,
                        sid=step.sid,
                        severity="warning",
                    )
                )

    writes = _writes(step, available)
    if writes and step.on_unavailable == "skip":
        result.add(
            ValidationIssue(
                code="write_step_skip",
                message=(
                    f"a step that runs @{writes[0]} cannot be skipped when the action "
                    "is unavailable; skipping a write leaves the visitor reassured and "
                    "nothing done"
                ),
                step_path=where,
                sid=step.sid,
                severity="error",
            )
        )

    if writes and inside_branch is not None:
        unguarded = not (inside_branch.guard or "").strip()
        confirmed = step.requires_confirmation or all(
            bool((available.get(name) or {}).get("requires_confirmation"))
            for name in writes
        )
        if unguarded and not confirmed:
            result.add(
                ValidationIssue(
                    code="write_under_model_branch",
                    message=(
                        f"@{writes[0]} writes data inside a branch the agent decides on "
                        "its own; add a check to the condition, or require confirmation "
                        "on the action"
                    ),
                    step_path=where,
                    sid=step.sid,
                    severity="error",
                )
            )


def _check_decision(
    step: ProcedureStep,
    result: ValidationResult,
    paths: Mapping[str, str],
    available: Mapping[str, Mapping[str, Any]],
    collected: set[str],
    *,
    for_enable: bool = False,
) -> None:
    """Findings for one decision point and everything under its arms."""
    where = paths.get(step.sid)

    if not step.branches:
        result.add(
            ValidationIssue(
                code="branch_no_arms",
                message="a decision point needs at least one branch",
                step_path=where,
                sid=step.sid,
                severity="error",
            )
        )
        return
    if len(step.branches) > MAX_BRANCHES_PER_DECISION:
        result.add(
            ValidationIssue(
                code="branch_arm_limit",
                message=(
                    f"a decision point may hold at most {MAX_BRANCHES_PER_DECISION} "
                    "branches, including 'otherwise'"
                ),
                step_path=where,
                sid=step.sid,
                severity="error",
            )
        )
    if not any(branch.is_fallback for branch in step.branches):
        result.add(
            ValidationIssue(
                code="branch_missing_otherwise",
                message=(
                    "this decision has no 'otherwise' branch, so nothing happens when "
                    "none of the conditions hold"
                ),
                step_path=where,
                sid=step.sid,
                severity="warning",
            )
        )

    for branch in step.branches:
        label = f"{where} › {branch.label}" if where else branch.label  # noqa: RUF001
        if not branch.is_fallback and not (branch.condition or "").strip():
            result.add(
                ValidationIssue(
                    code="branch_missing_condition",
                    message=f"branch {branch.label} needs a condition",
                    step_path=label,
                    sid=step.sid,
                    severity="error",
                )
            )
        if branch.guard:
            for problem in guard_issues(branch.guard):
                result.add(
                    ValidationIssue(
                        code="guard_invalid",
                        message=f"branch {branch.label}: {problem}",
                        step_path=label,
                        sid=step.sid,
                        severity="error",
                    )
                )
            for name in guard_variables(branch.guard):
                root = name.split(".")[0]
                if name not in collected and root not in {"contact", "user", "session"}:
                    result.add(
                        ValidationIssue(
                            code="guard_variable_never_collected",
                            message=(
                                f"branch {branch.label} checks {{{{{name}}}}}, which no "
                                "step collects; the branch will always fall through to "
                                "'otherwise'"
                            ),
                            step_path=label,
                            sid=step.sid,
                            severity="warning",
                        )
                    )
        elif not branch.is_fallback:
            words = vague_words(branch.condition)
            if words:
                result.add(
                    ValidationIssue(
                        code="vague_condition",
                        message=(
                            f"branch {branch.label} says {words[0]!r}, so the agent will "
                            "judge this — add a check if it must be exact"
                        ),
                        step_path=label,
                        sid=step.sid,
                        severity="warning",
                    )
                )

        for child in branch.steps:
            _check_step(
                child,
                result,
                paths,
                available,
                for_enable=for_enable,
                inside_branch=branch,
            )
            if child.kind == "branch":
                result.add(
                    ValidationIssue(
                        code="branch_too_deep",
                        message=(
                            "a branch cannot contain another decision point; procedures "
                            "work best flat"
                        ),
                        step_path=paths.get(child.sid, label),
                        sid=child.sid,
                        severity="error",
                    )
                )
