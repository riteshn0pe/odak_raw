"""Public, unauthenticated surfaces served from the API origin.

Two things live here and nothing else, because everything here is reachable
without a credential:

* ``GET /`` — the landing page, which carries the Google sign-in button. The
  button asks ``/v1/auth/google/start`` for Google's consent URL and sends the
  browser there; Google comes back to ``GET /login`` on this same origin, which
  finishes the exchange server-side and hands the session to the dashboard SPA
  in the URL fragment (the handoff the SPA adopts on boot).
* the SPA redirects — ``/app`` and ``/dashboard`` are dashboard routes, so a
  stray visit on the API origin is sent there rather than dead-ending on a
  404. ``/signup`` is a relic of the removed email/password signup and lands on
  the sign-in card.

Email/password login was removed by owner decision (2026-09-13): the sign-in
card goes straight to Google, and a first-time identity is provisioned
server-side (see ``app/api/v1/auth.py::google_identity_to_session``).

None of these routes read a tenant. They are public HTML by design; the /login
callback verifies a short-lived state cookie before it spends an outbound call
on the token exchange.
"""

from __future__ import annotations

import html
import json
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.auth.google import exchange_code

logger = get_logger(__name__)
router = APIRouter(tags=["public"], include_in_schema=False)

# The landing page stashes Google's `state` here before redirecting away; the
# /login callback requires the same value back before exchanging the code.
# SameSite=Lax so the top-level redirect back from Google carries it.
_STATE_COOKIE = "ca.g.state"


def _settings(request: Request) -> Settings:
    return request.app.state.container.settings  # type: ignore[no-any-return]


def _dashboard_base(settings: Settings) -> str:
    """Where the SPA lives. In local dev the app runs on the Vite origin, not the
    API origin, so app routes (/documents, …) do not exist here — send the
    browser to the dashboard instead of 404ing."""
    return settings.DASHBOARD_URL.rstrip("/") or "http://localhost:5173"


def _signin_redirect(message: str | None = None) -> RedirectResponse:
    """Back to the landing page's sign-in card, optionally with a message."""
    if not message:
        return RedirectResponse("/#signin", status_code=303)
    return RedirectResponse(f"/?e={quote(message)}#signin", status_code=303)


# /signup is a relic of the removed email/password flow; /login without a code
# is a stray visit (or a declined consent) — both land on the sign-in card.
@router.get("/signup", include_in_schema=False)
async def _signup_to_landing() -> RedirectResponse:
    return RedirectResponse("/#signin", status_code=307)


