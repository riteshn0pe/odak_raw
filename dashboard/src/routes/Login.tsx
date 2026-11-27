// Google-only sign-in. The button goes straight to Google's consent screen
// (via /auth/google/start, which mints the `state` we check on the way back);
// a first-time identity provisions a tenant + owner + default agent server-side,
// so there is no signup form to show. Email/password login was removed by owner
// decision (2026-09-13).
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { googleLogin, googleStart } from "../lib/auth";
import { ApiError } from "../lib/api";
import { Alert, Button, Spinner, ThemeToggle } from "../components/ui";
import { GlobalProgress } from "../components/GlobalProgress";

// Google echoes the state we minted at /google/start back on the redirect; we
// stash it here between the two page loads so a callback we did not initiate is
// rejected before it reaches the server.
const GOOGLE_STATE_KEY = "ca.google.oauth.state";

const USE_CASES = ["Support", "Knowledge base", "Product docs", "Sales enablement"];

// Multi-colour Google mark, inlined so the button carries no network dependency.
function GoogleMark() {
  return (
    <svg width="16" height="16" viewBox="0 0 18 18" aria-hidden className="shrink-0">
      <path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.91c1.7-1.57 2.69-3.88 2.69-6.62Z" />
      <path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.91-2.26c-.81.54-1.84.86-3.05.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.34A8.99 8.99 0 0 0 9 18Z" />
      <path fill="#FBBC05" d="M3.97 10.72a5.4 5.4 0 0 1 0-3.44V4.94H.96a9 9 0 0 0 0 8.12l3.01-2.34Z" />
      <path fill="#EA4335" d="M9 3.58c1.32 0 2.5.46 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A8.99 8.99 0 0 0 .96 4.94l3.01 2.34C4.68 5.16 6.66 3.58 9 3.58Z" />
    </svg>
  );
}

export function Login() {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();

  async function startGoogle() {
    setBusy(true); setError("");
    try {
      const { authorization_url, state } = await googleStart();
      sessionStorage.setItem(GOOGLE_STATE_KEY, state);
      location.href = authorization_url; // leave the SPA; Google brings us back
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Google sign-in is unavailable");
      setBusy(false);
    }
  }

  // Google redirects back to this route with ?code=&state=. Finish the exchange,
  // persist the session, then land on the dashboard.
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const code = params.get("code");
    const state = params.get("state");
    if (!code) return;

    const expected = sessionStorage.getItem(GOOGLE_STATE_KEY);
    sessionStorage.removeItem(GOOGLE_STATE_KEY);
    // clear the code/state from the URL so a refresh does not re-run the exchange
    window.history.replaceState({}, "", location.pathname);

    if (!expected || expected !== state) {
      setError("Google sign-in could not be verified, please try again");
      return;
    }

    setBusy(true);
    googleLogin(code)
      .then(() => nav("/agents"))
      .catch((e) => setError(e instanceof ApiError ? e.message : "something went wrong"))
      .finally(() => setBusy(false));
  }, [nav]);

  return (
    // flex, not grid: the legacy app.css `.grid` rule is unlayered and forces a
    // 16px gap that would show as a seam between the two panes.
    <div className="min-h-screen bg-canvas flex">
      <GlobalProgress />
      {/* Left: the sign-in. Owns the whole viewport below lg. */}
      <div className="relative flex-1 flex items-center justify-center px-6 py-14">
        <div className="absolute top-4 right-4"><ThemeToggle compact /></div>

        <div className="w-full max-w-sm flex flex-col gap-4">
          <div className="flex items-center gap-2.5 font-bold text-[15px] tracking-tight">
            <span className="w-7 h-7 rounded-lg bg-primary text-primary-fg grid place-items-center font-extrabold">K</span>
            kado
          </div>

          <div className="mt-2">
            <h1 className="text-2xl font-bold tracking-tight m-0">Welcome</h1>
            <p className="text-sm text-muted mt-1.5 m-0">Sign in to your workspace.</p>
          </div>

          <Button variant="primary" className="w-full py-2.5" onClick={startGoogle} disabled={busy}>
            {busy ? (
              <><Spinner /> Please wait…</>
            ) : (
              <><GoogleMark /> Continue with Google</>
            )}
          </Button>

          {error && <Alert>{error}</Alert>}
        </div>
      </div>

      {/* Right: value prop. Decorative only, so it drops on narrow screens. */}
      <div className="hidden lg:flex relative w-1/2 shrink-0 flex-col justify-center overflow-hidden px-14 text-white bg-gradient-to-br from-slate-900 via-slate-800 to-brand-hover">
        <div aria-hidden className="absolute inset-0 opacity-25"
          style={{
            backgroundImage: "radial-gradient(rgba(255,255,255,.55) 1px, transparent 1px)",
            backgroundSize: "22px 22px",
          }} />
        <div aria-hidden className="absolute -top-24 -right-24 w-96 h-96 rounded-full bg-brand/25 blur-3xl" />
        <div aria-hidden className="absolute -bottom-32 -left-16 w-96 h-96 rounded-full bg-white/5 blur-3xl" />

        <div className="relative max-w-md">
          <h2 className="text-[32px] leading-[1.15] font-bold tracking-tight m-0">
            An assistant that answers from your documents — with citations, on your site and on WhatsApp.
          </h2>
          <p className="text-[15px] leading-relaxed text-white/80 mt-5 m-0">
            Upload your content, configure once, embed anywhere. Ten minutes, no engineer required.
          </p>
          <div className="flex flex-wrap gap-2 mt-8">
            {USE_CASES.map((tag) => (
              <span key={tag} className="rounded-full bg-white/15 ring-1 ring-inset ring-white/25 px-3 py-1 text-xs font-semibold backdrop-blur-sm">
                {tag}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
