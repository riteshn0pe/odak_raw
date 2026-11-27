"""``{{hole}}`` rendering for agent prompt templates.

One interpolation idiom for the whole codebase, borrowed from the action and
procedure templating that already exists (``app/services/actions/executor.py``
resolves ``{{input.order_id}}`` the same way): double braces are identity, single
braces are this run's data. A template is rendered with tenant facts *before*
any ``.format(...)`` the caller applies, so the two never collide — the renderer
only ever touches ``{{…}}`` and passes ``{…}`` through untouched.

Two forms:

    {{company.company_name}}                      required hole
    {{company.hours|we reply in business hours}}   optional hole, inline default

A required hole with no value raises :class:`MissingFacts` — it is never
rendered as an empty string, never left as literal ``{{…}}``, and never
invented. An optional hole renders its inline default. The default text is
literal: a default may not itself contain braces or a pipe, so a rendered
prompt can never smuggle a second hole in through its own fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

# {{namespace.path}} or {{namespace.path|default}}. The default is everything up
# to the closing brace, so it may contain spaces and punctuation but no braces
# and no pipe (a second pipe would be part of the default, which is useless
# rather than dangerous — the regex makes the first pipe the separator).
_HOLE = re.compile(r"\{\{\s*([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)\s*(?:\|([^{}]*))?\}\}")


class MissingFacts(Exception):
    """A required ``{{hole}}`` had no value. The run must not proceed with it."""

    def __init__(self, holes: list[str]) -> None:
        self.holes = holes
        super().__init__("missing required facts: " + ", ".join(holes))


@dataclass(slots=True, frozen=True)
class ResolvedHole:
    """Where a hole's value came from — rendered, defaulted, or missing."""

    name: str
    value: str
    defaulted: bool


def holes_in(template: str) -> list[str]:
    """Every hole path in a template, duplicates removed, in order of first use."""
    seen: list[str] = []
    for match in _HOLE.finditer(template):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def _lookup(values: Mapping[str, Any], path: str) -> Any:
    """Resolve a dotted path against a nested mapping, None when absent."""
    node: Any = values
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _as_text(value: Any) -> str:
    """A fact value as prompt text. Lists join; None and empty are ""."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item).strip() for item in value if str(item or "").strip())
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value).strip()


def render(
    template: str,
    values: Mapping[str, Any],
    *,
    runtime: Mapping[str, Any] | None = None,
) -> str:
    """Fill every ``{{hole}}`` from ``values`` (namespaced) and ``runtime``.

    ``values`` maps a namespace to its mapping — ``{"company": {...},
    "owner": {...}}``. ``runtime`` is a flat per-run mapping exposed under the
    ``runtime`` namespace (``{{runtime.language}}``). A hole is missing when its
    resolved text is empty; a missing hole with a default renders the default,
    a missing hole without one is collected and raised as
    :class:`MissingFacts` at the end, so one error names every gap rather than
    the caller discovering them one render at a time.
    """
    namespaces: dict[str, Any] = dict(values)
    if runtime:
        namespaces["runtime"] = dict(runtime)

    missing: list[str] = []
    resolved: dict[str, ResolvedHole] = {}

    def _fill(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        if name in resolved:
            hole = resolved[name]
        else:
            namespace = name.split(".", 1)[0]
            value = _lookup(namespaces, name) if namespace in namespaces else None
            text = _as_text(value)
            if text:
                hole = ResolvedHole(name=name, value=text, defaulted=False)
            elif default is not None:
                hole = ResolvedHole(name=name, value=default.strip(), defaulted=True)
            else:
                hole = ResolvedHole(name=name, value="", defaulted=False)
                missing.append(name)
            resolved[name] = hole
        return hole.value

    text = _HOLE.sub(_fill, template)
    if missing:
        raise MissingFacts(missing)
    return text


def render_preview(
    template: str,
    values: Mapping[str, Any],
    *,
    runtime: Mapping[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """Render for the owner's preview pane: never raises, names the gaps.

    The Instructions editor shows the template against the tenant's current
    facts. A required hole with no value is shown as ``‹field›`` (a character
    that cannot appear in a template the API accepts) and listed, so the owner
    sees exactly which values are still missing rather than an error page.
    """
    namespaces: dict[str, Any] = dict(values)
    if runtime:
        namespaces["runtime"] = dict(runtime)
    missing: list[str] = []

    def _fill(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        namespace = name.split(".", 1)[0]
        text = _as_text(_lookup(namespaces, name)) if namespace in namespaces else ""
        if text:
            return text
        if default is not None:
            return default.strip()
        missing.append(name)
        return f"‹{name}›"

    return _HOLE.sub(_fill, template), missing
