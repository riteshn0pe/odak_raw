"""Action definitions: the tenant-authored contract for one server-side call.

Every field here is attacker-influenced in some way — a tenant authors the
definition, and the model fills the parameters from a conversation with a
stranger. The validators are therefore the security boundary and not a
convenience: what is not expressible here cannot be attempted at runtime.

Two rules shape the whole file:

Template holes are namespaced, and each namespace has a different trust level.
``{{input.*}}`` is whatever the model collected from the visitor and is never
trusted for identity. ``{{contact.*}}`` comes only from a verified identity and
is the only namespace safe for "look up MY order". Nothing else can reach an
outbound request.

The destination is fixed at authoring time. Scheme and host may not contain a
template hole, so no conversation — and no injected API response — can move the
target of a call. The SSRF guard still runs on the rendered URL; forbidding a
templated host means it never has to be the only thing standing there.

``effect`` carries a third value, ``local``, and the write gate keys on
``kind IN ('server', 'client')`` rather than on effect alone. A ``client`` action
has no server callout, so a gate written for outbound server effects cannot
classify it; and a platform ``system`` handler that writes to our own
tenant-scoped storage or to a destination the tenant configured once is a
materially different risk from a tenant-authored outbound write to a third party.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.constants import (
    ACTION_MAX_CALLS_PER_CONVERSATION,
    ACTION_MAX_WRITES_PER_CONVERSATION,
    RESERVED_TOOL_NAMES,
    Channel,
)

__all__ = [
    "ACTION_NAME_RE",
    "DENIED_HEADERS",
    "DENIED_HEADER_PREFIXES",
    "HEADER_NAME_RE",
    "LEDGER_STATUS_BY_OUTCOME",
    "PARAM_NAME_RE",
    "RESERVED_TOOL_NAMES",
    "SECRET_REF_RE",
    "SECRET_TEMPLATE_RE",
    "SYSTEM_HANDLERS",
    "TEMPLATE_RE",
    "VARIABLE_NAMESPACES",
    "ActionAuth",
    "ActionAuthType",
    "ActionContext",
    "ActionDefinition",
    "ActionEffect",
    "ActionHttp",
    "ActionKind",
    "ActionOutcome",
    "ActionOutcomeCode",
    "ActionParameter",
    "ActionResponse",
    "DataAccess",
    "ParamType",
    "RenderedRequest",
    "WidgetRender",
]

ACTION_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
PARAM_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
TEMPLATE_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_.]{0,63})\s*\}\}")
# Named secrets are a separate namespace with an upper-case shape, because they
# resolve out of action_secrets rather than out of the turn. Kept distinct from
# TEMPLATE_RE so a secret hole can never be satisfied by conversation data.
SECRET_TEMPLATE_RE = re.compile(r"\{\{\s*secret\.([A-Z][A-Z0-9_]{0,39})\s*\}\}")
SECRET_REF_RE = re.compile(r"^action:[0-9a-f]{32}$")

# Headers a tenant may never set. Host and Content-Length enable request
# smuggling and cache poisoning; the hop-by-hop set corrupts the connection; and
# Cookie is denied because a cookie jar is per-visitor state we do not have and a
# static cookie is a credential that belongs in tenant_secrets.
DENIED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "keep-alive",
        "expect",
        "te",
        "trailer",
        "proxy-authorization",
        "proxy-authenticate",
        "cookie",
        "cookie2",
        "set-cookie",
        "content-type",
        "user-agent",
        "accept-encoding",
    }
)
DENIED_HEADER_PREFIXES = ("x-forwarded-", "proxy-", ":", "sec-")

VARIABLE_NAMESPACES = frozenset(
    {"input", "contact", "user", "conversation", "agent", "tenant"}
)


class ActionKind(StrEnum):
    SERVER = "server"  # we call an HTTPS API and return JSON to the model
    CLIENT = "client"  # the visitor's browser runs a host-page handler
    WIDGET = "widget"  # render UI only, no call, no result
    SYSTEM = "system"  # implemented in our own code (web_search, escalate, ...)


class ActionEffect(StrEnum):
    READ = "read"  # observes. safe to retry, safe to run concurrently
    WRITE = "write"  # changes a third-party system of record. never retried,
    # never concurrent, audited on every invocation, forces
    # advisory grounding
    LOCAL = "local"  # side effects exist but are confined to tenant-scoped
    # storage or a destination the tenant configured once and
    # the model cannot redirect. platform guards apply —
    # cooldown, hourly ceiling, destination allowlist, rate
    # limiter — instead of visitor confirmation


class ParamType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ENUM = "enum"
    DATE = "date"  # ISO-8601 date
    EMAIL = "email"


class ActionParameter(BaseModel):
    """One value the agent collects from the user conversationally."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str
    type: ParamType = ParamType.STRING
    # the model reads this to decide what to ask for, so it is required and
    # cannot be a placeholder
    description: str = Field(min_length=3, max_length=300)
    required: bool = True
    enum: list[str] = Field(default_factory=list, max_length=25)
    max_length: int = Field(default=200, ge=1, le=2000)
    minimum: float | None = None
    maximum: float | None = None
    # applied to the collected value before interpolation. A tenant regex is
    # compiled at save time so a bad one fails the save, not a live turn
    pattern: str | None = Field(default=None, max_length=200)
    # a sample value the author writes to show the model the shape it should
    # collect ("A-1029", not "an order id"). Optional, so every definition
    # already stored in JSONB keeps parsing untouched
    example: str | None = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def _name_shape(cls, value: str) -> str:
        if not PARAM_NAME_RE.match(value):
            raise ValueError("parameter name must be snake_case, 1-32 chars")
        return value

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"pattern is not a valid regex: {exc}") from exc
        # a nested quantifier is the catastrophic-backtracking shape; matching
        # runs per turn on model-supplied text, so it is refused rather than
        # timed out
        if re.search(r"\([^)]*[+*]\)[+*]", value):
            raise ValueError("pattern nests unbounded quantifiers")
        return value

    @model_validator(mode="after")
    def _enum_consistency(self) -> ActionParameter:
        if self.type is ParamType.ENUM and not self.enum:
            raise ValueError("an enum parameter needs at least one option")
        if self.type is not ParamType.ENUM and self.enum:
            raise ValueError("enum options are only valid on an enum parameter")
        return self

    def json_schema(self) -> dict[str, Any]:
        """JSON Schema for the provider's tool declaration.

        Deliberately minimal: Gemini's OpenAI-compatibility layer rejects
        oneOf/$ref and is inconsistent about additionalProperties, so only the
        universally accepted keywords are emitted.
        """
        # `example` is folded into the description instead of being emitted as
        # its own keyword: it is not in Gemini's accepted function-declaration
        # subset and an unknown schema key 400s there, while description text
        # reaches every provider unchanged.
        description = self.description
        if self.example:
            description = f"{description} Example: {self.example}."
        base: dict[str, Any] = {"description": description}
        match self.type:
            case ParamType.ENUM:
                base |= {"type": "string", "enum": list(self.enum)}
            case ParamType.INTEGER:
                base |= {"type": "integer"}
            case ParamType.NUMBER:
                base |= {"type": "number"}
            case ParamType.BOOLEAN:
                base |= {"type": "boolean"}
            case ParamType.DATE:
                base |= {"type": "string", "format": "date"}
            case ParamType.EMAIL:
                base |= {"type": "string", "format": "email"}
            case _:
                base |= {"type": "string", "maxLength": self.max_length}
        if self.minimum is not None:
            base["minimum"] = self.minimum
        if self.maximum is not None:
            base["maximum"] = self.maximum
        return base


