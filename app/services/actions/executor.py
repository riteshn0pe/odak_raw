"""The action security envelope: render, guard, call, cap, project.

This is the part of the actions engine that can destroy the platform, so it is
organised as a list of controls rather than as a request pipeline. Each one stops
a named attack:

* **Scheme and host are fixed at authoring time.** `ActionHttp._url_shape` refuses
  a template hole anywhere in the authority, and the rendered URL is re-checked
  here. Without it, `https://{{input.region}}.api.example.com/orders` lets a
  prompt-injected model choose where the tenant's bearer token is sent.
* **SSRF on the rendered URL, every time.** `assert_fetchable` with
  `allow_private=False` hardcoded — actions do not participate in
  `URL_FETCH_ALLOW_PRIVATE` at all, because an action pointed at localhost in
  development is a habit that becomes a production incident.
* **Redirects are an error, not a hop.** Following a 3xx means re-attaching
  `Authorization: Bearer <tenant secret>` to a destination the tenant never
  named. `follow_redirects=False` on the client so httpx cannot do it behind our
  back.
* **DNS rebinding.** The name is resolved by the guard and again by the client
  when it connects. `client.stream()` plus `assert_peer_public` on the open
  response — before any body read — is what closes that gap. `client.post()`
  would read the body during the call and leave nothing to inspect. That is the
  single most important line in this file and the one a refactor is most likely
  to break.
* **Response size capped during the read**, not after. Buffering then checking is
  the bug: a hostile endpoint streaming gigabytes must cost us 20 KB and one
  round trip.
* **JSON only.** HTML and XML are where injection payloads and parser bombs live
  and we have no need to read them.
* **Allowlist projection.** Only leaves the tenant selected reach the prompt, so
  a field an attacker adds to their own API response later is invisible.
* **Credentials never leave a local.** `redact` builds the log view by
  construction — the executor knows exactly which header or query key it put the
  credential in, so nothing depends on guessing which value looks secret.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import quote, urlparse
from uuid import UUID

import httpx

from app.core.breaker import guard
from app.core.constants import (
    ACTION_ARRAY_PROJECTION_LIMIT,
    ACTION_BREAKER_NAME,
    ACTION_CONVERSATION_RATE_PER_MINUTE,
    ACTION_MAX_CALLS_PER_CONVERSATION,
    ACTION_QUOTA_EXEMPT_HANDLERS,
    ACTION_VISITOR_RATE_PER_MINUTE,
    IDENTITY_CONTACT_NAMESPACES,
    RL_SCOPE_ACTION_CONVERSATION,
    RL_SCOPE_ACTION_ONE,
    RL_SCOPE_ACTION_VISITOR,
)
from app.core.errors import (
    CircuitOpenError,
    ProviderError,
    RateLimitError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.urlguard import assert_fetchable, assert_peer_public
from app.domain.actions import (
    DENIED_HEADER_PREFIXES,
    DENIED_HEADERS,
    HEADER_NAME_RE,
    SECRET_TEMPLATE_RE,
    TEMPLATE_RE,
    ActionAuth,
    ActionAuthType,
    ActionContext,
    ActionDefinition,
    ActionEffect,
    ActionKind,
    ActionOutcome,
    ActionOutcomeCode,
    ActionParameter,
    ActionResponse,
    DataAccess,
    ParamType,
    RenderedRequest,
)

logger = get_logger(__name__)

_SCALARS = (str, int, float, bool)


class SecretResolver(Protocol):
    """Decrypts one `tenant_secrets` row into plaintext, for one call only.

    Not a port in `app/domain/ports.py`: it reaches our own Postgres through the
    same Fernet helper the channel integrations use, so it is an adapter detail
    rather than an external system.
    """

    async def resolve(self, tenant_id: UUID, ref: str) -> str | None: ...

    async def resolve_named(self, tenant_id: UUID, action_id: UUID) -> dict[str, str]: ...


class ContactResolver(Protocol):
    """Fills the `{{contact.*}}` / `{{user.*}}` namespace, or refuses to.

    Not a port in `app/domain/ports.py` for the same reason `SecretResolver` is
    not: `IdentityService` reaches our own Postgres through `ContactsRepo` under
    RLS, so it is an adapter detail rather than an external system. Structural
    typing is what keeps the security envelope from importing the service layer.
    """

    async def resolve_for_action(
        self,
        tenant_id: UUID,
        *,
        contact_id: UUID | None,
        verified: bool,
        holes: Sequence[str] = (),
    ) -> Any: ...


def _contact_holes(action: ActionDefinition) -> tuple[str, ...]:
    """The contact fields this action's request depends on. Bare names only.

    The same idiom as `ActionDefinition._templates_resolve`, over the same blob
    and the same `TEMPLATE_RE` the renderer uses, so a hole this misses is a hole
    that does not render either. `contact` and `user` are one namespace over one
    row, so `{{user.email}}` and `{{contact.email}}` collapse to `email`.
    """
    if action.http is None:
        return ()
    blob = json.dumps(action.http.model_dump(mode="json"))
    found: list[str] = []
    for match in TEMPLATE_RE.finditer(blob):
        namespace, _, rest = match.group(1).partition(".")
        if namespace in IDENTITY_CONTACT_NAMESPACES and rest and rest not in found:
            found.append(rest)
    return tuple(found)


# --------------------------------------------------------------------- redaction


def redact(request: RenderedRequest, auth: ActionAuth) -> dict[str, Any]:
    """Log-safe view of an outbound request. Values are shapes, not content.

    Auth material is removed by construction rather than by pattern: the executor
    knows exactly which header or query key it put the credential in, so nothing
    depends on guessing which value looks secret. Ordinary values are shapes too,
    because an outbound body legitimately contains the customer's email and order
    number.
    """
    headers: dict[str, Any] = {}
    auth_header = (request.auth_header_name or "").lower()
    for name, value in request.headers.items():
        if name.lower() == auth_header or name.lower() == "authorization":
            headers[name] = "<redacted>"
        else:
            headers[name] = len(value)

    parsed = urlparse(request.url)
    query_pairs: list[str] = []
    for pair in parsed.query.split("&") if parsed.query else []:
        key, _, _value = pair.partition("=")
        if request.auth_query_key and key == request.auth_query_key:
            query_pairs.append(f"{key}=<redacted>")
        else:
            query_pairs.append(f"{key}={len(_value)}")

    return {
        "method": request.method,
        # the path is tenant-authored and may carry an interpolated identifier, so
        # only the destination and the shape of the path are logged
        "host": parsed.hostname,
        "path_segments": len([part for part in parsed.path.split("/") if part]),
        "query": query_pairs,
        "headers": headers,
        "body": _body_shape(json.loads(request.body) if request.body else None),
        "auth_type": str(auth.type),
    }


def _body_shape(value: Any) -> Any:
    """Recursive `{key: type_and_length}`. Never a value."""
    if isinstance(value, Mapping):
        return {str(key): _body_shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_body_shape(item) for item in value[:5]]
    if isinstance(value, str):
        return f"str[{len(value)}]"
    if value is None:
        return "null"
    return type(value).__name__


# -------------------------------------------------------------------- projection


def _is_projectable_leaf(value: Any) -> bool:
    """Whether a value may be emitted as a selected leaf.

    An object leaf is refused. Emitting one wholesale would defeat the allowlist:
    every field inside it — including one an attacker adds to their own API
    response next week — would reach the model without the tenant ever choosing
    it. A list of scalars is fine and is truncated.
    """
    if value is None or isinstance(value, _SCALARS):
        return True
    if isinstance(value, list):
        return all(item is None or isinstance(item, _SCALARS) for item in value)
    return False


def _leaf_value(value: Any) -> Any:
    if isinstance(value, list):
        return value[:ACTION_ARRAY_PROJECTION_LIMIT]
    return value


def _out_key(path: str, key: str, rename: Mapping[str, str]) -> str:
    """Renames are looked up by full path first, then by bare leaf name."""
    renamed = rename.get(path)
    if renamed is None:
        renamed = rename.get(key)
    return renamed or key


def _project_into(
    dest: dict[str, Any],
    src: Any,
    segments: Sequence[str],
    path: str,
    rename: Mapping[str, str],
) -> None:
    if not isinstance(src, Mapping):
        return
    segment = segments[0]
    is_array = segment.endswith("[]")
    key = segment[:-2] if is_array else segment
    if key not in src:
        return
    value = src[key]
    last = len(segments) == 1
    out_key = _out_key(path, key, rename) if last else key

    if is_array:
        if not isinstance(value, list):
            return
        # an `a[].b` projection over a 5000-row response is a prompt bomb
        items = value[:ACTION_ARRAY_PROJECTION_LIMIT]
        if last:
            if _is_projectable_leaf(items):
                dest[out_key] = items
            return
        bucket = dest.get(out_key)
        if not isinstance(bucket, list):
            bucket = []
            dest[out_key] = bucket
        for index, item in enumerate(items):
            while len(bucket) <= index:
                bucket.append({})
            child = bucket[index]
            if isinstance(child, dict):
                _project_into(child, item, segments[1:], path, rename)
        return

    if last:
        if _is_projectable_leaf(value):
            dest[out_key] = _leaf_value(value)
        return
    child_dict = dest.get(out_key)
    if not isinstance(child_dict, dict):
        child_dict = {}
        dest[out_key] = child_dict
    _project_into(child_dict, value, segments[1:], path, rename)


def project(
    payload: Any, paths: Sequence[str], rename: Mapping[str, str]
) -> dict[str, Any]:
    """Build a new object containing only allowlisted leaves.

    Allowlist, not denylist: a field the tenant never selected is invisible,
    including one an attacker adds to a response later. Paths are `a.b` for
    objects and `a[].b` to project across an array, capped at 6 segments and 100
    paths by the schema so a projection cannot become the cost problem.
    """
    result: dict[str, Any] = {}
    for path in paths:
        _project_into(result, payload, path.split("."), path, rename)
    return result


def shape_payload(payload: Any, response: ActionResponse) -> dict[str, Any]:
    """The object the model is allowed to see.

    A non-object top level (array, scalar) is wrapped as `{"result": value}` so
    the projector and the prompt template always see an object.
    """
    if response.data_access is DataAccess.LIMITED:
        return project(payload, response.allowed_fields, response.rename)
    if isinstance(payload, Mapping):
        return {str(key): value for key, value in payload.items()}
    return {"result": payload}


# ------------------------------------------------------------ argument coercion


def coerce_arguments(
    parameters: Sequence[ActionParameter], arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate and coerce what the model passed against the declared parameters.

    The model is a well-meaning but unreliable caller: it sends `"3"` for an
    integer, omits a required field, and occasionally invents one. Anything not
    declared is dropped rather than forwarded, because an undeclared key can only
    have come from the conversation and has no template hole to fill.

    Raises `ValidationError` so the caller can turn it into a tool error the model
    can read and retry against.
    """
    declared = {parameter.name: parameter for parameter in parameters}
    result: dict[str, Any] = {}

    for name, parameter in declared.items():
        if name not in arguments or arguments[name] is None:
            if parameter.required:
                raise ValidationError(f"missing required parameter {name!r}")
            continue
        raw = arguments[name]

        match parameter.type:
            case ParamType.INTEGER:
                try:
                    value: Any = int(str(raw).strip())
                except (TypeError, ValueError) as exc:
                    raise ValidationError(f"{name!r} must be an integer") from exc
            case ParamType.NUMBER:
                try:
                    value = float(str(raw).strip())
                except (TypeError, ValueError) as exc:
                    raise ValidationError(f"{name!r} must be a number") from exc
            case ParamType.BOOLEAN:
                if isinstance(raw, bool):
                    value = raw
                elif str(raw).strip().lower() in ("true", "yes", "1"):
                    value = True
                elif str(raw).strip().lower() in ("false", "no", "0"):
                    value = False
                else:
                    raise ValidationError(f"{name!r} must be true or false")
            case _:
                value = str(raw).strip()
                if len(value) > parameter.max_length:
                    raise ValidationError(
                        f"{name!r} is longer than {parameter.max_length} characters"
                    )

        if parameter.type is ParamType.ENUM and value not in parameter.enum:
            raise ValidationError(f"{name!r} must be one of {', '.join(parameter.enum)}")
        if parameter.type is ParamType.DATE and not re.match(
            r"^\d{4}-\d{2}-\d{2}$", str(value)
        ):
            raise ValidationError(f"{name!r} must be an ISO-8601 date")
        if parameter.type is ParamType.EMAIL and not re.match(
            r"^[^@\s]+@[^@\s.]+\.[^@\s]+$", str(value)
        ):
            raise ValidationError(f"{name!r} must be an email address")
        if parameter.pattern is not None and not re.match(parameter.pattern, str(value)):
            raise ValidationError(f"{name!r} does not match the required format")
        if (
            parameter.minimum is not None
            and isinstance(value, (int, float))
            and float(value) < parameter.minimum
        ):
            raise ValidationError(f"{name!r} is below the minimum")
        if (
            parameter.maximum is not None
            and isinstance(value, (int, float))
            and float(value) > parameter.maximum
        ):
            raise ValidationError(f"{name!r} is above the maximum")

        result[name] = value

    return result


