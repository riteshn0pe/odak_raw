"""The three growth routes a stranger's browser reaches.

None of them carries a session, because none of them can: the person filling in
a contact form has no account, and the person clicking unsubscribe in a mail is
being asked to prove nothing except that they hold the link we sent them.

* ``GET /growth/form.js`` — a small embeddable script that posts a page's
  contact form to the endpoint below. The tenant pastes one script tag; they do
  not write any JavaScript.
* ``POST /v1/growth/form`` — the form endpoint. Rate limited per key and per IP,
  body-capped, and authenticated by the same signed ingress key the webhook door
  uses, so both doors share one archive-then-enqueue path.
* ``GET`` / ``POST /growth/unsubscribe/{token}`` — the opt-out. GET renders a
  confirmation and changes nothing; POST performs it. A GET that unsubscribed
  would be a URL that a mail scanner or a link-preview fetcher presses on the
  recipient's behalf.

Everything here is unauthenticated by design, so everything here is bounded: a
size cap before parsing, a rate limit before any database read that a caller
controls, and no response that distinguishes "wrong key" from "unknown tenant".
"""

from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app.api.deps import ContainerDep
from app.core.errors import AuthenticationError, ValidationError
from app.core.logging import get_logger, tenant_id_var

logger = get_logger(__name__)

router = APIRouter(tags=["growth"], include_in_schema=False)

PROVIDER = "website_form"
INBOUND_JOB = "growth_inbound_lead"

# Unauthenticated until the key is verified, so both are bounded first.
MAX_BODY_BYTES = 64 * 1024
FORM_RATE_PER_MINUTE = 20
IP_RATE_PER_MINUTE = 10

# The consent checkbox. Named here because the snippet emits it and the endpoint
# reads it, and a mismatch between the two would silently mean nobody ever
# opts in to WhatsApp.
CONSENT_FIELD = "whatsapp_optin"


# ------------------------------------------------------------------- snippet


_FORM_JS = """/* Growth form bridge.
 * Usage: <script src="{base}/growth/form.js" data-key="YOUR_KEY"></script>
 * Every <form data-growth-form> on the page is posted here as JSON as well as
 * to wherever it already went, so adding this never breaks an existing form.
 */
(function () {{
  var script = document.currentScript;
  var key = script && script.getAttribute("data-key");
  var endpoint = "{base}/v1/growth/form";
  if (!key) {{ return; }}

  function collect(form) {{
    var out = {{}};
    var data = new FormData(form);
    data.forEach(function (value, name) {{
      if (typeof value === "string") {{ out[name] = value; }}
    }});
    return out;
  }}

  function send(form) {{
    var body = collect(form);
    body.key = key;
    try {{
      // keepalive so the post survives the navigation a submit usually causes
      fetch(endpoint, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify(body),
        keepalive: true,
        mode: "cors"
      }}).catch(function () {{}});
    }} catch (e) {{ /* never block the page's own submit */ }}
  }}

  document.addEventListener("submit", function (event) {{
    var form = event.target;
    if (form && form.matches && form.matches("[data-growth-form]")) {{ send(form); }}
  }}, true);
}})();
"""


@router.get("/growth/form.js")
async def form_js(request: Request, container: ContainerDep) -> Response:
    """The embeddable snippet, served from the API as a CDN substitute.

    Built from the configured public base URL rather than shipped as a static
    file, because the endpoint it posts to has to be *this* deployment — a
    hard-coded host would work in production and silently post to production
    from staging.
    """
    base = str(container.settings.PUBLIC_BASE_URL or "").rstrip("/")
    return Response(
        content=_FORM_JS.format(base=base),
        media_type="application/javascript; charset=utf-8",
        headers={
            # short, so a tenant who rotates their key is not serving the old
            # snippet from a hundred browser caches for a day
            "Cache-Control": "public, max-age=300",
        },
    )


# ---------------------------------------------------------------- submission