class ActionAuthType(StrEnum):
    NONE = "none"
    BEARER = "bearer"  # Authorization: Bearer <secret>
    HEADER = "header"  # <header_name>: <secret>
    BASIC = "basic"  # Authorization: Basic base64(username:secret)
    QUERY = "query"  # ?<query_name>=<secret>


class ActionAuth(BaseModel):
    """How the outbound call authenticates.

    The credential is never here — only a reference into tenant_secrets. It is
    decrypted inside the executor at call time, interpolated into the rendered
    request, and redacted before anything is logged. No read path returns it.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: ActionAuthType = ActionAuthType.NONE
    secret_ref: str | None = Field(default=None, max_length=128)
    header_name: str | None = Field(default=None, max_length=64)
    query_name: str | None = Field(default=None, max_length=64)
    username: str | None = Field(default=None, max_length=128)

    @field_validator("secret_ref")
    @classmethod
    def _ref_shape(cls, value: str | None) -> str | None:
        if value is not None and not SECRET_REF_RE.match(value):
            raise ValueError("secret_ref must be an action secret reference")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> ActionAuth:
        if self.type is ActionAuthType.NONE:
            if self.secret_ref:
                raise ValueError("auth type none must not carry a secret reference")
            return self
        if not self.secret_ref:
            raise ValueError(f"auth type {self.type} requires a secret reference")
        if self.type is ActionAuthType.HEADER:
            if not self.header_name:
                raise ValueError("header auth requires header_name")
            lowered = self.header_name.lower()
            if not HEADER_NAME_RE.match(self.header_name):
                raise ValueError("header_name is not a valid header name")
            if lowered in DENIED_HEADERS or lowered.startswith(DENIED_HEADER_PREFIXES):
                raise ValueError(f"header {self.header_name!r} may not be set")
        if self.type is ActionAuthType.QUERY and not self.query_name:
            raise ValueError("query auth requires query_name")
        if self.type is ActionAuthType.BASIC and not self.username:
            raise ValueError("basic auth requires a username")
        return self


class ActionHttp(BaseModel):
    """The request template. Everything a tenant can shape about the wire call."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = Field(min_length=8, max_length=2048)
    query: dict[str, str] = Field(default_factory=dict, max_length=20)
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    # JSON request body template. Holes live inside string leaves; a hole that
    # is the entire leaf keeps the parameter's JSON type (an integer stays an
    # integer), which is what a typed API expects
    body: dict[str, Any] | None = None
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=30.0)

    @field_validator("url")
    @classmethod
    def _url_shape(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https":
            # http is refused even in development: a tenant action carries a
            # credential, and the dev convenience that URL_FETCH_ALLOW_PRIVATE
            # buys for document fixtures is not worth a plaintext bearer token
            raise ValueError("action url must be https")
        if not parsed.hostname:
            raise ValueError("action url has no host")
        if parsed.username or parsed.password:
            raise ValueError("action url must not embed credentials")
        # the destination is fixed at authoring time: a template hole in the
        # scheme, host, port, or userinfo would let a conversation choose where
        # we send a request, which is SSRF by design rather than by bug
        authority = value.split("://", 1)[1].split("/", 1)[0]
        if TEMPLATE_RE.search(authority) or "{{" in authority:
            raise ValueError("the host of an action url may not be templated")
        if parsed.fragment:
            raise ValueError("action url must not carry a fragment")
        return value

    @field_validator("headers")
    @classmethod
    def _headers_allowed(cls, value: dict[str, str]) -> dict[str, str]:
        for name, header_value in value.items():
            lowered = name.lower()
            if not HEADER_NAME_RE.match(name):
                raise ValueError(f"{name!r} is not a valid header name")
            if lowered in DENIED_HEADERS or lowered.startswith(DENIED_HEADER_PREFIXES):
                raise ValueError(f"header {name!r} may not be set")
            if TEMPLATE_RE.search(name) or "{{" in name:
                raise ValueError("header names may not be templated")
            if len(header_value) > 2048:
                raise ValueError(f"header {name!r} value is too long")
            # a CR, LF, or NUL in a header value is header injection / request
            # splitting. httpx rejects some of these; not relying on that
            if any(char in header_value for char in ("\r", "\n", "\0")):
                raise ValueError(f"header {name!r} value contains a control character")
        return value

    @model_validator(mode="after")
    def _body_only_where_meaningful(self) -> ActionHttp:
        if self.body is not None and self.method in ("GET", "DELETE"):
            raise ValueError(f"{self.method} must not carry a request body")
        if self.body is not None:
            # bounded here so a template cannot become the size problem
            rendered = json.dumps(self.body)
            if len(rendered) > 16_384:
                raise ValueError("request body template exceeds 16 KB")
        return self

    @property
    def host(self) -> str:
        """The authoring-time destination host, promoted to `actions.target_host`."""
        return (urlparse(self.url).hostname or "").lower()


class DataAccess(StrEnum):
    FULL = "full"
    LIMITED = "limited"


class ActionResponse(BaseModel):
    """What of the response the model is allowed to see."""

    model_config = ConfigDict(extra="forbid")

    data_access: DataAccess = DataAccess.FULL
    # dotted JSON paths. `a.b` for objects, `a[].b` to project across an array.
    # only allowlisted leaves reach the prompt, so a field the tenant never
    # chose — including one an attacker added to their own API response — is
    # invisible to the model
    allowed_fields: list[str] = Field(default_factory=list, max_length=100)
    # rename projected leaves, so two calendar vendors present one shape
    rename: dict[str, str] = Field(default_factory=dict, max_length=50)
    max_bytes: int = Field(default=20_480, ge=256, le=20_480)
    # captured by the test step; drives the field picker and is shown to the
    # tenant, never sent to a model
    sample: dict[str, Any] | None = None

    @field_validator("allowed_fields")
    @classmethod
    def _paths_shape(cls, value: list[str]) -> list[str]:
        for path in value:
            if not re.match(r"^[A-Za-z0-9_]+(\[\])?(\.[A-Za-z0-9_]+(\[\])?)*$", path):
                raise ValueError(f"{path!r} is not a valid field path")
            if path.count(".") > 6:
                raise ValueError(f"{path!r} is nested too deeply")
        return value

    @model_validator(mode="after")
    def _limited_needs_fields(self) -> ActionResponse:
        if self.data_access is DataAccess.LIMITED and not self.allowed_fields:
            raise ValueError("limited data access requires at least one allowed field")
        return self


class WidgetRender(BaseModel):
    """UI a widget-only action renders. No API call, no result."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["suggested_replies", "button", "form", "card"]
    label: str = Field(default="", max_length=120)
    # https only, and never javascript:/data:. the widget re-validates before
    # setting href, because a check that only exists server-side is one bug
    # away from being no check
    url: str | None = Field(default=None, max_length=2048)
    options: list[str] = Field(default_factory=list, max_length=8)
    fields: list[ActionParameter] = Field(default_factory=list, max_length=10)

    @field_validator("url")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if urlparse(value).scheme != "https":
            raise ValueError("a rendered link must be https")
        return value


# The closed set of platform-implemented handlers. `kind='system'` actions carry
# one of these; nothing else is dispatchable in our own code.
SYSTEM_HANDLERS = (
    "web_search",
    "escalate_to_human",
    "collect_lead",
    "collect_data",
    "notify_slack",
    # the calendar pair from the starter library: prebuilt code with a provider
    # setting, not two generic HTTP templates, because the two vendors need
    # different request shapes and a two-call sequence
    "book_meeting",
    "check_availability",
)


class ActionDefinition(BaseModel):
    """One action, as authored and as executed. The stored JSONB is this model."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # becomes the tool name the model sees
    name: str
    display_name: str = Field(min_length=1, max_length=80)
    # the "when to use" text. this is the entire basis on which the model
    # decides to call, so a vague one is a functional defect
    description: str = Field(min_length=10, max_length=1000)
    kind: ActionKind = ActionKind.SERVER
    effect: ActionEffect = ActionEffect.READ

    parameters: list[ActionParameter] = Field(default_factory=list, max_length=15)
    http: ActionHttp | None = None
    response: ActionResponse = Field(default_factory=ActionResponse)
    auth: ActionAuth = Field(default_factory=ActionAuth)
    render: WidgetRender | None = None
    system_handler: (
        Literal[
            "web_search",
            "escalate_to_human",
            "collect_lead",
            "collect_data",
            "notify_slack",
            "book_meeting",
            "check_availability",
        ]
        | None
    ) = None

    # the model never calls this on its own initiative; only a procedure step
    # referencing @name can fire it. the tool is still declared to the provider
    # while a procedure is active, and withheld entirely otherwise
    only_in_procedures: bool = False
    # the visitor must say yes in their own turn before the call is made
    requires_confirmation: bool = False
    # withheld from the toolset unless the turn carries a verified identity
    requires_verified_identity: bool = False
    # channels where this action can run. an action rendering a widget button
    # cannot run on WhatsApp, and a procedure referencing it degrades per-step
    channels: list[Channel] = Field(
        default_factory=lambda: [Channel.WIDGET, Channel.WHATSAPP, Channel.API]
    )
    rate_limit_per_minute: int = Field(default=30, ge=1, le=600)
    # send the masked conversation transcript with the request. audited when
    # enabled, because it is an egress of conversation content
    include_transcript: bool = False
    version: int = 1

    @field_validator("name")
    @classmethod
    def _name_shape(cls, value: str) -> str:
        if not ACTION_NAME_RE.match(value):
            raise ValueError("action name must be snake_case, 2-48 chars")
        if value in RESERVED_TOOL_NAMES:
            raise ValueError(f"{value!r} is reserved")
        return value

    @field_validator("channels")
    @classmethod
    def _channels_present(cls, value: list[Channel]) -> list[Channel]:
        if not value:
            raise ValueError("an action must be available on at least one channel")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _kind_coherent(self) -> ActionDefinition:
        match self.kind:
            case ActionKind.SERVER:
                if self.http is None:
                    raise ValueError("a server action needs an http request")
                if self.render is not None or self.system_handler is not None:
                    raise ValueError("a server action has no render or handler")
            case ActionKind.CLIENT:
                if self.http is not None:
                    raise ValueError("a client action makes no server-side request")
                if self.effect is ActionEffect.WRITE:
                    # the result comes back through the visitor's browser and is
                    # forgeable, so it can never be the record of a change
                    raise ValueError("a client action must be read-only")
            case ActionKind.WIDGET:
                if self.render is None:
                    raise ValueError("a widget action needs a render definition")
                if self.http is not None or (self.parameters and self.render.type != "form"):
                    raise ValueError("a widget action takes no request")
            case ActionKind.SYSTEM:
                if self.system_handler is None:
                    raise ValueError("a system action needs a handler name")
                if self.http is not None:
                    raise ValueError("a system action is implemented in code")
        # `local` is the platform-handler effect: side effects confined to
        # surfaces we or the tenant already own, with platform guards instead of
        # visitor confirmation. A tenant-authored server or client action reaches
        # a third party, so it can never claim it.
        if self.effect is ActionEffect.LOCAL and self.kind is not ActionKind.SYSTEM:
            raise ValueError("only a system action may have a local effect")
        return self

    @model_validator(mode="after")
    def _templates_resolve(self) -> ActionDefinition:
        """Every {{hole}} names a known namespace and a real parameter.

        This is where "look up MY order" is made safe: an action that reads
        {{contact.*}} is forced to declare requires_verified_identity, so it can
        never run for an anonymous visitor and can never be filled from
        something the model was told in conversation.
        """
        declared = {p.name for p in self.parameters}
        holes: set[str] = set()
        for blob in (self.http.model_dump() if self.http else {},):
            holes |= {m.group(1) for m in TEMPLATE_RE.finditer(json.dumps(blob))}

        uses_contact = False
        for hole in holes:
            namespace, _, rest = hole.partition(".")
            if namespace not in VARIABLE_NAMESPACES or not rest:
                raise ValueError(f"{{{{{hole}}}}} is not an available variable")
            if namespace == "input" and rest not in declared:
                raise ValueError(f"{{{{{hole}}}}} has no matching parameter")
            if namespace in ("contact", "user"):
                uses_contact = True

        if uses_contact and not self.requires_verified_identity:
            raise ValueError(
                "an action reading contact or user attributes must set "
                "requires_verified_identity"
            )
        # The write gate, narrowed to kind IN ('server', 'client'): a write the
        # model can trigger unprompted is one prompt injection away from being
        # triggered by a stranger. A `system` handler's writes are `local` and
        # carry platform guards instead, and a `widget` makes no call at all.
        if (
            self.kind in (ActionKind.SERVER, ActionKind.CLIENT)
            and self.effect is ActionEffect.WRITE
            and not (self.only_in_procedures or self.requires_confirmation)
        ):
            raise ValueError(
                "a write action must be procedure-only or require confirmation"
            )
        return self

    def tool_spec_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {p.name: p.json_schema() for p in self.parameters},
                    "required": [p.name for p in self.parameters if p.required],
                },
            },
        }

    def tool_spec_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": {p.name: p.json_schema() for p in self.parameters},
                "required": [p.name for p in self.parameters if p.required],
            },
        }

    def tool_spec(self) -> dict[str, Any]:
        """The canonical inbound tool shape: the Anthropic triple.

        One shape crosses the LLM port. `openai_compatible.py` translates it to
        `{"type": "function", "function": {...}}` on the way out, so a caller
        never chooses a dialect and a new provider needs no change here.
        """
        return self.tool_spec_anthropic()

    def promoted_columns(self) -> dict[str, Any]:
        """The `actions` columns that mirror this definition.

        The migration's contract is that every promoted column is written from
        `definition` in the same statement and never edited independently, so it
        is derived here rather than accepted from a caller.
        """
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "kind": str(self.kind),
            "effect": str(self.effect),
            "only_in_procedures": self.only_in_procedures,
            "requires_confirmation": self.requires_confirmation,
            "requires_verified_identity": self.requires_verified_identity,
            "channels": [str(channel) for channel in self.channels],
            "system_handler": self.system_handler,
            "target_host": self.http.host if self.http else None,
            "secret_ref": self.auth.secret_ref,
            "version": self.version,
        }

    def available_on(self, channel: Channel | str) -> bool:
        return str(channel) in {str(item) for item in self.channels}

    @property
    def is_write_gated(self) -> bool:
        """Whether the write gate applies: a tenant-authored outbound effect."""
        return (
            self.kind in (ActionKind.SERVER, ActionKind.CLIENT)
            and self.effect is ActionEffect.WRITE
        )