# ------------------------------------------------------------------- templating


class TemplateScope:
    """Resolves one template hole. Nothing outside these namespaces can render.

    `{{input.*}}` values are PII-restored on the way through: `runner._mask`
    replaced the visitor's email with `[EMAIL_1]` before the model ever saw it, so
    an un-restored hole would send the placeholder to the tenant's order API and
    every lookup would fail. Masking exists to keep PII away from the model
    vendor, not away from the customer's own system of record. `{{contact.*}}`
    needs no restore — it comes from the database and never passed through the
    model.
    """

    def __init__(
        self,
        *,
        arguments: Mapping[str, Any],
        ctx: ActionContext,
        secrets: Mapping[str, str] | None = None,
    ) -> None:
        self._arguments = arguments
        self._namespaces = ctx.variables()
        self._restore = ctx.restore
        self._secrets = secrets or {}
        self.restored_labels: set[str] = set()

    def lookup(self, hole: str) -> Any:
        namespace, _, rest = hole.partition(".")
        if not rest:
            return None
        if namespace == "input":
            if rest not in self._arguments:
                return None
            return self._restore_value(self._arguments[rest])
        source = self._namespaces.get(namespace)
        if source is None:
            return None
        return source.get(rest)

    def secret(self, name: str) -> str | None:
        return self._secrets.get(name)

    def _restore_value(self, value: Any) -> Any:
        if self._restore is None or not isinstance(value, str):
            return value
        restored = self._restore(value)
        if restored != value:
            self.restored_labels |= _placeholder_labels(value)
        return restored