@router.post("/v1/growth/form")
async def submit_form(request: Request, container: ContainerDep) -> dict[str, Any]:
    """One website form submission. Fast, bounded, and archived before parsed."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ValidationError("submission is too large")

    payload = _read(raw)
    key = str(payload.pop("key", "") or "") or request.headers.get("x-ingress-key", "")

    from app.services.growth.ingresskey import parse_ingress_key

    try:
        tenant_id, provider = parse_ingress_key(container.settings.SECRET_KEY, key)
    except AuthenticationError:
        logger.warning(
            "webhook_signature_rejected",
            provider=PROVIDER,
            reason="bad form key",
            ip=_client_ip(request),
        )
        raise
    if provider != PROVIDER:
        raise AuthenticationError("this key is not a website-form key")

    # rate limited on both dimensions: per key so one tenant's compromised
    # snippet cannot fill their own lead table, per IP so one script cannot fill
    # every tenant's. Checked after the signature so an unauthenticated flood
    # cannot consume a legitimate tenant's window
    await container.rate_limiter.check("growth_form", str(tenant_id), FORM_RATE_PER_MINUTE, 60)
    ip = _client_ip(request)
    if ip:
        await container.rate_limiter.check("growth_form_ip", ip, IP_RATE_PER_MINUTE, 60)

    tenant_id_var.set(str(tenant_id))
    await _record_consent(container, tenant_id, payload)

    from app.api.webhooks.ingress import accept_inbound

    return await accept_inbound(container, tenant_id, PROVIDER, payload, INBOUND_JOB)


async def _record_consent(
    container: Any, tenant_id: Any, payload: dict[str, Any]
) -> None:
    """Record a WhatsApp opt-in when — and only when — the box was ticked.

    A form submission is not WhatsApp consent; an explicit tick is. The proof is
    stored verbatim so that when somebody asks what the basis for contact was,
    there is an answer that is not "we assumed".

    Failure is swallowed: a missing consent row means the acknowledgement goes
    by email instead, which is the safe direction. Failing the submission would
    lose the lead over a bookkeeping write.
    """
    if str(payload.get(CONSENT_FIELD, "")).strip().lower() not in ("1", "true", "yes", "on"):
        return
    phone = str(payload.get("phone") or payload.get("mobile") or payload.get("whatsapp") or "")
    if not phone.strip():
        return
    try:
        await container.growth_compliance.record_consent(
            tenant_id,
            phone,
            channel="whatsapp",
            source="website_form",
            proof={"field": CONSENT_FIELD, "value": payload.get(CONSENT_FIELD)},
        )
    except Exception:
        logger.warning(
            "growth_form_consent_failed", tenant_id=str(tenant_id), exc_info=True
        )


# --------------------------------------------------------------- unsubscribe


@router.get("/growth/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe_page(token: str, container: ContainerDep) -> HTMLResponse:
    """Confirm, and change nothing.

    A GET that unsubscribes is a URL that a mail scanner, a chat client's link
    preview or a browser's speculative prefetch will eventually press for the
    recipient — the same reason the approval decision is a POST.
    """
    from app.services.growth.optout import parse as parse_optout

    try:
        _, address = parse_optout(container.settings.SECRET_KEY, token)
    except AuthenticationError:
        return HTMLResponse(_page("This link is no longer valid.", ""), status_code=400)
    return HTMLResponse(
        _page(
            f"Stop emails to {html.escape(address)}?",
            f'<form method="post" action="/growth/unsubscribe/{html.escape(token)}">'
            '<button type="submit">Unsubscribe me</button></form>',
        )
    )


@router.post("/growth/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe(token: str, container: ContainerDep) -> HTMLResponse:
    """Perform the opt-out. Also the RFC 8058 one-click target."""
    from app.services.growth.optout import parse as parse_optout

    try:
        tenant_id, address = parse_optout(container.settings.SECRET_KEY, token)
    except AuthenticationError:
        return HTMLResponse(_page("This link is no longer valid.", ""), status_code=400)

    tenant_id_var.set(str(tenant_id))
    await container.growth_compliance.unsubscribe(
        tenant_id, address, reason="unsubscribed", source="unsubscribe_link"
    )
    return HTMLResponse(
        _page(
            "Done — you won't hear from us again.",
            f"<p>{html.escape(address)} has been removed from every channel.</p>",
        )
    )


@router.get("/v1/growth/stores/shopify/callback", response_class=HTMLResponse)
async def shopify_callback(
    request: Request, container: ContainerDep
) -> HTMLResponse:
    """Shopify OAuth landing: trade the one-time code for a permanent token.

    Unauthenticated by necessity — Shopify redirects the owner's browser here
    with no session — so two signatures stand in for one. The signed `state`
    proves *we* started this install for this tenant and this shop; Shopify's
    own `hmac` over the query string proves the parameters (the `code` above
    all) came from Shopify and not from whoever is holding the browser. Both are
    checked before anything is stored. The token lands in tenant_secrets; this
    response confirms the shop name and nothing else.
    """
    from uuid import UUID

    from app.api.v1.growth import parse_oauth_state

    params = dict(request.query_params)
    code = (params.get("code") or "").strip()
    shop = (params.get("shop") or "").strip().lower()
    state = (params.get("state") or "").strip()
    if not code or not shop or not state:
        raise ValidationError("Shopify did not return a code")

    app_config = container.shopify
    if app_config is None:
        raise ValidationError("Shopify is not configured on this deployment yet")

    from app.adapters.stores.shopify import verify_oauth_callback

    if not verify_oauth_callback(params, container.settings.SHOPIFY_API_SECRET):
        # Shopify's documented requirement, and the half of the check that
        # `state` cannot do: without it a leaked authorize URL lets an attacker
        # finish somebody else's install with a code from a shop they control
        logger.warning("shopify_oauth_hmac_rejected", shop=shop[:120])
        raise ValidationError("that Shopify callback could not be verified")

    tenant_id: UUID
    state_shop: str
    tenant_id, state_shop = parse_oauth_state(container, state)
    if state_shop != shop:
        raise ValidationError("Shopify shop mismatch — start the connect step again")
    tenant_id_var.set(str(tenant_id))

    # unauthenticated by design (Shopify redirects here with no session), so
    # bounded like every other public door: one shop cannot burn the exchange
    # budget for everyone else
    await container.rate_limiter.check("shopify_oauth", _client_ip(request) or shop, 30, 60)

    from app.adapters.stores.shopify import exchange_shopify_code, shop_domain

    domain = shop_domain(shop)
    # the exchange needs no stored credential: the one-time code *is* the
    # credential, and it dies here whether the exchange succeeds or not
    try:
        token = await exchange_shopify_code(
            shop=domain,
            api_secret=container.settings.SHOPIFY_API_SECRET,
            api_key=str(app_config["api_key"]),
            code=code,
        )
    except Exception as exc:
        logger.warning(
            "shopify_oauth_exchange_failed", tenant_id=str(tenant_id), error=str(exc)[:200]
        )
        return HTMLResponse(
            _page(
                "Shopify connect failed",
                "<p>Shopify refused the code. Try connecting again.</p>",
            ),
            status_code=400,
        )

    ref = await container.growth_stores.store_credential(
        tenant_id, "shopify", domain, token
    )
    from datetime import UTC, datetime

    # a reconnect keeps its sync cursors (see the WooCommerce connect route
    # for why wiping them is pure waste)
    previous = await container.growth.integrations.get(tenant_id, "shopify", domain)
    health = dict((previous or {}).get("health") or {})
    health["connected_at"] = datetime.now(UTC).isoformat()
    health.pop("last_error", None)
    await container.growth.integrations.upsert(
        tenant_id,
        "shopify",
        status="active",
        external_ref=domain,
        secret_ref=ref,
        health=health,
    )
    # subscribe the shop's topics now that the token exists. Never fatal: the
    # service records the outcome on `health` and the hourly sync works either
    # way, so a Shopify hiccup here must not cost the tenant a connect they have
    # just approved in their browser
    await container.growth_stores.register_webhooks(tenant_id, "shopify", domain)
    await container.growth.audit.record(
        tenant_id,
        agent="owner",
        action="store_connected",
        input={"provider": "shopify", "store": domain},
        reason="Shopify OAuth completed",
    )
    logger.info("shopify_connected", tenant_id=str(tenant_id), shop=domain)
    return HTMLResponse(
        _page(
            "Shopify connected",
            f"<p><strong>{html.escape(domain)}</strong> is connected. "
            "You can close this tab and return to the dashboard — "
            "your customers and orders will sync within the hour.</p>",
        )
    )


@router.get("/v1/growth/crm/{provider}/callback", response_class=HTMLResponse)
async def crm_callback(
    provider: str, request: Request, container: ContainerDep
) -> HTMLResponse:
    """CRM OAuth landing: trade the one-time code for a stored grant.

    Unauthenticated by necessity — the owner's browser lands here straight from
    the CRM's consent screen with no session. The signed `state` proves we
    started this connect for this tenant and names the provider (and, for
    Zoho, the data centre). Unlike Shopify there is no second signature over
    the query string, because neither Zoho nor HubSpot signs the callback;
    what stands in for it is PKCE — the code is only exchangeable by whoever
    can present the verifier minted at connect time, and only the challenge
    ever left this server. A code from somebody else's consent screen fails
    the exchange even with a valid `state`.

    Zoho also sends `accounts-server` (its data-centre host) and `location`
    (the same as a code); the host is allow-listed inside the adapter rather
    than trusted, because the next thing that happens to it is a POST carrying
    our client secret.
    """
    from uuid import UUID

    from app.api.v1.growth import parse_oauth_state

    provider = (provider or "").strip().lower()
    params = dict(request.query_params)
    code = (params.get("code") or "").strip()
    state = (params.get("state") or "").strip()
    if provider not in ("zoho", "hubspot"):
        raise ValidationError("unknown CRM")
    if not code or not state:
        return HTMLResponse(
            _page(
                "CRM connect failed",
                "<p>The CRM did not return a code. Try connecting again.</p>",
            ),
            status_code=400,
        )

    tenant_id: UUID
    state_name: str
    tenant_id, state_name = parse_oauth_state(container, state)
    # the state's second field is `provider.region.verifier` (Zoho) or
    # `provider.app.verifier` (HubSpot); a provider mismatch means the
    # callback is answering a different connect than the one this route was
    # reached through
    state_provider, _, rest = state_name.partition(".")
    if state_provider.strip().lower() != provider:
        raise ValidationError("CRM mismatch — start the connect step again")
    region, _, verifier = rest.partition(".")
    if not verifier:
        # every state this deployment mints carries one; a state without it is
        # either not ours or from before PKCE, and both are expired connects
        raise ValidationError("that connect link has expired — start again")
    region = region.strip().lower()
    tenant_id_var.set(str(tenant_id))

    # unauthenticated by design, so bounded like every other public door — and
    # on the tenant the state names as well as the IP, because x-forwarded-for
    # is trivially rotated and the tenant is not
    await container.rate_limiter.check(
        "crm_oauth", _client_ip(request) or provider, 30, 60
    )
    await container.rate_limiter.check("crm_oauth_tenant", str(tenant_id), 30, 60)

    accounts_server = (params.get("accounts-server") or "").strip()
    try:
        await container.growth_crm.complete(
            tenant_id,
            provider,
            code,
            region=region,
            accounts_server=accounts_server,
            verifier=verifier,
        )
    except Exception as exc:
        logger.warning(
            "crm_oauth_exchange_failed",
            tenant_id=str(tenant_id),
            provider=provider,
            error=str(exc)[:200],
        )
        return HTMLResponse(
            _page(
                "CRM connect failed",
                "<p>The CRM refused the code. Try connecting again from the "
                "dashboard.</p>",
            ),
            status_code=400,
        )
    label = "Zoho CRM" if provider == "zoho" else "HubSpot"
    logger.info("crm_connected", tenant_id=str(tenant_id), provider=provider)
    return HTMLResponse(
        _page(
            f"{label} connected",
            f"<p><strong>{label}</strong> is connected. You can close this tab "
            "and return to the dashboard — your existing leads are being "
            "queued to push now.</p>",
        )
    )


def _read(raw: bytes) -> dict[str, Any]:
    """JSON or form-encoded; browsers post both and we accept both."""
    import json
    from urllib.parse import parse_qsl

    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise ValidationError("empty submission")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise ValidationError("submission is not valid JSON") from None
        if not isinstance(data, dict):
            raise ValidationError("submission must be a JSON object")
        return data
    parsed = dict(parse_qsl(text, keep_blank_values=True))
    if not parsed:
        raise ValidationError("submission could not be read")
    return parsed


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else ""


def _page(heading: str, body: str) -> str:
    """A plain, self-contained page.

    No stylesheet link, no script, no font: this is rendered inside a mail
    client's browser on a phone with an unknown network, and the one thing it
    must do is load.
    """
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Unsubscribe</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;margin:0;padding:3rem 1.5rem;"
        "max-width:32rem;color:#1a1a1a}button{font:inherit;padding:.6rem 1.2rem;"
        "border:1px solid #1a1a1a;background:#1a1a1a;color:#fff;border-radius:.4rem;"
        "cursor:pointer}</style></head><body>"
        f"<h1>{html.escape(heading)}</h1>{body}</body></html>"
    )