class ActionOutcomeCode(StrEnum):
    """Why a dispatch ended. Finer-grained than the ledger status on purpose.

    The model is told this, so it can explain itself ("I could not reach the
    order system"), and the tenant is shown it, so a misconfiguration is
    diagnosable without reading logs.
    """

    OK = "ok"
    # transport and protocol
    TIMEOUT = "timeout"
    REDIRECTED = "redirected"
    TOO_LARGE = "too_large"
    BAD_CONTENT_TYPE = "bad_content_type"
    BAD_JSON = "bad_json"
    HTTP_ERROR = "http_error"
    TRANSPORT_ERROR = "transport_error"
    # guards
    BLOCKED_SSRF = "blocked_ssrf"
    BLOCKED_HEADER = "blocked_header"
    CIRCUIT_OPEN = "circuit_open"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    CONVERSATION_LIMIT = "conversation_limit"
    WRITE_LIMIT = "write_limit"
    CHANNEL_UNAVAILABLE = "channel_unavailable"
    IDENTITY_REQUIRED = "identity_required"
    IDENTITY_INCOMPLETE = "identity_incomplete"
    INVALID_ARGUMENTS = "invalid_arguments"
    NOT_CONFIGURED = "not_configured"
    DISABLED = "disabled"
    # pauses and endings
    AWAITING_CLIENT = "awaiting_client"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    RENDERED = "rendered"