def _placeholder_labels(value: str) -> set[str]:
    """PII labels present in a masked string. Labels only, never values.

    `runner._mask` writes `[EMAIL_1]`, `[PHONE_IN_2]` and so on, so the label is
    everything before the trailing index.
    """
    return {match.group(1) for match in re.finditer(r"\[([A-Z][A-Z_]*)_\d+\]", value)}


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_text(template: str, scope: TemplateScope, *, encode: bool = False) -> str:
    """Interpolate a string template.

    `encode` percent-encodes each substituted value. That matters for the path and
    query of a URL: an un-encoded `../admin` or `?x=y` in a collected value would
    change the request target even though the host is fixed, which is the one way
    path templating could still move a call.
    """

    def substitute(match: Any) -> str:
        raw = _stringify(scope.lookup(match.group(1)))
        return quote(raw, safe="") if encode else raw

    def substitute_secret(match: Any) -> str:
        raw = scope.secret(match.group(1)) or ""
        return quote(raw, safe="") if encode else raw

    rendered = SECRET_TEMPLATE_RE.sub(substitute_secret, template)
    return TEMPLATE_RE.sub(substitute, rendered)


def render_json(node: Any, scope: TemplateScope) -> Any:
    """Interpolate a JSON body template, preserving types.

    A string leaf that is *entirely* one hole keeps the parameter's JSON type — an
    integer parameter stays an integer — which is what a typed API expects. A hole
    embedded in surrounding text renders as a string.
    """
    if isinstance(node, Mapping):
        return {str(key): render_json(value, scope) for key, value in node.items()}
    if isinstance(node, list):
        return [render_json(item, scope) for item in node]
    if not isinstance(node, str):
        return node

    whole = TEMPLATE_RE.fullmatch(node.strip())
    if whole is not None:
        return scope.lookup(whole.group(1))
    whole_secret = SECRET_TEMPLATE_RE.fullmatch(node.strip())
    if whole_secret is not None:
        return scope.secret(whole_secret.group(1)) or ""
    return render_text(node, scope)