@router.get("/login", include_in_schema=False)
async def login_callback(
    request: Request,
    code: Annotated[str | None, ...] = None,
    state: Annotated[str | None, ...] = None,
    error: Annotated[str | None, ...] = None,
) -> RedirectResponse:
    """Finish Google sign-in on the API origin.

    Google redirects here with ``?code=&state=``. The state cookie set by the
    landing page must match; then the code is exchanged, the identity becomes a
    session (signing in an existing user or provisioning a first-timer), and the
    session is handed to the SPA in the URL fragment — never a query parameter,
    which would land the token in an access log.
    """
    container = request.app.state.container
    settings = container.settings

    if error or not code or not state:
        return _signin_redirect("Google sign-in was cancelled")
    cookie_state = request.cookies.get(_STATE_COOKIE, "")
    if not cookie_state or cookie_state != state:
        return _signin_redirect("Google sign-in could not be verified, please try again")

    try:
        identity = await exchange_code(
            code,
            settings.GOOGLE_REDIRECT_URI,
            settings.GOOGLE_CLIENT_ID,
            settings.GOOGLE_CLIENT_SECRET,
            settings.HTTP_TIMEOUT_SECONDS,
        )
        # imported late: avoids a router-import cycle (v1 imports nothing here,
        # but keeping the dependency one-directional costs nothing)
        from app.api.v1.auth import google_identity_to_session

        result = await google_identity_to_session(identity, None, settings)
    except Exception as exc:  # any failure returns to the card, logged
        logger.warning("google_landing_callback_failed", error=str(exc)[:200])
        return _signin_redirect("Google sign-in failed, please try again")

    session = {
        "token": result.access_token,
        "email": identity["email"],
        "tenants": [
            {"tenant_id": str(m.tenant_id), "slug": m.slug, "name": m.name, "role": m.role}
            for m in result.memberships
        ],
        "activeSlug": result.tenant_slug,
    }
    response = RedirectResponse(
        _dashboard_base(settings) + "/#auth=" + quote(json.dumps(session)),
        status_code=303,
    )
    response.delete_cookie(_STATE_COOKIE, path="/")
    return response


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request) -> HTMLResponse:
    settings = _settings(request)
    product = html.escape(settings.PRODUCT_NAME)
    # json.dumps, not html.escape: this lands inside a <script> where HTML
    # entities are not decoded, and it produces a valid JS string literal.
    dashboard = json.dumps(_dashboard_base(settings))
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{product} — autonomous agents for your business</title>
<meta name="description" content="A swarm of autonomous agents that research, reach out, run campaigns and report — supervised by the business owner on Telegram or WhatsApp.">
<style>{_LANDING_CSS}</style></head>
<body>
{_header(product)}
<main>
<section class="wrap hero">
<span class="eyebrow">✦ A swarm that works while you don't</span>
<h1>Autonomous agents for <span class="dim">your business</span></h1>
<p class="lede">A team of specialised agents — research, outreach, campaigns, reports —
runs your growth every day on a shared knowledge base. They prepare the work, you
approve what matters, and the results reach you on Telegram or WhatsApp.</p>
<div class="ctas"><a class="btn primary lg" href="#signin">Sign in</a></div>
</section>
<section class="auth-wrap" id="signin">
<form class="auth-card" id="ca-auth-form">
<h2 id="ca-auth-title">Sign in</h2>
<p class="auth-sub">Continue with Google — a first sign-in creates your workspace.</p>
<div class="auth-error" id="ca-auth-error" role="alert"></div>
<button class="btn primary google-btn" id="ca-google-btn" type="button">
{_GOOGLE_MARK}
<span>Continue with Google</span>
</button>
</form>
</section>
</main>
{_footer(product)}
<script>var CA_DASHBOARD={dashboard};
{_AUTH_JS}</script>
</body></html>"""
    return HTMLResponse(body)


_GOOGLE_MARK = """<svg width="16" height="16" viewBox="0 0 18 18" aria-hidden="true" focusable="false"><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.91c1.7-1.57 2.69-3.88 2.69-6.62Z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.91-2.26c-.81.54-1.84.86-3.05.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.34A8.99 8.99 0 0 0 9 18Z"/><path fill="#FBBC05" d="M3.97 10.72a5.4 5.4 0 0 1 0-3.44V4.94H.96a9 9 0 0 0 0 8.12l3.01-2.34Z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.5.46 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A8.99 8.99 0 0 0 .96 4.94l3.01 2.34C4.68 5.16 6.66 3.58 9 3.58Z"/></svg>"""


_AUTH_JS = """
(function(){
  var err=document.getElementById('ca-auth-error');
  // an error handed back from the /login callback (?e=…): show it on the card
  try{
    var e=new URLSearchParams(location.search).get('e');
    if(e){err.textContent=e;err.style.display='block';}
  }catch(x){}
  document.getElementById('ca-google-btn').addEventListener('click',function(){
    var btn=this;btn.disabled=true;
    fetch('/v1/auth/google/start')
      .then(function(r){return r.json();})
      .then(function(d){
        if(!d||!d.authorization_url){throw new Error('Google sign-in is unavailable');}
        // The /login callback requires this value back in the state cookie
        // before it spends the outbound token exchange.
        document.cookie='ca.g.state='+encodeURIComponent(d.state)+'; Path=/; Max-Age=600; SameSite=Lax';
        location.href=d.authorization_url; // leave for Google; it comes back to /login
      })
      .catch(function(e){
        err.textContent=(e&&e.message)||'Google sign-in is unavailable';
        err.style.display='block';btn.disabled=false;
      });
  });
})();
"""