# `action_calls.status` has a CHECK constraint with eleven values; the outcome
# vocabulary above is richer. This is the only mapping between them, so an
# outcome added without a ledger status fails loudly here rather than as a
# constraint violation on a live insert.
LEDGER_STATUS_BY_OUTCOME: Mapping[str, str] = {
    ActionOutcomeCode.OK: "ok",
    ActionOutcomeCode.RENDERED: "ok",
    ActionOutcomeCode.TIMEOUT: "timeout",
    ActionOutcomeCode.REDIRECTED: "error",
    ActionOutcomeCode.TOO_LARGE: "error",
    ActionOutcomeCode.BAD_CONTENT_TYPE: "error",
    ActionOutcomeCode.BAD_JSON: "error",
    ActionOutcomeCode.HTTP_ERROR: "error",
    ActionOutcomeCode.TRANSPORT_ERROR: "error",
    ActionOutcomeCode.CIRCUIT_OPEN: "error",
    ActionOutcomeCode.NOT_CONFIGURED: "error",
    ActionOutcomeCode.BLOCKED_SSRF: "blocked",
    ActionOutcomeCode.BLOCKED_HEADER: "blocked",
    ActionOutcomeCode.RATE_LIMITED: "refused",
    ActionOutcomeCode.CONVERSATION_LIMIT: "refused",
    ActionOutcomeCode.WRITE_LIMIT: "refused",
    ActionOutcomeCode.CHANNEL_UNAVAILABLE: "refused",
    ActionOutcomeCode.INVALID_ARGUMENTS: "refused",
    ActionOutcomeCode.DISABLED: "refused",
    ActionOutcomeCode.QUOTA_EXHAUSTED: "quota_exhausted",
    ActionOutcomeCode.IDENTITY_REQUIRED: "identity_required",
    ActionOutcomeCode.IDENTITY_INCOMPLETE: "identity_incomplete",
    ActionOutcomeCode.AWAITING_CLIENT: "awaiting_client",
    ActionOutcomeCode.AWAITING_CONFIRMATION: "awaiting_confirmation",
    ActionOutcomeCode.DECLINED: "cancelled",
    ActionOutcomeCode.CANCELLED: "cancelled",
}