def assert_header_safe(name: str, value: str) -> None:
    """Re-enforce the header rules on rendered values.

    The save-time check saw the template; a template hole can inject a newline the
    static check never saw, and a CR or LF in a header value is request splitting.
    """
    lowered = name.lower()
    if not HEADER_NAME_RE.match(name):
        raise ValidationError(f"{name!r} is not a valid header name")
    if lowered in DENIED_HEADERS or lowered.startswith(DENIED_HEADER_PREFIXES):
        raise ValidationError(f"header {name!r} may not be set")
    if any(char in value for char in ("\r", "\n", "\0")):
        raise ValidationError(f"header {name!r} value contains a control character")
    if len(value) > 2048:
        raise ValidationError(f"header {name!r} value is too long")


# ----------------------------------------------------------------- the executor


class ActionExecutor:
    """Renders, guards and performs one action call.

    Owns no state about a conversation and no database handle: it is given a
    definition, the model's arguments and the turn's context, and it returns an
    `ActionOutcome`. Persistence — the ledger row, the suspension row — belongs to
    `ActionService`, so the risky code has exactly one job.
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        secrets: SecretResolver,
        breaker: Any,
        rate_limiter: Any,
        settings: Any,
        identity: ContactResolver | None = None,
    ) -> None:
        self._http = http
        self._secrets = secrets
        # the platform-wide `actions` breaker. The trip decision is per action
        # (`action:<id>`) so one tenant's bad credential cannot open another
        # tenant's integration; this one carries the aggregate error-rate window
        # that /admin/health/platform reports.
        self._breaker = breaker
        self._rate_limiter = rate_limiter
        self._settings = settings
        # `IdentityService | None`. None means contact resolution is unavailable,
        # which is a refusal for any action naming a `{{contact.*}}` hole — never a
        # blank substitution. See `_resolve_contact`.
        self._identity = identity

    # ------------------------------------------------------------- the ceilings

    async def check_ceilings(
        self,
        *,
        action: ActionDefinition,
        ctx: ActionContext,
        action_id: UUID | None = None,
    ) -> ActionOutcome | None:
        """Every gate that can refuse a call, cheapest first. None means proceed.

        Ordered so the free checks run before the ones that cost a cache round
        trip, and so a refusal never depends on state a later check would have
        changed. The escape hatch is exempt from the rate limiter, which is what
        makes "actions are unavailable" honest: the worst case is being handed
        to a person, never a silently worse answer.
        """
        exempt = action.system_handler in ACTION_QUOTA_EXEMPT_HANDLERS

        if not action.available_on(ctx.channel):
            return ActionOutcome.failure(
                ActionOutcomeCode.CHANNEL_UNAVAILABLE,
                f"{action.display_name} is not available on this channel",
            )
        if action.only_in_procedures and not ctx.in_procedure:
            return ActionOutcome.failure(
                ActionOutcomeCode.DISABLED,
                f"{action.display_name} can only run as part of a guided flow",
            )
        if action.requires_verified_identity and not ctx.identity_verified:
            return ActionOutcome.failure(
                ActionOutcomeCode.IDENTITY_REQUIRED,
                "this action needs a verified visitor identity",
            )

        # The write gate, narrowed to kind IN ('server', 'client'). A `local`
        # system effect is guarded by the platform instead, and a widget makes no
        # call at all. `only_in_procedures` satisfies the gate because the step,
        # not the model, chose to fire it.
        if action.is_write_gated and not (ctx.confirmed or ctx.in_procedure):
            return ActionOutcome(
                ok=False,
                outcome=ActionOutcomeCode.AWAITING_CONFIRMATION,
                error="the visitor has not confirmed this change yet",
            )

        call_ceiling = min(
            ctx.max_calls_per_conversation, ACTION_MAX_CALLS_PER_CONVERSATION
        )
        if not exempt and ctx.calls_this_conversation >= call_ceiling:
            return ActionOutcome.failure(
                ActionOutcomeCode.CONVERSATION_LIMIT,
                "too many live lookups in this conversation",
            )
        if (
            action.effect is ActionEffect.WRITE
            and ctx.writes_this_conversation >= ctx.max_writes_per_conversation
        ):
            return ActionOutcome.failure(
                ActionOutcomeCode.WRITE_LIMIT,
                "too many changes already made in this conversation",
            )

        # Layer 3 of the identity gate, and the last check that cannot be
        # invalidated by a later one. It costs a contact row read, so it runs after
        # every free ceiling; it runs *before* the rate limiter because a call that
        # can only be refused must not spend a window the visitor may need for a
        # call that would have worked. No exemption applies: the escape hatch is
        # exempt from the limiter, never from a security control, and a system
        # handler has no HTTP request to name a contact hole in anyway.
        identity_refusal = await self._resolve_contact(action=action, ctx=ctx)
        if identity_refusal is not None:
            return identity_refusal

        if exempt:
            return None
        return await self._check_rate_limits(action=action, ctx=ctx, action_id=action_id)

    async def _resolve_contact(
        self,
        *,
        action: ActionDefinition,
        ctx: ActionContext,
    ) -> ActionOutcome | None:
        """Fill `ctx.contact` from the verified row, or refuse. Never renders "".

        Interpolating an empty string is not a degraded lookup, it is a different
        one: `?customer=` does not fail against the tenant's API, it succeeds
        against a different resource. So every path that cannot produce a real
        value refuses, including the one where no resolver was injected at all —
        "resolution unavailable" is a refusal, not a blank hole.

        The refusal names no attribute. A message listing the field that was
        missing would let a visitor enumerate what the tenant stores about its
        customers, so the names go to the log and the model is told only that the
        details are unavailable.
        """
        holes = _contact_holes(action)
        if not holes:
            return None

        if self._identity is None:
            logger.warning(
                "action_identity_unresolvable",
                tenant=str(ctx.tenant_id),
                action=action.name,
                holes=len(holes),
            )
            return ActionOutcome.failure(
                ActionOutcomeCode.IDENTITY_INCOMPLETE,
                "this action needs visitor details that are unavailable",
            )

        resolution = await self._identity.resolve_for_action(
            ctx.tenant_id,
            contact_id=ctx.contact_id,
            verified=ctx.identity_verified,
            holes=holes,
        )
        if not resolution.ok:
            if resolution.outcome == ActionOutcomeCode.IDENTITY_INCOMPLETE:
                logger.info(
                    "action_identity_incomplete",
                    tenant=str(ctx.tenant_id),
                    action=action.name,
                    missing=list(resolution.missing),
                )
                return ActionOutcome.failure(
                    ActionOutcomeCode.IDENTITY_INCOMPLETE,
                    "this action needs visitor details that are unavailable",
                )
            return ActionOutcome.failure(
                ActionOutcomeCode.IDENTITY_REQUIRED,
                "this action needs a verified visitor identity",
            )

        # The resolved namespace reaches the renderer through `ctx.variables()`,
        # which is what `TemplateScope.__init__` reads. Assigning to the turn's own
        # context rather than threading a parameter keeps `build_request` and every
        # existing call site unchanged.
        ctx.contact = resolution.values
        return None

    async def _check_rate_limits(
        self,
        *,
        action: ActionDefinition,
        ctx: ActionContext,
        action_id: UUID | None,
    ) -> ActionOutcome | None:
        """Four independent windows. A 429 never escapes as an exception.

        A `RateLimitError` propagating mid-stream would kill the answer for a
        limit that is abuse control, not correctness, so it is converted to a tool
        error the model can explain. `RateLimiter.check` already fails open on a
        cache error by design.
        """
        tenant = str(ctx.tenant_id)
        windows: list[tuple[str, str, int]] = []
        if action_id is not None:
            windows.append(
                (
                    RL_SCOPE_ACTION_ONE,
                    f"{tenant}:{action_id}",
                    action.rate_limit_per_minute,
                )
            )
        if ctx.conversation_id is not None:
            windows.append(
                (
                    RL_SCOPE_ACTION_CONVERSATION,
                    f"{tenant}:{ctx.conversation_id}",
                    ACTION_CONVERSATION_RATE_PER_MINUTE,
                )
            )
        if ctx.external_user_ref:
            windows.append(
                (
                    RL_SCOPE_ACTION_VISITOR,
                    f"{tenant}:{ctx.external_user_ref}",
                    ACTION_VISITOR_RATE_PER_MINUTE,
                )
            )

        for scope, identifier, limit in windows:
            try:
                await self._rate_limiter.check(scope, identifier, limit, 60)
            except RateLimitError:
                logger.info("action_rate_limited", scope=scope, action=action.name)
                return ActionOutcome.failure(
                    ActionOutcomeCode.RATE_LIMITED,
                    f"{action.display_name} is temporarily unavailable, try again shortly",
                )
        return None

    # ------------------------------------------------------------- the rendering

    async def build_request(
        self,
        *,
        action: ActionDefinition,
        arguments: Mapping[str, Any],
        ctx: ActionContext,
        action_id: UUID | None = None,
    ) -> tuple[RenderedRequest, tuple[str, ...]]:
        """Render the request and inject the credential.

        The credential is decrypted here, immediately before rendering, held in a
        local, and never returned in an outcome. Nothing in this method's return
        value is safe to log — `redact` is the only path from here to a log line.
        """
        http = action.http
        if http is None:  # pragma: no cover - guarded by the caller
            raise ValidationError("this action has no request to render")

        named_secrets: dict[str, str] = {}
        if action_id is not None and SECRET_TEMPLATE_RE.search(
            json.dumps(http.model_dump(mode="json"))
        ):
            named_secrets = await self._secrets.resolve_named(ctx.tenant_id, action_id)

        scope = TemplateScope(arguments=arguments, ctx=ctx, secrets=named_secrets)

        # The authority is fixed at authoring time and re-checked here, so the
        # rendered path and query are the only templated parts of the target.
        parsed = urlparse(http.url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path = render_text(parsed.path, scope, encode=True)
        query_pairs: list[str] = []
        if parsed.query:
            query_pairs.append(render_text(parsed.query, scope, encode=True))
        for key, template in http.query.items():
            value = render_text(template, scope, encode=True)
            query_pairs.append(f"{quote(key, safe='')}={value}")

        headers: dict[str, str] = {
            # we set these two, always. A tenant who could set Content-Type could
            # send multipart/form-data and shape a body we never validated; a
            # tenant who could set User-Agent could spoof a browser to evade
            # someone's bot protection and make us the abuser.
            "Content-Type": "application/json",
            "User-Agent": str(self._settings.ACTIONS_USER_AGENT),
            "Accept": "application/json",
        }
        for name, template in http.headers.items():
            value = render_text(template, scope)
            assert_header_safe(name, value)
            headers[name] = value

        body_bytes: bytes | None = None
        if http.body is not None:
            rendered_body = render_json(http.body, scope)
            if (
                action.include_transcript
                and ctx.transcript
                and isinstance(rendered_body, dict)
                and "transcript" not in rendered_body
            ):
                # Plan-gated and audited at enable time, and the transcript is the
                # PII-masked one — there is no unmasked option.
                rendered_body["transcript"] = ctx.transcript
            body_bytes = json.dumps(rendered_body, separators=(",", ":")).encode()

        auth_query_key: str | None = None
        auth_header_name: str | None = None
        if action.auth.type is not ActionAuthType.NONE and action.auth.secret_ref:
            credential = await self._secrets.resolve(
                ctx.tenant_id, action.auth.secret_ref
            )
            if not credential:
                raise ValidationError("the action credential is not configured")
            match action.auth.type:
                case ActionAuthType.BEARER:
                    headers["Authorization"] = f"Bearer {credential}"
                    auth_header_name = "Authorization"
                case ActionAuthType.BASIC:
                    raw = f"{action.auth.username or ''}:{credential}".encode()
                    headers["Authorization"] = (
                        f"Basic {base64.b64encode(raw).decode('ascii')}"
                    )
                    auth_header_name = "Authorization"
                case ActionAuthType.HEADER:
                    name = action.auth.header_name or ""
                    assert_header_safe(name, credential)
                    headers[name] = credential
                    auth_header_name = name
                case ActionAuthType.QUERY:
                    auth_query_key = action.auth.query_name or ""
                    query_pairs.append(
                        f"{quote(auth_query_key, safe='')}="
                        f"{quote(credential, safe='')}"
                    )
                case _:  # pragma: no cover - the enum is closed
                    pass

        query = "&".join(pair for pair in query_pairs if pair)
        url = f"{base}{path}" + (f"?{query}" if query else "")

        request = RenderedRequest(
            method=http.method,
            url=url,
            headers=headers,
            body=body_bytes,
            auth_query_key=auth_query_key,
            auth_header_name=auth_header_name,
        )
        return request, tuple(sorted(scope.restored_labels))

    # --------------------------------------------------------------- the dispatch

    async def execute(
        self,
        *,
        action: ActionDefinition,
        arguments: Mapping[str, Any],
        ctx: ActionContext,
        action_id: UUID | None = None,
    ) -> ActionOutcome:
        """Guard, render, call, cap, project. The whole server path."""
        blocked = await self.check_ceilings(action=action, ctx=ctx, action_id=action_id)
        if blocked is not None:
            return blocked

        if action.kind is not ActionKind.SERVER or action.http is None:
            return ActionOutcome.failure(
                ActionOutcomeCode.NOT_CONFIGURED,
                "this action does not make a server-side request",
            )

        try:
            coerced = coerce_arguments(action.parameters, arguments)
        except ValidationError as exc:
            # handed back to the model rather than raised: it can read this and
            # ask the visitor for the missing value
            return ActionOutcome.failure(
                ActionOutcomeCode.INVALID_ARGUMENTS, str(exc)
            )

        try:
            request, labels = await self.build_request(
                action=action, arguments=coerced, ctx=ctx, action_id=action_id
            )
        except ValidationError as exc:
            return ActionOutcome.failure(ActionOutcomeCode.BLOCKED_HEADER, str(exc))

        # SSRF on the *rendered* URL, every time. allow_private is hardcoded False
        # rather than read from URL_FETCH_ALLOW_PRIVATE: document ingestion wants
        # localhost fixtures in development, an action pointed at localhost is a
        # habit that becomes a production incident.
        if urlparse(request.url).scheme != "https":
            return ActionOutcome.failure(
                ActionOutcomeCode.BLOCKED_SSRF, "action url must be https"
            )
        try:
            assert_fetchable(
                request.url,
                allow_private=False,
                allowed_ports=self._settings.actions_allowed_ports,
            )
        except ValidationError as exc:
            logger.warning(
                "action_blocked_ssrf", action=action.name, tenant=str(ctx.tenant_id)
            )
            return ActionOutcome.failure(ActionOutcomeCode.BLOCKED_SSRF, str(exc))

        max_bytes = min(
            action.response.max_bytes, int(self._settings.ACTIONS_MAX_RESPONSE_BYTES)
        )
        outcome = await self._call(
            action=action, request=request, action_id=action_id, max_bytes=max_bytes
        )
        outcome.request_bytes = request.request_bytes
        outcome.restored_labels = labels
        outcome.quota_exempt = action.system_handler in ACTION_QUOTA_EXEMPT_HANDLERS

        if labels:
            logger.info("action_pii_restored", action=action.name, labels=list(labels))
        logger.info(
            "action_called",
            action=action.name,
            status=outcome.http_status,
            duration_ms=outcome.duration_ms,
            bytes=outcome.bytes_read,
            outcome=str(outcome.outcome),
            request=redact(request, action.auth),
        )
        return outcome

    async def _call(
        self,
        *,
        action: ActionDefinition,
        request: RenderedRequest,
        action_id: UUID | None,
        max_bytes: int,
    ) -> ActionOutcome:
        """The guarded wire call. Every failure becomes an outcome, never a raise.

        The breaker is keyed on the action id, not the host: two tenants both
        calling `api.stripe.com` would otherwise share a breaker, so one tenant's
        bad credential would trip the other tenant's integration.
        """
        breaker_name = (
            f"action:{action_id}" if action_id is not None else ACTION_BREAKER_NAME
        )
        timeouts = httpx.Timeout(
            action.http.timeout_seconds if action.http else 10.0, connect=5.0
        )
        started = time.monotonic()
        pending: ActionOutcome | None = None
        outcome: ActionOutcome
        try:
            with guard(breaker_name):
                pending = await self._stream_call(
                    request=request,
                    response_spec=action.response,
                    timeouts=timeouts,
                    max_bytes=max_bytes,
                    started=started,
                )
                if pending.http_status is not None and pending.http_status >= 500:
                    # a 5xx is evidence the upstream is unhealthy, so it has to
                    # reach the breaker; a 4xx means it answered and our request
                    # was wrong, and counting that would open the breaker for
                    # every conversation behind one bad parameter
                    raise ProviderError("action", f"HTTP {pending.http_status}")
                outcome = pending
        except CircuitOpenError:
            outcome = ActionOutcome.failure(
                ActionOutcomeCode.CIRCUIT_OPEN,
                f"{action.display_name} is temporarily unreachable",
                duration_ms=_elapsed_ms(started),
            )
        except ProviderError:
            outcome = pending if pending is not None else ActionOutcome.failure(
                ActionOutcomeCode.HTTP_ERROR,
                "the action target returned an error",
                duration_ms=_elapsed_ms(started),
            )
        except ValidationError as exc:
            # assert_peer_public rejected the address the socket actually reached
            logger.warning("action_blocked_ssrf", action=action.name, reason="peer")
            outcome = ActionOutcome.failure(
                ActionOutcomeCode.BLOCKED_SSRF, str(exc), duration_ms=_elapsed_ms(started)
            )
        except httpx.TimeoutException:
            outcome = ActionOutcome.failure(
                ActionOutcomeCode.TIMEOUT,
                f"{action.display_name} did not respond in time",
                duration_ms=_elapsed_ms(started),
            )
        except httpx.HTTPError as exc:
            outcome = ActionOutcome.failure(
                ActionOutcomeCode.TRANSPORT_ERROR,
                f"could not reach the action target: {type(exc).__name__}",
                duration_ms=_elapsed_ms(started),
            )

        try:
            self._breaker.note_call(success=outcome.ok)
        except Exception:  # pragma: no cover - health accounting is best effort
            logger.debug("action_breaker_note_failed", action=action.name)
        return outcome

    async def _stream_call(
        self,
        *,
        request: RenderedRequest,
        response_spec: ActionResponse,
        timeouts: httpx.Timeout,
        max_bytes: int,
        started: float,
    ) -> ActionOutcome:
        """`client.stream`, because the body must not be read before the peer check.

        `client.post()` would read the body during the call and leave no open
        response to inspect, so DNS rebinding would be unguarded. This is the
        single most important line in the file.
        """
        async with self._http.stream(
            request.method,
            request.url,
            headers=request.headers,
            content=request.body,
            timeout=timeouts,
        ) as response:
            assert_peer_public(response, allow_private=False)

            if response.is_redirect:
                # following it would re-attach the tenant's credential to a
                # destination the tenant never named, which is how secrets leak
                return ActionOutcome.failure(
                    ActionOutcomeCode.REDIRECTED,
                    "the action target redirected; configure the final URL directly",
                    http_status=response.status_code,
                    duration_ms=_elapsed_ms(started),
                )

            if response.status_code >= 400:
                return ActionOutcome.failure(
                    ActionOutcomeCode.HTTP_ERROR,
                    f"the action target returned HTTP {response.status_code}",
                    http_status=response.status_code,
                    duration_ms=_elapsed_ms(started),
                )

            content_type = (
                response.headers.get("content-type", "").split(";")[0].strip().lower()
            )
            if not (content_type == "application/json" or content_type.endswith("+json")):
                # HTML and XML are where injection payloads and parser bombs live,
                # and we have no need to read them
                return ActionOutcome.failure(
                    ActionOutcomeCode.BAD_CONTENT_TYPE,
                    f"the action target returned {content_type or 'no'} content, not JSON",
                    http_status=response.status_code,
                    duration_ms=_elapsed_ms(started),
                )

            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                # a hint, never the control: chunked responses have none and a
                # lying header is trivial. this is the cheap reject only
                return ActionOutcome.failure(
                    ActionOutcomeCode.TOO_LARGE,
                    f"action response exceeded {max_bytes} bytes",
                    http_status=response.status_code,
                    duration_ms=_elapsed_ms(started),
                )

            buffer = bytearray()
            async for chunk in response.aiter_bytes(4096):
                buffer.extend(chunk)
                if len(buffer) > max_bytes:
                    # abort the read: leaving the context drops the connection, so
                    # an endpoint streaming gigabytes costs us the cap and one RTT
                    return ActionOutcome.failure(
                        ActionOutcomeCode.TOO_LARGE,
                        f"action response exceeded {max_bytes} bytes",
                        http_status=response.status_code,
                        duration_ms=_elapsed_ms(started),
                        bytes_read=len(buffer),
                    )
            status = response.status_code

        try:
            payload = json.loads(bytes(buffer)) if buffer else {}
        except ValueError:
            return ActionOutcome.failure(
                ActionOutcomeCode.BAD_JSON,
                "the action target returned a body that is not valid JSON",
                http_status=status,
                duration_ms=_elapsed_ms(started),
                bytes_read=len(buffer),
            )

        return ActionOutcome(
            ok=True,
            outcome=ActionOutcomeCode.OK,
            data=shape_payload(payload, response_spec),
            http_status=status,
            duration_ms=_elapsed_ms(started),
            bytes_read=len(buffer),
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)