def _header(product: str) -> str:
    return f"""<script>(function(){{try{{var t=localStorage.getItem('ca.theme');if(t==='dark'||(!t&&matchMedia('(prefers-color-scheme:dark)').matches))document.documentElement.classList.add('dark');}}catch(e){{}}}})();
function caToggleTheme(){{var d=document.documentElement.classList.toggle('dark');try{{localStorage.setItem('ca.theme',d?'dark':'light');}}catch(e){{}}}}</script>
<header><div class="wrap nav">
<a class="logo" href="/"><span class="mark">C</span> {product}</a>
<div class="actions">
<button class="btn ghost theme-btn" onclick="caToggleTheme()" aria-label="Toggle theme" title="Toggle theme">◐</button>
<a class="btn primary" href="#signin">Sign in</a></div>
</div></header>"""


def _footer(product: str) -> str:
    return f"""<footer><div class="wrap footer-row">
<div class="logo"><span class="mark">C</span> {product}</div>
<div>© {product}</div>
</div></footer>"""


_LANDING_CSS = """
:root{color-scheme:light;--bg:#ffffff;--fg:#0b0d12;--muted:#5a616e;--line:#e6e8ec;--card:#f8f9fb;--primary:#111114;--primary-fg:#fff;--accent:#5b5bd6;--danger:#c2372f}
html.dark{color-scheme:dark;--bg:#0a0c10;--fg:#eef1f5;--muted:#a3abb8;--line:#242a33;--card:#14171d;--primary:#f4f6f8;--primary-fg:#0a0c10;--accent:#8b8bf0;--danger:#ff6b62}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;font:16px/1.6 "Inter",system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
.wrap{max-width:1120px;margin:0 auto;padding:0 24px}
header{position:sticky;top:0;z-index:10;backdrop-filter:blur(10px);background:color-mix(in srgb,var(--bg) 82%,transparent);border-bottom:1px solid var(--line)}
.nav{display:flex;justify-content:space-between;align-items:center;height:64px}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px;letter-spacing:-.02em}
.logo .mark{width:28px;height:28px;border-radius:8px;background:var(--primary);color:var(--primary-fg);display:grid;place-items:center;font-weight:800}
nav.links{display:flex;gap:28px;align-items:center}
nav.links a{color:var(--muted);font-weight:500;font-size:15px}
nav.links a:hover{color:var(--fg)}
.actions{display:flex;gap:12px;align-items:center}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:10px 18px;border-radius:10px;font-weight:600;font-size:15px;border:1px solid transparent;cursor:pointer}
.btn.primary{background:var(--primary);color:var(--primary-fg)}
.btn.ghost{border-color:var(--line);color:var(--fg);background:transparent}
.btn.lg{padding:14px 26px;font-size:16px}
.hero{text-align:center;padding:96px 0 48px}
.eyebrow{display:inline-flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--accent);background:color-mix(in srgb,var(--accent) 12%,transparent);padding:6px 14px;border-radius:999px;margin-bottom:24px}
h1{font-size:clamp(2.2rem,5.5vw,4rem);line-height:1.05;letter-spacing:-.035em;margin:0 auto;max-width:16ch;font-weight:800}
h1 .dim{color:var(--muted)}
.lede{font-size:clamp(1.05rem,2vw,1.3rem);color:var(--muted);max-width:44ch;margin:22px auto 0}
.ctas{margin-top:34px;display:flex;gap:14px;justify-content:center;flex-wrap:wrap}
.auth-wrap{display:flex;justify-content:center;padding:16px 0 96px;scroll-margin-top:80px}
.auth-card{width:100%;max-width:400px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px;text-align:left}
.auth-card h2{margin:0 0 4px;font-size:20px;letter-spacing:-.01em}
.auth-sub{color:var(--muted);font-size:14px;margin:0 0 18px}
.auth-error{display:none;color:var(--danger);font-size:13px;margin-bottom:12px}
.auth-card .btn{width:100%;margin-top:6px}
.google-btn svg{display:block}
footer{border-top:1px solid var(--line);padding:40px 0;color:var(--muted);font-size:14px}
.footer-row{display:flex;justify-content:space-between;flex-wrap:wrap;gap:16px}
@media(max-width:720px){nav.links{display:none}
"""