@dataclass(slots=True)
class RenderedRequest:
    """A concrete outbound request, after interpolation and credential injection.

    Held in a local inside the executor and never returned to a caller: the auth
    header value is plaintext here. `redact` is the only thing that turns one of
    these into something loggable.
    """

    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None = None
    # the query key the credential was placed under, if auth type is `query`.
    # Recorded rather than inferred so redaction never has to guess which value
    # looks secret.
    auth_query_key: str | None = None
    auth_header_name: str | None = None

    @property
    def request_bytes(self) -> int:
        return len(self.body or b"")


@dataclass(slots=True)
class ActionContext:
    """Everything about the turn that a dispatch is allowed to read.

    The namespaces here are the PII policy in code form. The transcript, the
    current message text, retrieved chunk text, other contacts and
    `external_user_ref` are deliberately not renderable into a request — only
    what is listed as a template namespace can leave.
    """

    tenant_id: UUID
    agent_id: UUID | None = None
    conversation_id: UUID | None = None
    message_id: UUID | None = None
    channel: Channel = Channel.API
    # the provider's tool-use id for this call. carried into a suspension row so
    # a resumed turn can match the answer to the call that asked for it
    call_id: str = ""

    # identity. `contact` is the only namespace safe for "look up MY order", and
    # it is populated from the contacts row, never from the conversation
    identity_verified: bool = False
    # the verified contact row this turn resolves `{{contact.*}}` from. On its own
    # it is not proof of anything: `ActionExecutor._resolve_contact` passes
    # `identity_verified` alongside it and the resolver refuses without both.
    contact_id: UUID | None = None
    contact: Mapping[str, Any] = field(default_factory=dict)
    external_user_ref: str | None = None

    # the turn's PII restore closure. `_mask` replaced the visitor's email with
    # [EMAIL_1] before the model ever saw it, so an un-restored {{input.email}}
    # would make every lookup fail against the tenant's own API. Masking exists
    # to keep PII from the model vendor, not from the customer's order system.
    restore: Callable[[str], str] | None = None
    # PII-masked transcript, only when the action sets include_transcript.
    # There is no unmasked option.
    transcript: str | None = None

    # per-conversation counters, maintained by the loop across turns
    calls_this_conversation: int = 0
    writes_this_conversation: int = 0
    max_calls_per_conversation: int = ACTION_MAX_CALLS_PER_CONVERSATION
    max_writes_per_conversation: int = ACTION_MAX_WRITES_PER_CONVERSATION

    procedure_run_id: UUID | None = None
    # true only while a procedure step is driving the call, which is what makes
    # an only_in_procedures action dispatchable
    in_procedure: bool = False
    # the visitor already said yes, in their own turn, to this exact call
    confirmed: bool = False

    def variables(self) -> dict[str, Mapping[str, Any] | dict[str, Any]]:
        """The non-input namespaces, as the renderer sees them."""
        return {
            "contact": self.contact,
            "user": self.contact,
            "conversation": {"id": str(self.conversation_id or "")},
            "agent": {"id": str(self.agent_id or "")},
            "tenant": {"id": str(self.tenant_id)},
        }


