// Tenant session handling.

import { api, setTokenSource } from "./api";

const TENANT_KEY = "kado.tenant.session";

export interface TenantSession {
  token: string;
  email: string;
  tenants: { tenant_id: string; slug: string; name: string; role: string }[];
  activeSlug: string | null;
}

// Where the sign-in form lives: the API-origin landing page. In dev the SPA
// and the API are different origins (5173 vs 8000), so the origin is baked at
// dev/build time from the same VITE_API_TARGET the dev proxy uses — see
// __API_ORIGIN__ in vite.config.ts. Empty means same-origin.
const API_ORIGIN = typeof __API_ORIGIN__ !== "undefined" ? __API_ORIGIN__ : "";

/** Absolute URL of the landing page's sign-in card (the login surface). */
export function landingUrl(): string {
  return `${API_ORIGIN}/#signin`;
}

function read<T>(key: string): T | null {
  const raw = localStorage.getItem(key);
  return raw ? (JSON.parse(raw) as T) : null;
}

function write(key: string, value: unknown): void {
  localStorage.setItem(key, JSON.stringify(value));
}

// which token the api client sends comes from one source — the tenant session —
// so no call site has to juggle it.
setTokenSource(() => read<TenantSession>(TENANT_KEY)?.token ?? null);

// --- tenant ---

// Email/password login and signup were removed with the backend's
// /v1/auth/login and /v1/auth/signup (owner decision, 2026-09-13): the sign-in
// button goes straight to Google, which provisions a first-time identity
// server-side. Only the Google pair below remains for the tenant app.

export function tenantSession(): TenantSession | null {
  return read<TenantSession>(TENANT_KEY);
}

// --- google oauth ---

// Starts the OAuth flow: the server builds Google's consent URL from its
// configured client id; the caller stashes `state` and redirects the browser.
export async function googleStart(): Promise<{ authorization_url: string; state: string }> {
  return api.get<{ authorization_url: string; state: string }>("/v1/auth/google/start");
}

// Completes the flow after Google redirects back with a code. Provisioning of a
// new tenant happens server-side, mirroring signup; the AuthResult carries no
// email, so it is filled in later by /v1/auth/me.
export async function googleLogin(
  code: string,
  redirectUri?: string,
  tenantName?: string,
): Promise<TenantSession> {
  const result = await api.post<{
    access_token: string;
    memberships: { tenant_id: string; slug: string; name: string; role: string }[];
  }>("/v1/auth/google/callback", { code, redirect_uri: redirectUri, tenant_name: tenantName });
  const session: TenantSession = {
    token: result.access_token,
    email: "",
    tenants: result.memberships,
    activeSlug: result.memberships[0]?.slug ?? null,
  };
  write(TENANT_KEY, session);
  return session;
}

export function switchTenant(slug: string): void {
  const session = tenantSession();
  if (session) write(TENANT_KEY, { ...session, activeSlug: slug });
}

// the active tenant slug rides on X-Tenant so a multi-tenant user's requests
// resolve to the tenant they are currently looking at
export function tenantHeaders(): Record<string, string> {
  const slug = tenantSession()?.activeSlug;
  return slug ? { "X-Tenant": slug } : {};
}

export function logout(): void {
  localStorage.removeItem(TENANT_KEY);
  location.href = landingUrl();
}

// Landing-page handoff, inbound: the sign-in form on the API-origin landing
// page posts to /v1/auth itself and sends the whole session here in the URL
// fragment (fragments never reach a server, so the token does not end up in an
// access log). Called once on boot, before the router mounts, so the session
// exists by the time any screen reads it. The hash is stripped either way so a
// refresh or a shared link does not re-run the handoff.
export function adoptHandoffSession(): void {
  const payload = location.hash.match(/#auth=(.+)$/)?.[1];
  if (!payload) return;
  history.replaceState({}, "", location.pathname + location.search);
  try {
    const session = JSON.parse(decodeURIComponent(payload)) as TenantSession;
    if (session?.token) write(TENANT_KEY, session);
  } catch {
    // malformed handoff — drop it; the visitor lands signed-out on the landing page
  }
}