@dataclass(slots=True)
class ActionOutcome:
    """The result of one dispatch. Never carries a credential.

    `data` is what the model is allowed to see: already projected through
    `allowed_fields` when data access is limited, already size-capped, and
    always an object so the prompt template and the projector see one shape.
    """

    ok: bool
    outcome: str = ActionOutcomeCode.OK
    data: dict[str, Any] | None = None
    error: str | None = None
    http_status: int | None = None
    duration_ms: int = 0
    request_bytes: int = 0
    bytes_read: int = 0
    # widget renders and client actions carry a payload for the browser rather
    # than a result for the model
    render: dict[str, Any] | None = None
    # set when the turn must end and resume later
    suspension_id: UUID | None = None
    # PII labels restored into the outbound request. Labels only, never values.
    restored_labels: tuple[str, ...] = ()
    # true when this call should not count against the monthly quota — the
    # escape hatch is always open
    quota_exempt: bool = False

    @property
    def ledger_status(self) -> str:
        """The `action_calls.status` value for this outcome.

        Falls back to `error` rather than raising: a ledger row is written on the
        failure path, and an unmapped outcome must not turn a failed action into
        a failed turn.
        """
        return LEDGER_STATUS_BY_OUTCOME.get(self.outcome, "error")

    @property
    def suspended(self) -> bool:
        return self.outcome in (
            ActionOutcomeCode.AWAITING_CLIENT,
            ActionOutcomeCode.AWAITING_CONFIRMATION,
        )

    def for_model(self) -> dict[str, Any]:
        """The tool-result payload. A failure is a result, never an exception.

        An action that fails has to come back to the model as readable text, so
        it can tell the visitor what happened. Raising instead would kill the
        answer for something that is abuse control or a third party's outage.
        """
        if self.ok:
            return {"ok": True, "data": self.data if self.data is not None else {}}
        return {
            "ok": False,
            "outcome": str(self.outcome),
            "error": self.error or "the action could not be completed",
        }

    @classmethod
    def failure(
        cls,
        outcome: str,
        error: str,
        *,
        http_status: int | None = None,
        duration_ms: int = 0,
        bytes_read: int = 0,
    ) -> ActionOutcome:
        return cls(
            ok=False,
            outcome=outcome,
            error=error,
            http_status=http_status,
            duration_ms=duration_ms,
            bytes_read=bytes_read,
        )
